"""Error payload privacy through real streaming SDKs and both event handlers.

The OpenAI adapter consumes synthetic SSE bytes through an offline HTTP transport.
No provider calls or real credentials are used.
"""

import asyncio
import json

import httpx
import pytest
from fi_instrumentation import FITracer, TraceConfig
from llama_index.core.llms import ChatMessage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_llamaindex import LlamaIndexInstrumentor
from traceai_llamaindex._handler import _Span


PRIVATE_ERROR = "synthetic-private-stream-error-body"
ANSWER = "synthetic partial answer"
CONTENT_FLAGS = (
    "hide_inputs", "hide_outputs", "hide_input_messages", "hide_output_messages",
    "hide_input_text", "hide_output_text",
)
PRIVACY_SETTINGS = [
    pytest.param({"hide_error_details": True}, True, id="explicit-hide"),
    pytest.param({"hide_inputs": True}, True, id="inferred-from-inputs"),
    pytest.param({}, False, id="default-visible"),
    pytest.param({"hide_inputs": True, "hide_error_details": False}, False,
                 id="explicit-opt-out"),
]


@pytest.fixture
def telemetry(monkeypatch):
    for flag in (*CONTENT_FLAGS, "hide_error_details"):
        monkeypatch.delenv("FI_" + flag.upper(), raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LlamaIndexInstrumentor()
    try:
        yield provider, exporter, instrumentor
    finally:
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
        provider.shutdown()


def assert_error(span, hidden, error_type):
    assert span.status.status_code == StatusCode.ERROR
    errors = [event for event in span.events if event.name == "exception"]
    assert errors
    assert all(event.attributes["exception.type"].endswith(error_type) for event in errors)
    exported = json.dumps({
        "attributes": dict(span.attributes),
        "description": span.status.description,
        "events": [dict(event.attributes or {}) for event in span.events],
    })
    assert (PRIVATE_ERROR not in exported) is hidden
    if hidden:
        assert not span.status.description
        assert all("exception.message" not in event.attributes for event in errors)
        assert all("exception.stacktrace" not in event.attributes for event in errors)


@pytest.fixture
def error_clients():
    state = {"mode": "http-error", "requests": 0}
    chunk = ("data: " + json.dumps({
        "id": "synthetic-stream", "object": "chat.completion.chunk", "created": 1,
        "model": "gpt-4o-mini", "choices": [{"index": 0,
            "delta": {"role": "assistant", "content": ANSWER}, "finish_reason": None}],
    }) + "\n\n").encode()

    class SyncBytes(httpx.SyncByteStream):
        def __iter__(self):
            yield chunk
            raise httpx.ReadError(PRIVATE_ERROR)

    class AsyncBytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield chunk
            raise httpx.ReadError(PRIVATE_ERROR)

    def respond(request, asynchronous=False):
        state["requests"] += 1
        assert json.loads(request.content)["stream"] is True
        if state["mode"] == "http-error":
            return httpx.Response(400, json={"error": {
                "message": PRIVATE_ERROR, "type": "invalid_request_error", "code": "test",
            }})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=AsyncBytes() if asynchronous else SyncBytes())

    with httpx.Client(transport=httpx.MockTransport(respond)) as sync:
        asynchronous = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: respond(request, True)
        ))
        try:
            yield sync, asynchronous, state
        finally:
            asyncio.run(asynchronous.aclose())


def error_model(error_clients):
    OpenAI = pytest.importorskip("llama_index.llms.openai").OpenAI
    sync, asynchronous, _ = error_clients
    return OpenAI(model="gpt-4o-mini", api_key="synthetic-not-a-key", max_retries=0,
                  api_base="https://offline.invalid/v1", http_client=sync,
                  async_http_client=asynchronous)


def assert_stream_error(telemetry, mode, hidden):
    _, exporter, instrumentor = telemetry
    spans = exporter.get_finished_spans()
    assert spans
    assert any(span.name.endswith((".stream_chat", ".astream_chat")) for span in spans)
    for span in spans:
        assert_error(span, hidden, "BadRequestError" if mode == "http-error" else "ReadError")
    assert not instrumentor._span_handler.open_spans
    assert not any(span.active for span in instrumentor._span_handler._export_queue.spans.values())


