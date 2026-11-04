"""Responses privacy and raw-envelope contracts using synthetic SDK transports."""

import asyncio
import inspect
import json

import httpx
import openai
import pytest
from fi_instrumentation import TraceConfig, register
from fi_instrumentation.fi_types import ProjectType
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_openai import OpenAIInstrumentor

KEY = "synthetic-review-key-not-for-export"
ERROR = "synthetic-private-tool-error"


@pytest.fixture
def tracing():
    exporter = InMemorySpanExporter()
    provider = register(
        project_name="independent-review",
        project_type=ProjectType.OBSERVE,
        span_exporter=exporter,
        batch=False,
        verbose=False,
    )
    try:
        yield provider, exporter
    finally:
        OpenAIInstrumentor().uninstrument()
        provider.force_flush()
        provider.shutdown()


def client(handler, async_=False, key=KEY):
    http = (httpx.AsyncClient if async_ else httpx.Client)(
        transport=httpx.MockTransport(handler), trust_env=False
    )
    return (openai.AsyncOpenAI if async_ else openai.OpenAI)(
        api_key=key,
        base_url="https://api.parallel.ai/v1",
        max_retries=0,
        http_client=http,
    )


def body(status="completed"):
    return {
        "id": "resp_review",
        "object": "response",
        "created_at": 1,
        "model": "parallel",
        "status": status,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "output": [],
        "error": None,
        "usage": {
            "input_tokens": 5,
            "output_tokens": 2,
            "total_tokens": 7,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


def sse(kind, response):
    return (
        "event: "
        + kind
        + "\ndata: "
        + json.dumps({"type": kind, "sequence_number": 1, "response": response})
        + "\n\n"
    ).encode()


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("traceback_marker", [False, True])
def test_key_redaction_survives_core_status_cleaning(tracing, async_, traceback_marker):
    provider, exporter = tracing
    OpenAIInstrumentor().instrument(tracer_provider=provider)
    message = "Invalid credential: " + KEY
    if traceback_marker:
        message += "\nTraceback (most recent call last):\n  synthetic remote failure"

    def handler(request):
        assert request.headers["authorization"] == "Bearer " + KEY
        return httpx.Response(
            401, json={"error": {"code": "invalid_api_key", "message": message}}
        )

    async def run():
        async with client(handler, True) as c:
            await c.responses.create(model="parallel", input="synthetic question")

    with pytest.raises(openai.AuthenticationError) as caught:
        if async_:
            asyncio.run(run())
        else:
            with client(handler) as c:
                c.responses.create(model="parallel", input="synthetic question")
    (span,) = exporter.get_finished_spans()
    assert KEY in str(caught.value)
    assert KEY not in span.to_json(), span.to_json()


@pytest.mark.parametrize("async_", [False, True])
def test_callable_credentials_are_scrubbed_without_extra_provider_calls(
    tracing, async_
):
    if tuple(map(int, openai.__version__.split(".")[:2])) < (3, 0):
        pytest.skip("callable OpenAI credentials are a latest-SDK feature")
    provider, exporter = tracing
    OpenAIInstrumentor().instrument(tracer_provider=provider)
    calls = []

    def token():
        calls.append(1)
        return KEY

    async def atoken():
        return token()

    def handler(request):
        assert request.headers["authorization"] == "Bearer " + KEY
        return httpx.Response(
            401,
            json={
                "error": {"code": "invalid_api_key", "message": "Invalid key: " + KEY}
            },
        )

    async def run():
        async with client(handler, True, atoken) as c:
            await c.responses.create(model="parallel", input="synthetic question")

    with pytest.raises(openai.AuthenticationError) as caught:
        if async_:
            asyncio.run(run())
        else:
            with client(handler, key=token) as c:
                c.responses.create(model="parallel", input="synthetic question")
    (span,) = exporter.get_finished_spans()
    assert len(calls) == 1
    assert KEY in str(caught.value)
    assert caught.value.request.headers["authorization"] == "Bearer " + KEY
    assert KEY not in span.to_json(), span.to_json()


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize(
    "kind",
    ["http_error", "mcp_protocol_error", "mcp_tool_execution_error", "mcp_list_tools"],
)
@pytest.mark.skipif(
    "hide_error_details" not in inspect.signature(TraceConfig).parameters,
    reason="Error-detail masking requires a core with hide_error_details support",
)
def test_hidden_error_details_cover_mcp_failure_output(
    tracing, async_, stream, hidden, kind
):
    provider, exporter = tracing
    OpenAIInstrumentor().instrument(
        tracer_provider=provider, config=TraceConfig(hide_error_details=hidden)
    )
    response = body()
    response["output"] = [
        {
            "id": "mcp_review",
            "type": "mcp_call",
            "name": "lookup",
            "arguments": "{}",
            "server_label": "test",
            "error": {"type": "http_error", "code": 500, "message": ERROR},
            "output": "ordinary tool result",
        }
    ]
    if kind == "mcp_list_tools":
        response["output"] = [
            {
                "id": "mcp_list",
                "type": kind,
                "server_label": "test",
                "tools": [],
                "error": ERROR,
            }
        ]
    elif kind == "mcp_tool_execution_error":
        response["output"][0]["error"] = {"type": kind, "content": {"text": ERROR}}
    else:
        response["output"][0]["error"]["type"] = kind

    def handler(request):
        if stream:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse("response.completed", response),
            )
        return httpx.Response(200, json=response)

    async def run():
        async with client(handler, True) as c:
            result = await c.responses.create(
                model="parallel", input="question", stream=stream
            )
            if stream:
                events = [event async for event in result]
                assert ERROR in events[-1].response.model_dump_json()
            else:
                assert ERROR in result.model_dump_json()

    if async_:
        asyncio.run(run())
    else:
        with client(handler) as c:
            result = c.responses.create(
                model="parallel", input="question", stream=stream
            )
            if stream:
                assert ERROR in list(result)[-1].response.model_dump_json()
            else:
                assert ERROR in result.model_dump_json()
    (span,) = exporter.get_finished_spans()
    assert (ERROR in span.to_json()) is (not hidden), span.to_json()
    output = json.loads(span.attributes["output.value"])["output"][0]
    if kind != "mcp_list_tools":
        assert output["output"] == "ordinary tool result"
        assert output["error"]["type"] == kind
        if kind != "mcp_tool_execution_error":
            assert output["error"]["code"] == 500
    elif hidden:
        assert "error" not in output


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("status", ["failed", "incomplete", "completed", "in_progress"])
@pytest.mark.parametrize("close_http", [False, True])
def test_raw_stream_lifecycle_retains_sdk_contract(tracing, async_, status, close_http):
    provider, exporter = tracing
    OpenAIInstrumentor().instrument(tracer_provider=provider)
    response = body(status)
    if status == "failed":
        response["error"] = {"code": "server_error", "message": "synthetic failure"}
    kind = "response.created" if status == "in_progress" else "response." + status

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(kind, response),
        )

    async def run():
        async with client(handler, True) as c:
            raw = await c.responses.with_raw_response.create(
                model="parallel", input="question", stream=True
            )
            assert raw.status_code == 200
            assert raw.headers["content-type"] == "text/event-stream"
            assert isinstance(raw, openai._legacy_response.LegacyAPIResponse)
            assert not exporter.get_finished_spans()
            stream = raw.parse()
            assert raw.parse() is stream
            assert raw.parse(to=openai.types.responses.Response) is stream
            assert (await stream.__anext__()).response.status == status
            if close_http:
                await raw.http_response.aclose()
            else:
                await stream.close()
            await stream.close()
            assert raw.http_response.is_closed

    if async_:
        asyncio.run(run())
    else:
        with client(handler) as c:
            raw = c.responses.with_raw_response.create(
                model="parallel", input="question", stream=True
            )
            assert raw.status_code == 200
            assert raw.headers["content-type"] == "text/event-stream"
            assert isinstance(raw, openai._legacy_response.LegacyAPIResponse)
            assert not exporter.get_finished_spans()
            stream = raw.parse()
            assert raw.parse() is stream
            assert raw.parse(to=openai.types.responses.Response) is stream
            assert next(stream).response.status == status
            if close_http:
                raw.http_response.close()
            else:
                stream.close()
            stream.close()
            assert raw.http_response.is_closed
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is (
        StatusCode.OK if status == "completed" else StatusCode.ERROR
    )
    if status != "in_progress":
        assert span.attributes["gen_ai.usage.input_tokens"] == 5
        assert span.attributes["gen_ai.usage.output_tokens"] == 2
        assert span.attributes["gen_ai.usage.total_tokens"] == 7


