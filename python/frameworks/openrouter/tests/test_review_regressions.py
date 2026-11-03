"""Privacy, in-band failures, and nested request context using the real SDK."""
import asyncio
import json

import httpx
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_openrouter import OpenRouterInstrumentor
from test_instrumentation import chat_body, sdk


@pytest.fixture
def instrument():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    inst = OpenRouterInstrumentor()
    def start(config=None):
        inst.instrument(tracer_provider=provider, config=config or TraceConfig())
        return provider, exporter
    yield start
    inst.uninstrument()
    provider.shutdown()


@pytest.mark.parametrize("flag,content", [
    ("hide_input_messages", "SYNTHETIC_PRIVATE_MESSAGE"),
    ("hide_input_text", "SYNTHETIC_PRIVATE_MESSAGE"),
    ("hide_input_images", "data:image/png;base64,SYNTHETIC_IMAGE_MARKER"),
])
def test_chat_input_privacy(instrument, flag, content):
    _, exporter = instrument(TraceConfig(**{flag: True}))
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "SYNTHETIC_PRIVATE_MESSAGE"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,SYNTHETIC_IMAGE_MARKER"}},
    ]}]
    sdk(lambda req: httpx.Response(200, json=chat_body())).chat.send(model="vendor/request", messages=messages)
    attrs = dict(exporter.get_finished_spans()[0].attributes)
    assert content not in str(attrs)
    assert "input.value" not in attrs


@pytest.mark.parametrize("flag", ["hide_output_messages", "hide_output_text"])
def test_chat_output_privacy(instrument, flag):
    _, exporter = instrument(TraceConfig(**{flag: True}))
    sdk(lambda req: httpx.Response(200, json=chat_body())).chat.send(model="vendor/request", messages=[{"role": "user", "content": "hi"}])
    attrs = dict(exporter.get_finished_spans()[0].attributes)
    assert "Tokyo" not in str(attrs)
    assert "output.value" not in attrs


def chat_error_stream():
    chunks = [
        {"id": "chat-1", "object": "chat.completion.chunk", "created": 1,
         "model": "vendor/served", "choices": [{"index": 0, "delta": {"content": "Tok"}, "finish_reason": None}]},
        {"id": "chat-1", "object": "chat.completion.chunk", "created": 1,
         "model": "vendor/served", "choices": [], "error": {"code": 502, "message": "SYNTHETIC_PRIVATE_ERROR"}},
    ]
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content=("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode())


@pytest.mark.parametrize("async_call", [False, True])
def test_in_band_stream_error_drops_partial_answer(instrument, async_call):
    _, exporter = instrument()
    client = sdk(lambda req: chat_error_stream())
    if async_call:
        async def run():
            stream = await client.chat.send_async(model="vendor/request", messages=[{"role": "user", "content": "hi"}], stream=True)
            return [part async for part in stream]
        chunks = asyncio.run(run())
    else:
        chunks = list(client.chat.send(model="vendor/request", messages=[{"role": "user", "content": "hi"}], stream=True))
    assert len(chunks) == 2
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert "output.value" not in span.attributes
    assert "SYNTHETIC_PRIVATE_ERROR" not in str(dict(span.attributes))


@pytest.mark.parametrize("state", ["failed", "incomplete"])
def test_responses_failure_drops_partial_answer(instrument, state):
    _, exporter = instrument()
    body = {"id": "resp_1", "object": "response", "created_at": 1, "completed_at": 2,
            "error": {"code": "server_error", "message": "SYNTHETIC_PRIVATE_ERROR"} if state == "failed" else None,
            "frequency_penalty": None, "incomplete_details": None, "instructions": None,
            "metadata": None, "model": "vendor/served", "output": [], "output_text": "partial",
            "parallel_tool_calls": False, "presence_penalty": None, "status": state,
            "temperature": None, "tool_choice": "auto", "tools": [], "top_p": None,
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}
    result = sdk(lambda req: httpx.Response(200, json=body)).responses.send(model="vendor/request", input="hi")
    assert result.status == state
    span = exporter.get_finished_spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert "output.value" not in span.attributes
    assert "SYNTHETIC_PRIVATE_ERROR" not in str(dict(span.attributes))


@pytest.mark.parametrize("async_call", [False, True])
def test_http_child_nested_under_llm(instrument, async_call):
    provider, exporter = instrument()
    tracer = provider.get_tracer("test")
    def handler(req):
        with tracer.start_as_current_span("http.client"):
            return httpx.Response(200, json=chat_body())
    client = sdk(handler)
    with tracer.start_as_current_span("caller"):
        if async_call:
            asyncio.run(client.chat.send_async(model="vendor/request", messages=[{"role": "user", "content": "hi"}]))
        else:
            client.chat.send(model="vendor/request", messages=[{"role": "user", "content": "hi"}])
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["openrouter.chat"].parent.span_id == spans["caller"].context.span_id
    assert spans["http.client"].parent.span_id == spans["openrouter.chat"].context.span_id


def test_multimodal_input_never_serializes_image_by_default(instrument):
    _, exporter = instrument()
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,SYNTHETIC_IMAGE_MARKER"}},
    ]}]
    sdk(lambda req: httpx.Response(200, json=chat_body())).chat.send(model="vendor/request", messages=messages)
    assert "input.value" not in exporter.get_finished_spans()[0].attributes


@pytest.mark.parametrize("async_call", [False, True])
def test_stream_http_child_nested_under_llm(instrument, async_call):
    provider, exporter = instrument()
    tracer = provider.get_tracer("test")
    chunk = {"id": "chat-1", "object": "chat.completion.chunk", "created": 1,
             "model": "vendor/served", "choices": [{"index": 0, "delta": {"content": "Tokyo"}, "finish_reason": None}]}
    payload = "data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n"
    def handler(req):
        with tracer.start_as_current_span("http.client"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=payload.encode())
    client = sdk(handler)
    with tracer.start_as_current_span("caller"):
        if async_call:
            async def run():
                stream = await client.chat.send_async(model="vendor/request", messages=[{"role": "user", "content": "hi"}], stream=True)
                return [part async for part in stream]
            asyncio.run(run())
        else:
            list(client.chat.send(model="vendor/request", messages=[{"role": "user", "content": "hi"}], stream=True))
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["http.client"].parent.span_id == spans["openrouter.chat"].context.span_id
    assert spans["openrouter.chat"].parent.span_id == spans["caller"].context.span_id
