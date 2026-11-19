"""Offline Writer SDK harness: the real SDK client over an httpx MockTransport, no network."""
import json

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from writerai import AsyncWriter, Writer

API_KEY = "synthetic-writer-key-DO-NOT-EXPORT"


def make_client(handler):
    transport = httpx.MockTransport(handler)
    return Writer(
        api_key=API_KEY,
        base_url="http://writer.test",
        max_retries=0,
        http_client=httpx.Client(transport=transport),
    )


def make_async_client(handler):
    transport = httpx.MockTransport(handler)
    return AsyncWriter(
        api_key=API_KEY,
        base_url="http://writer.test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=transport),
    )


def chat_body(text="Tokyo", **message):
    msg = {"role": "assistant", "content": text}
    msg.update(message)
    return {
        "id": "chat-1",
        "object": "chat.completion",
        "created": 1,
        "model": "palmyra-x5-served",
        "choices": [{"index": 0, "finish_reason": "stop", "message": msg}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def completion_body(text="done"):
    return {"id": "cmpl-1", "choices": [{"text": text}], "model": "palmyra-x-003-instruct"}


def sse(*events):
    out = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=out.encode())


def chunk(content=None, finish=None, usage=None, tool_calls=None, model="palmyra-x5-served"):
    delta = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    body = {
        "id": "chat-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def attrs(span):
    return dict(span.attributes)


def blob(span):
    return json.dumps(attrs(span), default=str)


@pytest.fixture
def provider():
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer_provider.exporter = exporter
    yield tracer_provider
    tracer_provider.shutdown()


@pytest.fixture
def instrument(provider):
    from traceai_writer import WriterInstrumentor

    instrumentors = []

    def start(**kwargs):
        instrumentor = WriterInstrumentor()
        instrumentor.instrument(tracer_provider=provider, **kwargs)
        instrumentors.append(instrumentor)
        return provider.exporter

    yield start
    for instrumentor in reversed(instrumentors):
        instrumentor.uninstrument()


@pytest.fixture
def spans(instrument):
    return instrument()