def test_concurrent_callable_credentials_use_each_failed_request(tracing):
    if tuple(map(int, openai.__version__.split(".")[:2])) < (3, 0):
        pytest.skip("callable OpenAI credentials are a latest-SDK feature")
    provider, exporter = tracing
    OpenAIInstrumentor().instrument(tracer_provider=provider)
    keys = [KEY + "-first", KEY + "-second"]
    issued = []

    async def run():
        both_sent = asyncio.Event()
        received = []

        async def token():
            value = keys[len(issued)]
            issued.append(value)
            return value

        async def handler(request):
            credential = request.headers["authorization"][7:]
            received.append(credential)
            if len(received) == 2:
                both_sent.set()
            await asyncio.wait_for(both_sent.wait(), 3)
            return httpx.Response(
                401,
                json={
                    "error": {
                        "code": "invalid_api_key",
                        "message": "Invalid key: " + credential,
                    }
                },
            )

        async with client(handler, True, token) as c:
            results = await asyncio.gather(
                *[c.responses.create(model="parallel", input="question") for _ in keys],
                return_exceptions=True,
            )
        assert set(received) == set(keys)
        assert all(isinstance(result, openai.AuthenticationError) for result in results)
        for result in results:
            assert result.request.headers["authorization"][7:] in str(result)

    asyncio.run(run())
    assert issued == keys
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    for span in spans:
        assert span.status.status_code is StatusCode.ERROR
        assert all(key not in span.to_json() for key in keys)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("raw", [False, True])
def test_callable_credentials_survive_stream_creation(tracing, async_, raw):
    if tuple(map(int, openai.__version__.split(".")[:2])) < (3, 0):
        pytest.skip("callable OpenAI credentials are a latest-SDK feature")
    provider, exporter = tracing
    OpenAIInstrumentor().instrument(tracer_provider=provider)
    calls = []

    def token():
        calls.append(1)
        return KEY

    async def atoken():
        return token()

    response = body("failed")
    response["error"] = {"code": "server_error", "message": "Rejected " + KEY}

    def handler(request):
        assert request.headers["authorization"] == "Bearer " + KEY
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse("response.failed", response),
        )

    async def run():
        async with client(handler, True, atoken) as c:
            api = c.responses.with_raw_response if raw else c.responses
            result = await api.create(model="parallel", input="question", stream=True)
            stream = result.parse() if raw else result
            assert KEY in (await stream.__anext__()).response.error.message
            await stream.close()

    if async_:
        asyncio.run(run())
    else:
        with client(handler, key=token) as c:
            api = c.responses.with_raw_response if raw else c.responses
            result = api.create(model="parallel", input="question", stream=True)
            stream = result.parse() if raw else result
            assert KEY in next(stream).response.error.message
            stream.close()
    assert len(calls) == 1
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert KEY not in span.to_json()
