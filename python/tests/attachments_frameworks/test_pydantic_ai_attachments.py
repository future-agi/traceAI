"""Real Pydantic AI agent runs (FunctionModel) -> traceai_pydantic_ai -> loopback F01."""

import http.server
import threading

import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("traceai_pydantic_ai")

from conftest import PDF, assert_uris_resolve, b64, flush, png, refs, wav  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402
from fi_instrumentation.attachments import AttachmentConfig, create_attachment_manager  # noqa: E402
from pydantic_ai import Agent, BinaryContent, ImageUrl  # noqa: E402
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402
from traceai_pydantic_ai import PydanticAIInstrumentor  # noqa: E402

try:
    from pydantic_ai.messages import FilePart
except ImportError:  # older SDKs have no generated-file parts
    FilePart = None

IMAGE = png(seed=61)
GENERATED = png(seed=62)
SCREEN = png(seed=63)


def echo(messages, info: AgentInfo):
    return ModelResponse(parts=[TextPart("ok")])


@pytest.fixture
def instrument(spans):
    tracer_provider, exporter = spans
    state = {}

    def install(manager, config=None):
        # skip_dep_check: pydantic-ai-slim does not satisfy the instrumentor's "pydantic-ai" requirement.
        PydanticAIInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager,
                                            config=config or TraceConfig(), skip_dep_check=True)
        state["on"] = True
        return exporter

    yield install
    if state.get("on"):
        PydanticAIInstrumentor().uninstrument()


def request_spans(exporter):
    return [s for s in exporter.get_finished_spans() if "model" in s.name.lower() and "request" in s.name.lower()]


def test_binary_content_prompt(f01, instrument):
    manager = f01.manager()
    exporter = instrument(manager)
    Agent(FunctionModel(echo)).run_sync(["what is this", BinaryContent(data=IMAGE, media_type="image/png"),
                                         BinaryContent(data=PDF, media_type="application/pdf"),
                                         BinaryContent(data=wav(), media_type="audio/wav")])
    flush(manager)
    assert sorted(f01.payloads) == sorted([IMAGE, PDF, wav()])
    purposes = {body["mime_type"]: body["purpose"] for body in f01.uploads}
    assert purposes == {"image/png": "model_input", "application/pdf": "document", "audio/wav": "model_input"}
    owners = [s for s in request_spans(exporter) if refs(s)]
    assert len(owners) == 1 and len(refs(owners[0])) == 3


def _screenshot_agent():
    calls = {"n": 0}

    def model(messages, info: AgentInfo):
        calls["n"] += 1
        if calls["n"] == 1:
            return ModelResponse(parts=[ToolCallPart(tool_name="screenshot", args={}, tool_call_id="call_1")])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(model))

    @agent.tool_plain
    def screenshot() -> BinaryContent:
        return BinaryContent(data=SCREEN, media_type="image/png")

    try:
        agent.run_sync("take a screenshot")
    except Exception as exc:  # noqa: BLE001
        # pydantic-ai 0.1.0's FunctionModel cannot JSON-serialize a binary tool return when it estimates
        # usage for the second request; the tool span has already finished by then.
        if "serializ" not in str(exc).lower():
            raise


def tool_spans(exporter):
    return [s for s in exporter.get_finished_spans() if s.name.startswith("pydantic_ai.tool.call")]


def test_tool_result_binary_is_owned_by_each_observing_span(f01, instrument):
    manager = f01.manager()
    exporter = instrument(manager)
    _screenshot_agent()
    flush(manager)
    (tool,) = [s for s in tool_spans(exporter) if refs(s)]
    (ref,) = refs(tool)
    # Span refs carry purpose and indices only; tool_call_id travels in the upload request.
    assert ref["purpose"] == "tool_result"
    by_occurrence = {b["occurrence_id"]: b for b in f01.uploads}
    body = by_occurrence[ref["occurrence_id"]]
    assert body["association"]["parent"]["span_id"] == format(tool.context.span_id, "016x")
    assert body["purpose"] == "tool_result" and body["direction"] == "input" and body.get("tool_call_id") == "call_1"
    # Newer SDKs reach the second model request, which re-sends the tool return as model input: that span
    # owns ANOTHER occurrence of the same bytes (never the tool span's).
    owners = [s for s in exporter.get_finished_spans() if refs(s)]
    assert len(f01.payloads) == len(owners) and set(f01.payloads) == {SCREEN}
    for span in owners:
        for r in refs(span):
            assert by_occurrence[r["occurrence_id"]]["association"]["parent"]["span_id"] == \
                format(span.context.span_id, "016x")
    assert_uris_resolve(exporter)


