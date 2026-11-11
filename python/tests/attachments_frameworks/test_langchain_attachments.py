"""Real langchain-core callbacks -> traceai_langchain -> loopback F01 (typed message media)."""

from typing import Any, List, Optional

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("traceai_langchain")

from conftest import PDF, assert_uris_resolve, b64, flush, png, refs, wav  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402
from fi_instrumentation.attachments import AttachmentConfig, create_attachment_manager  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF  # noqa: E402
from traceai_langchain import LangChainInstrumentor  # noqa: E402

IMAGE = png(seed=21)
OUTPUT = png(seed=22)
TOOL = png(seed=23)


class FakeChat(BaseChatModel):
    """A real BaseChatModel: LangChain's own callback manager drives the tracer."""

    reply: Any = "ok"
    fail: bool = False

    @property
    def _llm_type(self) -> str:
        return "fake-media"

    def _generate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None, run_manager=None,
                  **kwargs: Any) -> ChatResult:
        if self.fail:
            raise RuntimeError("provider said: secret error body")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])


@pytest.fixture
def instrument(spans):
    tracer_provider, exporter = spans
    state = {}

    def install(manager, config=None, provider=None):
        LangChainInstrumentor().instrument(tracer_provider=provider or tracer_provider, attachments=manager,
                                           config=config or TraceConfig())
        state["on"] = True
        return exporter

    yield install
    if state.get("on"):
        LangChainInstrumentor().uninstrument()


def llm_span(exporter):
    found = [s for s in exporter.get_finished_spans() if s.attributes.get("gen_ai.span.kind") == "LLM"
             or s.name == "FakeChat"]
    assert found, [s.name for s in exporter.get_finished_spans()]
    return found[-1]


def human(*blocks):
    return HumanMessage(content=[{"type": "text", "text": "describe"}, *blocks])


BLOCK_STYLES = {
    "openai_image_url": ({"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64(IMAGE)}},
                         IMAGE, "image/png", "model_input"),
    "lc03_standard": ({"type": "image", "source_type": "base64", "data": b64(IMAGE), "mime_type": "image/png"},
                      IMAGE, "image/png", "model_input"),
    "lc1_content_block": ({"type": "image", "base64": b64(IMAGE), "mime_type": "image/png"},
                          IMAGE, "image/png", "model_input"),
    "anthropic_source": ({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                      "data": b64(IMAGE)}}, IMAGE, "image/png", "model_input"),
    "openai_file_pdf": ({"type": "file", "file": {"filename": "a.pdf",
                                                  "file_data": "data:application/pdf;base64," + b64(PDF)}},
                        PDF, "application/pdf", "document"),
    "openai_input_audio": ({"type": "input_audio", "input_audio": {"data": b64(wav()), "format": "wav"}},
                           wav(), "audio/wav", "model_input"),
}


@pytest.mark.parametrize("style", sorted(BLOCK_STYLES))
def test_input_block_styles_upload_original_bytes(f01, instrument, style):
    block, data, mime, purpose = BLOCK_STYLES[style]
    manager = f01.manager()
    exporter = instrument(manager)
    FakeChat().invoke([human(block)])
    flush(manager)
    assert f01.payloads == [data]
    (body,) = f01.uploads
    assert body["mime_type"] == mime and body["direction"] == "input" and body["purpose"] == purpose
    span_refs = refs(llm_span(exporter))
    assert [(r["occurrence_id"], r["mime_type"]) for r in span_refs] == [(body["occurrence_id"], mime)]


def test_output_media_and_tool_results(f01, instrument):
    manager = f01.manager()
    exporter = instrument(manager)
    reply = [{"type": "text", "text": "here"}, {"type": "image", "base64": b64(OUTPUT), "mime_type": "image/png"}]
    tool = ToolMessage(content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64(TOOL)}}],
                       tool_call_id="call_screen_1")
    ask = AIMessage(content="", tool_calls=[{"id": "call_screen_1", "name": "screenshot", "args": {}}])
    FakeChat(reply=reply).invoke([human(), ask, tool])
    flush(manager)
    assert sorted(f01.payloads) == sorted([TOOL, OUTPUT])
    bodies = {body["direction"]: body for body in f01.uploads}
    assert bodies["input"]["purpose"] == "tool_result" and bodies["input"].get("tool_call_id") == "call_screen_1"
    assert bodies["output"]["purpose"] == "model_output"
    positions = {(r["direction"], r.get("message_index"), r.get("part_index"), r.get("role"))
                 for r in refs(llm_span(exporter))}
    assert ("input", 2, 0, "tool") in positions and ("output", 0, 1, "assistant") in positions


