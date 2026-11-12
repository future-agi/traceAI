"""Public SDK contracts with real models and only the HTTP transport replaced."""

import asyncio
import json
from contextlib import contextmanager

import cohere
import httpx
import pytest
from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import EmbeddingAttributes, SpanAttributes
from fi_instrumentation.instrumentation.config import REDACTED_VALUE, suppress_tracing
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_cohere import CohereInstrumentor


PROMPT = "private synthetic input"
REPLY = "private synthetic output"
KEY = "synthetic-not-a-credential"
FINAL = {
    "text": REPLY,
    "generation_id": "synthetic-generation",
    "finish_reason": "COMPLETE",
    "meta": {"billed_units": {"input_tokens": 2, "output_tokens": 3}},
    "tool_calls": [{"name": "synthetic_tool", "parameters": {"text": REPLY}}],
}
EVENTS = [
    {"event_type": "text-generation", "text": REPLY, "is_finished": False},
    {"event_type": "stream-end", "is_finished": True,
     "finish_reason": "COMPLETE", "response": FINAL},
]
VECTOR_KEY = (
    f"{SpanAttributes.EMBEDDING_EMBEDDINGS}.0.{EmbeddingAttributes.EMBEDDING_VECTOR}"
)


def encode(event):
    return (json.dumps(event) + "\n").encode()


@contextmanager
def capture(config=None, enabled=True):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = CohereInstrumentor()
    if enabled:
        instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        yield exporter, provider
    finally:
        if enabled:
            instrumentor.uninstrument()
        provider.shutdown()


class EventBody(httpx.AsyncByteStream):
    def __init__(self, mode="complete"):
        self.mode = mode
        self.closed = False
        self.waiting = asyncio.Event()
        self.error = httpx.ReadError("synthetic stream failure")

    async def __aiter__(self):
        if self.mode == "empty":
            return
        yield encode(EVENTS[0])
        if self.mode == "error":
            raise self.error
        if self.mode == "cancel":
            self.waiting.set()
            await asyncio.Event().wait()
        yield encode(EVENTS[1])

    async def aclose(self):
        self.closed = True


async def consume_async_stream(mode, enabled, config=None):
    body = EventBody(mode)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, headers={"content-type": "application/x-ndjson"}, stream=body
        )

    with capture(config, enabled) as (exporter, provider):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = cohere.AsyncClient(api_key=KEY, httpx_client=http)
            with provider.get_tracer(__name__).start_as_current_span("parent") as parent:
                stream = client.chat_stream(model="command-r", message=PROMPT)
                assert hasattr(stream, "__aiter__")
                assert requests == []  # The public SDK does not read ahead.
                if mode == "empty":
                    assert [event async for event in stream] == []
                else:
                    first = await stream.__anext__()
                    assert first.event_type == "text-generation"
                    assert first.text == REPLY
                    assert trace.get_current_span() is parent
                    if mode == "close":
                        await stream.aclose()
                    elif mode == "error":
                        with pytest.raises(httpx.ReadError) as caught:
                            await stream.__anext__()
                        assert caught.value is body.error
                    elif mode == "cancel":
                        task = asyncio.create_task(stream.__anext__())
                        await asyncio.wait_for(body.waiting.wait(), timeout=2)
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
                    else:
                        remaining = [event async for event in stream]
                        assert len(remaining) == 1
                        assert remaining[0].event_type == "stream-end"
                        assert remaining[0].response.text == REPLY
                await stream.aclose()  # Repeated close must not export twice.
                assert body.closed
                assert trace.get_current_span() is parent
                parent_id = parent.get_span_context().span_id
            provider.force_flush()
        assert len(requests) == 1
        assert requests[0]["message"] == PROMPT
        spans = [s for s in exporter.get_finished_spans() if s.name != "parent"]
        return spans, parent_id


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["complete", "empty", "close", "error", "cancel"])
async def test_async_stream_regression(mode):
    spans, parent_id = await consume_async_stream(mode, enabled=True)
    assert len(spans) == 1, "AsyncClient.chat_stream must export one span"
    span = spans[0]
    assert span.name == "cohere.chat_stream"
    assert span.parent.span_id == parent_id
    assert span.attributes[SpanAttributes.GEN_AI_REQUEST_MODEL] == "command-r"
    if mode in ("complete", "empty"):
        assert span.status.status_code == trace.StatusCode.OK
    if mode == "complete":
        assert span.attributes[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] == 2
        assert span.attributes[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] == 3
        assert json.loads(span.attributes[SpanAttributes.OUTPUT_VALUE])["text"] == REPLY
    if mode == "error":
        assert span.status.status_code == trace.StatusCode.ERROR
        assert len(span.events) == 1
    assert KEY not in json.dumps(dict(span.attributes))


@pytest.mark.asyncio
async def test_async_stream_privacy_regression():
    spans, _ = await consume_async_stream(
        "complete", enabled=True, config=TraceConfig(hide_inputs=True, hide_outputs=True)
    )
    assert len(spans) == 1
    attributes = dict(spans[0].attributes)
    assert attributes[SpanAttributes.INPUT_VALUE] == REDACTED_VALUE
    assert attributes[SpanAttributes.OUTPUT_VALUE] == REDACTED_VALUE
    assert not any(k.startswith(SpanAttributes.GEN_AI_INPUT_MESSAGES) for k in attributes)
    assert not any(k.startswith(SpanAttributes.GEN_AI_OUTPUT_MESSAGES) for k in attributes)
    for value in (PROMPT, REPLY, KEY):
        assert value not in json.dumps(attributes)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["complete", "empty", "close", "error", "cancel"])
