"""Raw OpenAI SDK response contracts using synthetic in-memory transport."""

import asyncio
import json

import httpx
import openai
import pytest
from fi_instrumentation import TraceConfig, register
from fi_instrumentation.fi_types import ProjectType
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_openai import OpenAIInstrumentor


@pytest.fixture
def tracing():
    exporter = InMemorySpanExporter()
    provider = register(
        project_name="raw-review", project_type=ProjectType.OBSERVE,
        span_exporter=exporter, batch=False, verbose=False,
    )
    config = TraceConfig(
        hide_inputs=True, hide_input_messages=True,
        hide_llm_invocation_parameters=True, hide_error_details=True,
    )
    OpenAIInstrumentor().instrument(tracer_provider=provider, config=config)
    try:
        yield exporter
    finally:
        OpenAIInstrumentor().uninstrument()
        provider.shutdown()


def body(status="completed"):
    return {
        "id": "resp_review", "object": "response", "created_at": 1,
        "model": "synthetic", "status": status, "parallel_tool_calls": True,
        "tool_choice": "auto", "tools": [], "output": [], "error": None,
        "usage": {
            "input_tokens": 5, "output_tokens": 2, "total_tokens": 7,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


def client(payload, async_):
    class SyncBody(httpx.SyncByteStream):
        def __iter__(self):
            yield payload

    class AsyncBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield payload

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "application/json"},
            stream=AsyncBody() if async_ else SyncBody(),
        )

    http_client = (httpx.AsyncClient if async_ else httpx.Client)(
        transport=httpx.MockTransport(handler), trust_env=False,
    )
    return (openai.AsyncOpenAI if async_ else openai.OpenAI)(
        api_key="synthetic-credential", max_retries=0, http_client=http_client,
    )


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("parse_to", ["default", "dict", "str"])
def test_raw_custom_parse_respects_input_and_error_privacy(tracing, async_, parse_to):
    hidden_input = "synthetic-private-input-24193"
    hidden_error = "synthetic-private-provider-error-88123"
    response = body("failed")
    response.update(
        instructions=hidden_input, metadata={"private": hidden_input},
        input=hidden_input, error={"code": "server_error", "message": hidden_error},
    )
    payload = json.dumps(response).encode()
    kwargs = {} if parse_to == "default" else {
        "to": dict[str, object] if parse_to == "dict" else str,
    }

    async def run_async():
        async with client(payload, True) as c:
            async with c.responses.with_streaming_response.create(
                model="synthetic", input=hidden_input, instructions=hidden_input,
            ) as raw:
                return await raw.parse(**kwargs)

    if async_:
        result = asyncio.run(run_async())
    else:
        with client(payload, False) as c:
            with c.responses.with_streaming_response.create(
                model="synthetic", input=hidden_input, instructions=hidden_input,
            ) as raw:
                result = raw.parse(**kwargs)

    # The application still receives the original unmodified response.
    application_data = (
        result if isinstance(result, str) else json.dumps(result) if isinstance(result, dict)
        else result.model_dump_json()
    )
    assert hidden_error in application_data
    span, = tracing.get_finished_spans()
    assert hidden_input not in span.to_json()
    assert hidden_error not in span.to_json()
    assert span.status.status_code is StatusCode.ERROR


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("status", ["completed", "failed"])
@pytest.mark.parametrize("mode", ["read", "iter_bytes", "iter_text", "iter_lines"])
def test_non_sse_raw_iteration_uses_body_status(tracing, async_, status, mode):
    payload = json.dumps(body(status)).encode()

    async def run_async():
        async with client(payload, True) as c:
            async with c.responses.with_streaming_response.create(
                model="synthetic", input="question",
            ) as raw:
                if mode == "read":
                    assert await raw.read() == payload
                else:
                    chunks = [part async for part in getattr(raw, mode)()]
                    actual = (b"" if mode == "iter_bytes" else "").join(chunks)
                    assert actual == (payload if mode == "iter_bytes" else payload.decode())

    if async_:
        asyncio.run(run_async())
    else:
        with client(payload, False) as c:
            with c.responses.with_streaming_response.create(
                model="synthetic", input="question",
            ) as raw:
                if mode == "read":
                    assert raw.read() == payload
                else:
                    chunks = list(getattr(raw, mode)())
                    actual = (b"" if mode == "iter_bytes" else "").join(chunks)
                    assert actual == (payload if mode == "iter_bytes" else payload.decode())

    span, = tracing.get_finished_spans()
    expected = StatusCode.OK if status == "completed" else StatusCode.ERROR
    assert span.status.status_code is expected


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("instrumented", [False, True])
@pytest.mark.parametrize("endpoint", ["responses", "chat"])
def test_legacy_raw_text_remains_string(tracing, async_, instrumented, endpoint):
    if not instrumented:
        OpenAIInstrumentor().uninstrument()
    response = body()
    if endpoint == "chat":
        response = {
            "id": "chat_review", "object": "chat.completion", "created": 1,
            "model": "synthetic", "choices": [{
                "index": 0, "message": {"role": "assistant", "content": "answer"},
                "finish_reason": "stop",
            }],
        }
    payload = json.dumps(response).encode()

    def params(c):
        if endpoint == "responses":
            return c.responses, {"model": "synthetic", "input": "question"}
        return c.chat.completions, {
            "model": "synthetic", "messages": [{"role": "user", "content": "question"}],
        }

    async def run_async():
        async with client(payload, True) as c:
            resource, kwargs = params(c)
            raw = await resource.with_raw_response.create(**kwargs)
            assert raw.content == payload
            assert json.loads(raw.text) == response

    if async_:
        asyncio.run(run_async())
    else:
        with client(payload, False) as c:
            resource, kwargs = params(c)
            raw = resource.with_raw_response.create(**kwargs)
            assert raw.content == payload
            assert json.loads(raw.text) == response