@pytest.mark.parametrize("flag", ["hide_outputs", "hide_output_text", "hide_output_messages"])
def test_tool_span_output_hide_flags_deny_its_tool_result_media(f01, instrument, flag):
    """The tool span redacts its result under the OUTPUT hide flags; the same media must not be captured there."""
    manager = f01.manager()
    exporter = instrument(manager, config=TraceConfig(**{flag: True}))
    _screenshot_agent()
    flush(manager)
    tools = tool_spans(exporter)
    assert tools, [s.name for s in exporter.get_finished_spans()]
    tool_ids = {format(s.context.span_id, "016x") for s in tools}
    for span in tools:
        assert refs(span) == [], (flag, span.name)
        result = (span.attributes or {}).get("pydantic_ai.tool.result")
        assert result is None or result == "__REDACTED__", (flag, result)
    assert not [b for b in f01.uploads if b["association"]["parent"]["span_id"] in tool_ids], flag
    # Only a later model request (input-governed, so not hidden by an output flag) may own a copy.
    owners = {s.name for s in exporter.get_finished_spans() if refs(s)}
    assert all(span in {s.name for s in request_spans(exporter)} for span in owners), owners
    assert_uris_resolve(exporter)


@pytest.mark.skipif(FilePart is None, reason="FilePart needs a newer pydantic-ai")
def test_generated_file_output_and_error_path(f01, instrument):
    manager = f01.manager()
    instrument(manager)

    def generate(messages, info):
        return ModelResponse(parts=[TextPart("here"), FilePart(content=BinaryContent(data=GENERATED,
                                                                                       media_type="image/png"))])

    Agent(FunctionModel(generate)).run_sync("draw")

    def failing(messages, info):
        raise RuntimeError("provider error body: secret")

    with pytest.raises(Exception):
        Agent(FunctionModel(failing)).run_sync(["fail", BinaryContent(data=IMAGE, media_type="image/png")])
    flush(manager)
    outputs = [body for body in f01.uploads if body["direction"] == "output"]
    assert len(outputs) == 1 and outputs[0]["purpose"] == "model_output"
    assert GENERATED in f01.payloads


def test_urls_are_never_fetched(f01, instrument):
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        manager = f01.manager()
        instrument(manager)
        Agent(FunctionModel(echo)).run_sync(["look", ImageUrl(url="http://127.0.0.1:%d/private.png"
                                                                  % server.server_port),
                                             ImageUrl(url="http://169.254.169.254/latest/meta-data")])
        flush(manager)
        assert hits == [] and f01.after_bootstrap() == []
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("config", [TraceConfig(hide_inputs=True), TraceConfig(hide_input_images=True),
                                    TraceConfig(pii_redaction=True), TraceConfig(hide_input_text=True)])
def test_local_restrictions_deny(f01, instrument, config):
    manager = f01.manager()
    instrument(manager, config=config)
    Agent(FunctionModel(echo)).run_sync(["x", BinaryContent(data=IMAGE, media_type="image/png")])
    flush(manager)
    assert f01.after_bootstrap() == []


def test_mismatched_declared_type_and_disabled_manager(f01, instrument):
    instrument(create_attachment_manager(config=AttachmentConfig(enabled=False)))
    Agent(FunctionModel(echo)).run_sync(["x", BinaryContent(data=IMAGE, media_type="image/png")])
    PydanticAIInstrumentor().uninstrument()
    manager = f01.manager()
    instrument(manager)
    Agent(FunctionModel(echo)).run_sync(["x", BinaryContent(data=PDF, media_type="image/png")])
    flush(manager)
    assert f01.payloads == []
