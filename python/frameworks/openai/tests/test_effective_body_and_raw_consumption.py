"""Effective credentials and lazy body lifecycles through real SDK transports."""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

try:
    import httpx2 as httpx
except ImportError:
    import httpx
import openai
import pytest
from fi_instrumentation import TraceConfig, register
from fi_instrumentation.fi_types import ProjectType
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_openai import OpenAIInstrumentor
from traceai_openai._privacy import request_credentials


@pytest.fixture
def tracing():
    exporter = InMemorySpanExporter()
    provider = register(
        project_name="body-consumption",
        project_type=ProjectType.OBSERVE,
        span_exporter=exporter,
        batch=False,
        verbose=False,
    )
    OpenAIInstrumentor().instrument(tracer_provider=provider, config=TraceConfig())
    try:
        yield provider, exporter
    finally:
        OpenAIInstrumentor().uninstrument()
        provider.force_flush()
        provider.shutdown()


def sdk(handler, async_=False):
    http = (httpx.AsyncClient if async_ else httpx.Client)(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )
    return (openai.AsyncOpenAI if async_ else openai.OpenAI)(
        api_key="synthetic-static-key",
        max_retries=0,
        http_client=http,
    )


def response_body(status="completed"):
    return {
        "id": "resp_review",
        "object": "response",
        "created_at": 1,
        "model": "synthetic",
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


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("extra", [False, True])
@pytest.mark.parametrize("credential", ["authorization", "header"])
def test_mcp_credentials_in_effective_body(tracing, async_, extra, credential):
    _, exporter = tracing
    secret = 'synthetic-extra-mcp-credential-17498-"quoted"'
    tool = {
        "type": "mcp",
        "server_label": "synthetic",
        "server_url": "https://synthetic.invalid/mcp",
        **(
            {"authorization": secret}
            if credential == "authorization"
            else {"headers": {"X-Private-Token": secret}}
        ),
    }
    original_tool = deepcopy(tool)
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        assert captured[-1]["tools"][0] == tool
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "invalid_request",
                    "message": "MCP authentication failed for " + secret,
                }
            },
        )

    params = {"model": "synthetic", "input": "question"}
    params.update(
        {"tools": [], "extra_body": {"tools": [tool]}} if extra else {"tools": [tool]}
    )

    async def run():
        async with sdk(handler, True) as client:
            await client.responses.create(**params)

    with pytest.raises(openai.BadRequestError) as caught:
        if async_:
            asyncio.run(run())
        else:
            with sdk(handler) as client:
                client.responses.create(**params)
    assert captured and tool == original_tool
    assert secret in caught.value.body["message"]
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert "synthetic-extra-mcp-credential" not in span.to_json()
    if extra:
        assert "synthetic.invalid/mcp" not in span.to_json()


@pytest.mark.parametrize("override", [None, [], openai.Omit()])
def test_effective_tools_override_original_without_mutation(override):
    original = {"tools": [{"type": "mcp", "authorization": "synthetic-original"}]}
    extra = {"tools": override}
    secrets = request_credentials(None, SimpleNamespace(extra_json=extra), original)
    assert "synthetic-original" not in secrets
    assert original["tools"][0]["authorization"] == "synthetic-original"
    assert extra["tools"] is override


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("mode", ["parse", "json", "text", "read"])
@pytest.mark.parametrize("status", ["completed", "failed", "incomplete", "cancelled"])
def test_raw_body_consumption(tracing, async_, mode, status):
    _, exporter = tracing
    body = response_body(status)

    def handler(request):
        return httpx.Response(200, json=body)

    def check(result):
        if mode == "parse":
            assert result.status == status
        elif mode == "json":
            assert result == body
        else:
            assert json.loads(result) == body

    async def run():
        async with sdk(handler, True) as client:
            async with client.responses.with_streaming_response.create(
                model="synthetic",
                input="question",
            ) as raw:
                check(await getattr(raw, mode)())
                check(await getattr(raw, mode)())
            await raw.close()

    if async_:
        asyncio.run(run())
    else:
        with sdk(handler) as client:
            with client.responses.with_streaming_response.create(
                model="synthetic",
                input="question",
            ) as raw:
                check(getattr(raw, mode)())
                check(getattr(raw, mode)())
            raw.close()
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is (
        StatusCode.OK if status == "completed" else StatusCode.ERROR
    )


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("mode", ["read", "text", "json"])
@pytest.mark.parametrize("outcome", ["completed", "unread", "error", "cancel", "sse"])
def test_raw_consumption_is_lazy_and_errors_finish_once(tracing, async_, mode, outcome):
    _, exporter = tracing
    reads = []
    closes = []
    error = (
        KeyboardInterrupt("synthetic abort")
        if outcome == "cancel"
        else RuntimeError("synthetic read failure")
    )
    body = json.dumps(response_body()).encode()

    class SyncBody(httpx.SyncByteStream):
        def __iter__(self):
            reads.append(1)
            if outcome in {"error", "cancel"}:
                raise error
            yield body

        def close(self):
            closes.append(1)

    class AsyncBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            reads.append(1)
            if outcome in {"error", "cancel"}:
                raise error
            yield body

        async def aclose(self):
            closes.append(1)

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=AsyncBody() if async_ else SyncBody(),
        )

    params = {"model": "synthetic", "input": "question", "stream": outcome == "sse"}

    async def run():
        async with sdk(handler, True) as client:
            async with client.responses.with_streaming_response.create(**params) as raw:
                assert not reads
                assert not exporter.get_finished_spans()
                if outcome != "unread":
                    await getattr(raw, mode)()
            await raw.close()

    def run_sync():
        with sdk(handler) as client:
            with client.responses.with_streaming_response.create(**params) as raw:
                assert not reads
                assert not exporter.get_finished_spans()
                if outcome != "unread":
                    getattr(raw, mode)()
            raw.close()

    if outcome in {"error", "cancel"}:
        with pytest.raises(type(error)) as caught:
            asyncio.run(run()) if async_ else run_sync()
        assert caught.value is error
    else:
        asyncio.run(run()) if async_ else run_sync()
    assert len(reads) == (0 if outcome == "unread" else 1)
    assert closes
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is (
        StatusCode.OK if outcome == "completed" else StatusCode.ERROR
    )
    if outcome in {"error", "cancel"}:
        assert len([event for event in span.events if event.name == "exception"]) == 1
