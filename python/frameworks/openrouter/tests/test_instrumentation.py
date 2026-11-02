"""Real OpenRouter SDK calls with synthetic responses and no external traffic."""
import asyncio
import json

import httpx
import pytest
from openrouter import OpenRouter
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_openrouter import OpenRouterInstrumentor


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = OpenRouterInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    yield exporter
    instrumentor.uninstrument()
    provider.shutdown()


def sdk(handler):
    transport = httpx.MockTransport(handler)
    return OpenRouter(api_key="synthetic-key", client=httpx.Client(transport=transport),
                      async_client=httpx.AsyncClient(transport=transport))


def chat_body():
    return {"id": "chat-1", "object": "chat.completion", "created": 1,
            "model": "vendor/served", "system_fingerprint": None, "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "Tokyo"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}


def attributes(spans):
    finished = spans.get_finished_spans()
    assert len(finished) == 1
    return dict(finished[0].attributes)


def test_chat_sync_records_model_usage_content_without_secret(spans):
    seen = []
    def handle(req):
        seen.append(req)
        return httpx.Response(200, json=chat_body())
    client = sdk(handle)
    result = client.chat.send(model="vendor/request", messages=[{"role": "user", "content": "Japan?"}])
    assert result.choices[0].message.content == "Tokyo"
    a = attributes(spans)
    assert len(seen) == 1 and seen[0].url.host == "openrouter.ai"
    assert a["gen_ai.provider.name"] == "openrouter"
    assert a["gen_ai.request.model"] == "vendor/request"
    assert a["gen_ai.response.model"] == "vendor/served"
    assert a["gen_ai.usage.input_tokens"] == 3
    assert a["gen_ai.usage.output_tokens"] == 2
    assert a["output.value"] == "Tokyo"
    assert "synthetic-key" not in str(a)


def test_chat_async(spans):
    client = sdk(lambda req: httpx.Response(200, json=chat_body()))
    result = asyncio.run(client.chat.send_async(model="vendor/request", messages=[{"role": "user", "content": "Japan?"}]))
    assert result.choices[0].message.content == "Tokyo"
    assert attributes(spans)["gen_ai.usage.input_tokens"] == 3


def test_error_does_not_export_response_or_key(spans):
    client = sdk(lambda req: httpx.Response(401, json={"error": {"message": "synthetic-key is invalid"}}))
    with pytest.raises(Exception):
        client.chat.send(model="vendor/request", messages=[{"role": "user", "content": "Japan?"}])
    a = attributes(spans)
    assert a["error.type"]
    assert "synthetic-key" not in str(a)


def test_chat_stream_finishes_after_consumption_with_final_usage(spans):
    chunks = [{"id": "chat-1", "object": "chat.completion.chunk", "created": 1,
               "model": "vendor/served", "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
              for text in ("Tok", "yo")]
    chunks.append({"id": "chat-1", "object": "chat.completion.chunk", "created": 1,
                   "model": "vendor/served", "choices": [],
                   "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})
    body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    client = sdk(lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode()))
    stream = client.chat.send(model="vendor/request", messages=[{"role": "user", "content": "Japan?"}], stream=True)
    assert not spans.get_finished_spans()
    assert list(stream)
    a = attributes(spans)
    assert a["output.value"] == "Tokyo"
    assert a["gen_ai.usage.input_tokens"] == 3


def test_embedding_has_usage_and_no_vector_content(spans):
    client = sdk(lambda req: httpx.Response(200, json={
        "object": "list", "model": "vendor/embed", "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
        "usage": {"prompt_tokens": 4, "total_tokens": 4}}))
    answer = client.embeddings.generate(model="vendor/embed", input="secret input")
    assert len(answer.data[0].embedding) == 2
    a = attributes(spans)
    assert a["gen_ai.operation.name"] == "embeddings"
    assert a["gen_ai.usage.input_tokens"] == 4
    assert "input.value" not in a and "output.value" not in a


def test_responses_and_beta_alias(spans):
    body = {"id": "resp_1", "object": "response", "created_at": 1, "completed_at": 2,
            "error": None, "frequency_penalty": None, "incomplete_details": None,
            "instructions": None, "metadata": None, "model": "vendor/served",
            "output": [], "output_text": "Tokyo", "parallel_tool_calls": False,
            "presence_penalty": None, "status": "completed", "temperature": None,
            "tool_choice": "auto", "tools": [], "top_p": None,
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5,
                      "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 0}}}
    client = sdk(lambda req: httpx.Response(200, json=body))
    assert client.responses.send(model="vendor/request", input="Japan?").output_text == "Tokyo"
    assert client.beta.responses.send(model="vendor/request", input="Japan?").output_text == "Tokyo"
    finished = spans.get_finished_spans()
    assert len(finished) == 2
    assert all(s.attributes["gen_ai.provider.name"] == "openrouter" for s in finished)
    assert all(s.attributes.get("gen_ai.usage.output_tokens") == 2 for s in finished), [dict(s.attributes) for s in finished]


def test_stream_early_close_marks_incomplete_without_claiming_final_text(spans):
    chunks = [{"id": "chat-1", "object": "chat.completion.chunk", "created": 1,
               "model": "vendor/served", "choices": [{"index": 0, "delta": {"content": "Tok"}, "finish_reason": None}]}]
    body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    client = sdk(lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode()))
    with client.chat.send(model="vendor/request", messages=[{"role": "user", "content": "Japan?"}], stream=True) as stream:
        next(stream)
    a = attributes(spans)
    assert a["openrouter.stream.incomplete"] is True
    assert "output.value" not in a


def test_chat_async_stream_usage(spans):
    chunks = [{"id": "chat-1", "object": "chat.completion.chunk", "created": 1,
               "model": "vendor/served", "choices": [{"index": 0, "delta": {"content": "Tokyo"}, "finish_reason": None}]},
              {"id": "chat-1", "object": "chat.completion.chunk", "created": 1,
               "model": "vendor/served", "choices": [],
               "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}]
    body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    client = sdk(lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode()))
    async def run():
        stream = await client.chat.send_async(model="vendor/request", messages=[{"role": "user", "content": "Japan?"}], stream=True)
        return [chunk async for chunk in stream]
    assert len(asyncio.run(run())) == 2
    a = attributes(spans)
    assert a["output.value"] == "Tokyo"
    assert a["gen_ai.usage.input_tokens"] == 3


def test_chat_async_close_ends_incomplete_span(spans):
    chunk = {"id": "chat-1", "object": "chat.completion.chunk", "created": 1,
             "model": "vendor/served", "choices": [{"index": 0, "delta": {"content": "Tok"}, "finish_reason": None}]}
    body = "data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n"
    client = sdk(lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode()))
    async def run():
        stream = await client.chat.send_async(model="vendor/request", messages=[{"role": "user", "content": "Japan?"}], stream=True)
        await stream.__anext__()
        await stream.close()
    asyncio.run(run())
    assert attributes(spans)["openrouter.stream.incomplete"] is True


def test_hide_inputs_outputs(spans):
    OpenRouterInstrumentor().uninstrument()
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = OpenRouterInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(hide_inputs=True, hide_outputs=True))
    try:
        client = sdk(lambda req: httpx.Response(200, json=chat_body()))
        client.chat.send(model="vendor/request", messages=[{"role": "user", "content": "secret prompt"}])
        a = attributes(exporter)
        assert "input.value" not in a and "output.value" not in a
        assert a["gen_ai.usage.input_tokens"] == 3
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def test_uninstrument_restores_methods(spans):
    instrumentor = OpenRouterInstrumentor()
    instrumentor.uninstrument()
    client = sdk(lambda req: httpx.Response(200, json=chat_body()))
    client.chat.send(model="vendor/request", messages=[{"role": "user", "content": "Japan?"}])
    assert spans.get_finished_spans() == ()
