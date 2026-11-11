"""Real llama-index-core dispatcher events -> traceai_llamaindex -> loopback F01 (ChatMessage blocks)."""

import io

import pytest

pytest.importorskip("llama_index.core")
pytest.importorskip("traceai_llamaindex")

from conftest import PDF, b64, flush, png, refs, wav  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402
from fi_instrumentation.attachments import AttachmentConfig, create_attachment_manager  # noqa: E402
from traceai_llamaindex import LlamaIndexInstrumentor  # noqa: E402

types = pytest.importorskip("llama_index.core.base.llms.types")
if not hasattr(types, "ImageBlock"):
    pytest.skip("ChatMessage blocks need llama-index-core 0.12.3+", allow_module_level=True)

from llama_index.core.base.llms.types import (  # noqa: E402
    ChatMessage, ChatResponse, CompletionResponse, ImageBlock, LLMMetadata, TextBlock,
)
from llama_index.core.llms.callbacks import llm_chat_callback, llm_completion_callback  # noqa: E402
from llama_index.core.llms.custom import CustomLLM  # noqa: E402

DocumentBlock = getattr(types, "DocumentBlock", None)
AudioBlock = getattr(types, "AudioBlock", None)
IMAGE = png(seed=41)
OUTPUT = png(seed=42)


class MediaLLM(CustomLLM):
    """A real CustomLLM whose chat() is wrapped by LlamaIndex's own dispatcher callbacks."""

    reply_blocks: list = []

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(model_name="media-llm", is_chat_model=True)

    @llm_chat_callback()
    def chat(self, messages, **kwargs):
        return ChatResponse(message=ChatMessage(role="assistant", blocks=list(self.reply_blocks)))

    @llm_completion_callback()
    def complete(self, prompt, formatted=False, **kwargs):
        return CompletionResponse(text="ok")

    @llm_completion_callback()
    def stream_complete(self, prompt, formatted=False, **kwargs):
        yield CompletionResponse(text="ok", delta="ok")


@pytest.fixture
def instrument(spans):
    tracer_provider, exporter = spans
    state = {}

    def install(manager, config=None):
        LlamaIndexInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager,
                                            config=config or TraceConfig())
        state["on"] = True
        return exporter

    yield install
    if state.get("on"):
        LlamaIndexInstrumentor().uninstrument()


def chat_refs(exporter):
    return [ref for span in exporter.get_finished_spans() for ref in refs(span)]


def test_input_and_output_blocks(f01, instrument):
    manager = f01.manager()
    exporter = instrument(manager)
    llm = MediaLLM(reply_blocks=[TextBlock(text="here"), ImageBlock(image=OUTPUT, image_mimetype="image/png")])
    blocks = [TextBlock(text="what"), ImageBlock(image=IMAGE, image_mimetype="image/png")]
    if AudioBlock is not None:
        blocks.append(AudioBlock(audio=wav(), format="wav"))
    if DocumentBlock is not None:
        blocks.append(DocumentBlock(data=PDF, document_mimetype="application/pdf"))
    llm.chat([ChatMessage(role="user", blocks=blocks)])
    flush(manager)
    expected = [IMAGE, OUTPUT] + ([wav()] if AudioBlock is not None else []) + \
        ([PDF] if DocumentBlock is not None else [])
    assert sorted(f01.payloads) == sorted(expected)
    found = {(r["direction"], r.get("message_index"), r.get("part_index"), r["mime_type"])
             for r in chat_refs(exporter)}
    assert ("input", 0, 1, "image/png") in found
    if AudioBlock is not None:
        assert ("input", 0, 2, "audio/wav") in found
    assert ("output", 0, 1, "image/png") in found
    purposes = {body["mime_type"]: body["purpose"] for body in f01.uploads if body["direction"] == "input"}
    assert purposes["image/png"] == "model_input"
    if DocumentBlock is not None:
        assert purposes["application/pdf"] == "document"


def test_url_path_and_stream_sources_are_never_read(f01, instrument, tmp_path):
    manager = f01.manager()
    instrument(manager)
    path = tmp_path / "secret.png"
    path.write_bytes(IMAGE)
    stream = io.BytesIO(IMAGE)
    blocks = [ImageBlock(url="http://127.0.0.1:9/never.png", image_mimetype="image/png"),
              ImageBlock(path=path, image_mimetype="image/png")]
    try:
        blocks.append(ImageBlock(image=stream, image_mimetype="image/png"))
    except Exception:
        pass  # older cores reject IOBase sources at construction
    MediaLLM().chat([ChatMessage(role="user", blocks=blocks)])
    flush(manager)
    assert f01.payloads == []
    assert stream.tell() in (0, len(IMAGE))  # never read by the instrumentation beyond what LlamaIndex did


def test_mismatched_magic_is_rejected(f01, instrument):
    manager = f01.manager()
    instrument(manager)
    MediaLLM().chat([ChatMessage(role="user", blocks=[ImageBlock(image=PDF, image_mimetype="image/png")])])
    flush(manager)
    assert f01.payloads == []


@pytest.mark.parametrize("config", [TraceConfig(hide_inputs=True), TraceConfig(hide_input_images=True),
                                    TraceConfig(pii_redaction=True), TraceConfig(hide_input_messages=True)])
def test_local_restrictions_deny_input(f01, instrument, config):
    manager = f01.manager()
    instrument(manager, config=config)
    MediaLLM().chat([ChatMessage(role="user", blocks=[ImageBlock(image=IMAGE, image_mimetype="image/png")])])
    flush(manager)
    assert f01.after_bootstrap() == []


def test_hide_outputs_denies_output(f01, instrument):
    manager = f01.manager()
    instrument(manager, config=TraceConfig(hide_outputs=True))
    MediaLLM(reply_blocks=[ImageBlock(image=OUTPUT, image_mimetype="image/png")]).chat(
        [ChatMessage(role="user", content="hi")])
    flush(manager)
    assert f01.after_bootstrap() == []


def test_disabled_and_missing_manager_capture_nothing(f01, instrument):
    instrument(create_attachment_manager(config=AttachmentConfig(enabled=False)))
    MediaLLM().chat([ChatMessage(role="user", blocks=[ImageBlock(image=IMAGE, image_mimetype="image/png")])])
    LlamaIndexInstrumentor().uninstrument()
    instrument(None)
    MediaLLM().chat([ChatMessage(role="user", blocks=[ImageBlock(image=IMAGE, image_mimetype="image/png")])])
    f01.manager()
    assert f01.after_bootstrap() == []