@pytest.mark.parametrize("settings,hidden", PRIVACY_SETTINGS)
@pytest.mark.parametrize("mode", ["http-error", "read-error"])
def test_real_sync_stream_error_privacy(telemetry, error_clients, settings, hidden, mode):
    openai = pytest.importorskip("openai")
    provider, _, instrumentor = telemetry
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**settings))
    error_clients[2]["mode"] = mode
    stream = error_model(error_clients).stream_chat([ChatMessage(role="user", content="hello")])
    if mode == "read-error":
        assert next(stream).message.content == ANSWER
    error_type = openai.BadRequestError if mode == "http-error" else httpx.ReadError
    with pytest.raises(error_type, match=PRIVATE_ERROR):
        list(stream)
    assert error_clients[2]["requests"] == 1
    assert_stream_error(telemetry, mode, hidden)


@pytest.mark.asyncio
@pytest.mark.parametrize("settings,hidden", PRIVACY_SETTINGS)
@pytest.mark.parametrize("mode", ["http-error", "read-error"])
async def test_real_async_stream_error_privacy(telemetry, error_clients, settings, hidden, mode):
    openai = pytest.importorskip("openai")
    provider, _, instrumentor = telemetry
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**settings))
    error_clients[2]["mode"] = mode
    stream = await error_model(error_clients).astream_chat(
        [ChatMessage(role="user", content="hello")]
    )
    if mode == "read-error":
        assert (await stream.__anext__()).message.content == ANSWER
    error_type = openai.BadRequestError if mode == "http-error" else httpx.ReadError
    with pytest.raises(error_type, match=PRIVATE_ERROR):
        [chunk async for chunk in stream]
    assert error_clients[2]["requests"] == 1
    assert_stream_error(telemetry, mode, hidden)


@pytest.mark.parametrize("settings,hidden", PRIVACY_SETTINGS)
def test_real_chat_engine_error_privacy(telemetry, error_clients, settings, hidden):
    from llama_index.core.chat_engine.types import StreamingAgentChatResponse
    from llama_index.core.memory import ChatMemoryBuffer

    openai = pytest.importorskip("openai")
    provider, exporter, instrumentor = telemetry
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**settings))
    response = StreamingAgentChatResponse(chat_stream=error_model(error_clients).stream_chat(
        [ChatMessage(role="user", content="hello")]
    ))
    # This real SDK method emits StreamChatErrorEvent when its LLM stream fails.
    # Call it directly so the exception is observed, not left on a background thread.
    with pytest.raises(openai.BadRequestError, match=PRIVATE_ERROR):
        response.write_response_to_history(ChatMemoryBuffer.from_defaults())
    assert any(span.name.endswith(".write_response_to_history")
               for span in exporter.get_finished_spans())
    assert_stream_error(telemetry, "http-error", hidden)


@pytest.mark.parametrize("settings,hidden", PRIVACY_SETTINGS + [
    pytest.param({flag: True}, True, id=flag) for flag in CONTENT_FLAGS[1:]
])
@pytest.mark.parametrize("event_name", ["ExceptionEvent", "StreamChatErrorEvent"])
@pytest.mark.parametrize("waiting", [False, True], ids=["active", "streaming"])
def test_error_event_payload_respects_effective_privacy(
    telemetry, settings, hidden, event_name, waiting,
):
    from llama_index.core.instrumentation.events.chat_engine import StreamChatErrorEvent

    if event_name == "ExceptionEvent":
        event_type = pytest.importorskip(
            "llama_index.core.instrumentation.events.exception"
        ).ExceptionEvent
    else:
        event_type = StreamChatErrorEvent
    provider, exporter, _ = telemetry
    config = TraceConfig(**settings)
    tracer = FITracer(provider.get_tracer(__name__), config=config)
    span = _Span(id_="synthetic-span", otel_span=tracer.start_span("error-event"), config=config)
    span._waiting_for_streaming = waiting
    span["output.value"] = ANSWER
    error = ValueError(PRIVATE_ERROR)
    span.process_event(event_type(exception=error))
    span.finish(error)
    result, = exporter.get_finished_spans()
    assert_error(result, hidden, "ValueError")
    # Error-detail hiding alone must not suppress ordinary, non-error output.
    if settings == {"hide_error_details": True}:
        assert result.attributes["output.value"] == ANSWER
    # Opting out of error hiding keeps the original error payload behavior.
    if not hidden:
        assert PRIVATE_ERROR in result.attributes["output.value"]