async def test_uninstrumented_async_stream_control(mode):
    spans, _ = await consume_async_stream(mode, enabled=False)
    assert spans == []


@pytest.mark.asyncio
async def test_suppressed_async_stream_control():
    with suppress_tracing():
        spans, _ = await consume_async_stream("complete", enabled=True)
    assert spans == []


@pytest.mark.asyncio
async def test_async_stream_instrument_uninstrument_regression():
    original = cohere.AsyncClient.chat_stream
    for _ in range(2):
        spans, _ = await consume_async_stream("complete", enabled=True)
        assert cohere.AsyncClient.chat_stream is original
        assert len(spans) == 1
        spans, _ = await consume_async_stream("complete", enabled=False)
        assert spans == []


@pytest.mark.asyncio
async def test_existing_chat_paths_control():
    requests = []

    def respond(request):
        request_body = json.loads(request.content)
        requests.append(request_body)
        if request_body.get("stream"):
            return httpx.Response(
                200, headers={"content-type": "application/x-ndjson"},
                content=b"".join(encode(event) for event in EVENTS),
            )
        return httpx.Response(200, json=FINAL)

    with capture() as (exporter, provider):
        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            client = cohere.Client(api_key=KEY, httpx_client=http)
            events = list(client.chat_stream(model="command-r", message=PROMPT))
            assert len(events) == 2
            assert events[0].text == REPLY
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = cohere.AsyncClient(api_key=KEY, httpx_client=http)
            response = await client.chat(model="command-r", message=PROMPT)
            assert response.text == REPLY
        provider.force_flush()
        assert [s.name for s in exporter.get_finished_spans()] == [
            "cohere.chat_stream", "cohere.chat"
        ]
        assert len(requests) == 2


async def embed_call(asynchronous, typed, vectors, enabled=True, config=None):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "synthetic-embedding",
            "texts": [PROMPT] * len(vectors),
            "response_type": "embeddings_by_type" if typed else "embeddings_floats",
            "embeddings": {"float": vectors} if typed else vectors,
            "meta": {"billed_units": {"input_tokens": 2}},
        })

    kwargs = dict(texts=[PROMPT] * max(1, len(vectors)), model="embed-english-v3.0",
                  input_type="search_document")
    if typed:
        kwargs["embedding_types"] = ["float"]
    with capture(config, enabled) as (exporter, provider):
        if asynchronous:
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                client = cohere.AsyncClient(api_key=KEY, httpx_client=http)
                response = await client.embed(**kwargs)
        else:
            with httpx.Client(transport=httpx.MockTransport(respond)) as http:
                client = cohere.Client(api_key=KEY, httpx_client=http)
                response = client.embed(**kwargs)
        data = response.model_dump() if hasattr(response, "model_dump") else response.dict()
        actual = data["embeddings"]
        if typed:
            actual = actual.get("float", actual.get("float_"))
        assert actual == vectors
        assert len(requests) == 1
        assert requests[0]["texts"] == kwargs["texts"]
        provider.force_flush()
        return exporter.get_finished_spans()


def assert_embeddings(spans, vectors, caplog, hidden=False):
    assert len(spans) == 1
    assert not [r for r in caplog.records if r.name.startswith("traceai_cohere") and r.exc_info]
    span = spans[0]
    assert span.name == "cohere.embed"
    attributes = dict(span.attributes)
    assert span.status.status_code == trace.StatusCode.OK
    assert attributes[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] == 2
    if vectors:
        assert attributes["cohere.embeddings_count"] == len(vectors)
        assert attributes["cohere.embedding_dimensions"] == len(vectors[0])
    if hidden:
        assert VECTOR_KEY not in attributes
        assert attributes[SpanAttributes.INPUT_VALUE] == REDACTED_VALUE
        assert attributes[SpanAttributes.OUTPUT_VALUE] == REDACTED_VALUE
        assert PROMPT not in json.dumps(attributes)
    else:
        if vectors:
            assert json.loads(attributes[VECTOR_KEY]) == vectors[0][:10]
        else:
            assert VECTOR_KEY not in attributes
        assert json.loads(attributes[SpanAttributes.OUTPUT_VALUE]) == {"embeddings_count": len(vectors)}
    assert KEY not in json.dumps(attributes)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("vectors", [[], [[0.25, 0.5, 0.75]], [[0.25, 0.5, 0.75]] * 2])
async def test_typed_embeddings_regression(asynchronous, vectors, caplog):
    spans = await embed_call(asynchronous, True, vectors)
    assert_embeddings(spans, vectors, caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_typed_embeddings_privacy_regression(asynchronous, caplog):
    vectors = [[0.25, 0.5, 0.75]]
    spans = await embed_call(asynchronous, True, vectors, config=TraceConfig(
        hide_inputs=True, hide_outputs=True, hide_embedding_vectors=True
    ))
    assert_embeddings(spans, vectors, caplog, hidden=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
async def test_legacy_embeddings_control(asynchronous, hidden, caplog):
    vectors = [[0.25, 0.5, 0.75]]
    config = TraceConfig(hide_inputs=hidden, hide_outputs=hidden, hide_embedding_vectors=hidden)
    spans = await embed_call(asynchronous, False, vectors, config=config)
    assert_embeddings(spans, vectors, caplog, hidden)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_uninstrumented_typed_embeddings_control(asynchronous):
    assert not await embed_call(asynchronous, True, [[0.25, 0.5, 0.75]], enabled=False)