def test_failed_run_never_captures_output_and_input_still_follows_policy(f01, instrument):
    manager = f01.manager()
    instrument(manager)
    with pytest.raises(RuntimeError):
        FakeChat(fail=True, reply=[{"type": "image", "base64": b64(OUTPUT), "mime_type": "image/png"}]).invoke(
            [human({"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64(IMAGE)}})])
    flush(manager)
    assert f01.payloads == [IMAGE]


@pytest.mark.parametrize("config", [
    TraceConfig(hide_inputs=True), TraceConfig(hide_input_messages=True), TraceConfig(hide_input_images=True),
    TraceConfig(hide_input_text=True), TraceConfig(pii_redaction=True),
])
def test_local_input_restrictions_deny(f01, instrument, config):
    manager = f01.manager()
    instrument(manager, config=config)
    FakeChat().invoke([human({"type": "image", "base64": b64(IMAGE), "mime_type": "image/png"})])
    flush(manager)
    assert f01.after_bootstrap() == []


def test_pii_redaction_beats_server_allowance(f01, instrument):
    f01.state["policy"]["allow_unredacted_media_with_text_pii"] = True
    manager = f01.manager()
    instrument(manager, config=TraceConfig(pii_redaction=True))
    FakeChat().invoke([human({"type": "image", "base64": b64(IMAGE), "mime_type": "image/png"})])
    flush(manager)
    assert f01.after_bootstrap() == []


def test_hide_outputs_denies_output_only(f01, instrument):
    manager = f01.manager()
    instrument(manager, config=TraceConfig(hide_outputs=True))
    FakeChat(reply=[{"type": "image", "base64": b64(OUTPUT), "mime_type": "image/png"}]).invoke(
        [human({"type": "image", "base64": b64(IMAGE), "mime_type": "image/png"})])
    flush(manager)
    assert f01.payloads == [IMAGE]


def test_server_policy_audio_only_denies_images(f01, instrument):
    f01.state["policy"]["allowed_modalities"] = ["audio"]
    manager = f01.manager()
    instrument(manager)
    FakeChat().invoke([human({"type": "image", "base64": b64(IMAGE), "mime_type": "image/png"},
                             {"type": "input_audio", "input_audio": {"data": b64(wav()), "format": "wav"}})])
    flush(manager)
    assert f01.payloads == [wav()]


def test_disabled_missing_and_unsampled_capture_nothing(f01, instrument, monkeypatch):
    block = {"type": "image", "base64": b64(IMAGE), "mime_type": "image/png"}
    instrument(create_attachment_manager(config=AttachmentConfig(enabled=False)))
    FakeChat().invoke([human(block)])
    LangChainInstrumentor().uninstrument()
    instrument(None)
    FakeChat().invoke([human(block)])
    LangChainInstrumentor().uninstrument()
    manager = f01.manager()
    exporter = InMemorySpanExporter()
    unsampled = TracerProvider(sampler=ALWAYS_OFF)
    unsampled.add_span_processor(SimpleSpanProcessor(exporter))
    instrument(manager, provider=unsampled)
    FakeChat().invoke([human(block)])
    flush(manager)
    assert f01.after_bootstrap() == []


def test_env_disable_wins(f01, instrument, monkeypatch):
    manager = f01.manager()
    instrument(manager)
    monkeypatch.setenv("FI_ATTACHMENTS_ENABLED", "false")
    FakeChat().invoke([human({"type": "image", "base64": b64(IMAGE), "mime_type": "image/png"})])
    flush(manager)
    assert f01.after_bootstrap() == []


def test_unsupported_sources_are_ignored(f01, instrument):
    manager = f01.manager()
    instrument(manager)
    FakeChat().invoke([human(
        {"type": "image_url", "image_url": {"url": "https://example.invalid/remote.png"}},
        {"type": "image", "source_type": "id", "id": "file-abc"},
        {"type": "file", "file": {"file_id": "file-abc"}},
        {"type": "image", "base64": b64(IMAGE), "mime_type": "image/gif"},
        {"type": "image", "base64": "@@@@", "mime_type": "image/png"},
        {"type": "image", "base64": b64(b"GIF89a-not-png"), "mime_type": "image/png"},
    )])
    flush(manager)
    assert f01.payloads == []


def test_same_image_on_two_chat_spans_gives_each_span_its_own_occurrence(f01, instrument):
    from langchain_core.runnables import RunnableLambda

    manager = f01.manager()
    exporter = instrument(manager)
    block = {"type": "image", "base64": b64(IMAGE), "mime_type": "image/png"}
    chain = RunnableLambda(lambda _: FakeChat().invoke([human(block)])) | \
        RunnableLambda(lambda _: FakeChat().invoke([human(block)]))
    chain.invoke("x")
    flush(manager)
    assert f01.payloads == [IMAGE, IMAGE]  # byte deduplication is server-side
    chats = [s for s in exporter.get_finished_spans() if s.name == "FakeChat"]
    assert [len(refs(s)) for s in chats] == [1, 1]
    owned = {r["occurrence_id"]: format(s.context.span_id, "016x") for s in chats for r in refs(s)}
    assert owned == {b["occurrence_id"]: b["association"]["parent"]["span_id"] for b in f01.uploads}
    assert_uris_resolve(exporter)
