"""Independent review probes; all credentials and transports are synthetic."""

import asyncio
import json

import httpx
import openai
import pytest
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_openai import OpenAIInstrumentor


@pytest.fixture
def tracing():
    exporter = InMemorySpanExporter()
    provider = register(
        project_name="review-edges",
        project_type=ProjectType.OBSERVE,
        span_exporter=exporter,
        batch=False,
        verbose=False,
    )
    OpenAIInstrumentor().instrument(tracer_provider=provider)
    try:
        yield provider, exporter
    finally:
        OpenAIInstrumentor().uninstrument()
        provider.force_flush()
        provider.shutdown()


def client(handler, async_=False, key="synthetic-static-key"):
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
        "id": "resp_edge",
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


def sse(status):
    response = body(status)
    if status == "failed":
        response["error"] = {"code": "server_error", "message": "synthetic failure"}
    kind = "response.created" if status == "in_progress" else "response." + status
    return (
        "event: "
        + kind
        + "\ndata: "
        + json.dumps({"type": kind, "sequence_number": 1, "response": response})
        + "\n\n"
    ).encode()


def test_concurrent_callable_nonstream_credentials(tracing):
    if int(openai.__version__.split(".")[0]) < 3:
        pytest.skip("callable auth not available at floor")
    _provider, exporter = tracing
    keys = ["synthetic-alpha-credential-198273", "synthetic-bravo-credential-543215"]
    issued = []

    async def run():
        received = []
        both_sent = asyncio.Event()

        async def token():
            key = keys[len(issued)]
            issued.append(key)
            return key

        async def handler(request):
            key = request.headers["authorization"][7:]
            received.append(key)
            if len(received) == 2:
                both_sent.set()
            await asyncio.wait_for(both_sent.wait(), 3)
            response = body("failed")
            response["error"] = {
                "code": "server_error",
                "message": "Rejected token " + key,
            }
            return httpx.Response(200, json=response)

        async with client(handler, True, token) as c:
            responses = await asyncio.gather(
                *[c.responses.create(model="parallel", input="question") for _ in keys]
            )
        assert set(received) == set(keys)
        assert {r.error.message for r in responses} == {
            "Rejected token " + k for k in keys
        }

    asyncio.run(run())
    assert issued == keys
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert all(s.status.status_code == StatusCode.ERROR for s in spans)
    leaked = {
        key: [
            name
            for span in spans
            for name, value in span.attributes.items()
            if key in str(value)
        ]
        for key in keys
    }
    assert not any(leaked.values()), leaked


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("status", ["failed", "completed", "in_progress"])
@pytest.mark.parametrize("exhaust", [False, True])
def test_streaming_response_envelope_lifecycle(tracing, async_, status, exhaust):
    provider, exporter = tracing

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse(status)
        )

    async def run():
        async with client(handler, True) as c:
            async with c.responses.with_streaming_response.create(
                model="parallel", input="question", stream=True
            ) as raw:
                stream = await raw.parse()
                if exhaust:
                    events = [chunk async for chunk in stream]
                    assert events[-1].response.status == status
                else:
                    assert (await stream.__anext__()).response.status == status

    if async_:
        asyncio.run(run())
    else:
        with client(handler) as c:
            with c.responses.with_streaming_response.create(
                model="parallel", input="question", stream=True
            ) as raw:
                stream = raw.parse()
                if exhaust:
                    events = list(stream)
                    assert events[-1].response.status == status
                else:
                    assert next(stream).response.status == status
    provider.force_flush()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, f"Expected one finalized span, got {len(spans)}"
    (span,) = spans
    assert span.status.status_code == (
        StatusCode.OK if status == "completed" else StatusCode.ERROR
    ), span.to_json()
    if status != "in_progress":
        assert span.attributes["gen_ai.usage.total_tokens"] == 7, span.to_json()


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("instrumented", [False, True])
def test_raw_explicit_stream_parse(tracing, async_, instrumented):
    provider, exporter = tracing
    if not instrumented:
        OpenAIInstrumentor().uninstrument()
    from openai.types.responses import ResponseStreamEvent

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse("failed")
        )

    async def run():
        async with client(handler, True) as c:
            raw = await c.responses.with_raw_response.create(
                model="parallel", input="question", stream=True
            )
            stream = raw.parse(to=openai.AsyncStream[ResponseStreamEvent])
            assert raw.parse(to=openai.AsyncStream[ResponseStreamEvent]) is stream
            events = [event async for event in stream]
            assert events[-1].response.status == "failed"
            await stream.close()

    if async_:
        asyncio.run(run())
    else:
        with client(handler) as c:
            raw = c.responses.with_raw_response.create(
                model="parallel", input="question", stream=True
            )
            stream = raw.parse(to=openai.Stream[ResponseStreamEvent])
            assert raw.parse(to=openai.Stream[ResponseStreamEvent]) is stream
            events = list(stream)
            assert events[-1].response.status == "failed"
            stream.close()
    provider.force_flush()
    spans = exporter.get_finished_spans()
    if instrumented:
        assert len(spans) == 1, f"Expected one finalized span, got {len(spans)}"
        assert spans[0].status.status_code is StatusCode.ERROR
        assert spans[0].attributes["gen_ai.usage.total_tokens"] == 7
    else:
        assert not spans


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("parse", [False, True])
def test_lazy_envelope_does_not_read_until_requested(tracing, async_, streaming, parse):
    provider, exporter = tracing
    reads = []
    closes = []
    payload = sse("completed") if streaming else json.dumps(body()).encode()

    class SyncBody(httpx.SyncByteStream):
        def __iter__(self):
            reads.append("read")
            yield payload

        def close(self):
            closes.append("close")

    class AsyncBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            reads.append("read")
            yield payload

        async def aclose(self):
            closes.append("close")

    def handler(request):
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream" if streaming else "application/json"
            },
            stream=AsyncBody() if async_ else SyncBody(),
        )

    async def run():
        async with client(handler, True) as c:
            async with c.responses.with_streaming_response.create(
                model="parallel", input="question", stream=streaming
            ) as raw:
                assert isinstance(raw, openai.AsyncAPIResponse)
                assert not reads
                assert not exporter.get_finished_spans()
                if parse:
                    result = await raw.parse()
                    assert await raw.parse() is result
                    if streaming:
                        assert not reads
                        assert (await result.__anext__()).response.status == "completed"
                    else:
                        assert result.status == "completed"

    if async_:
        asyncio.run(run())
    else:
        with client(handler) as c:
            with c.responses.with_streaming_response.create(
                model="parallel", input="question", stream=streaming
            ) as raw:
                assert isinstance(raw, openai.APIResponse)
                assert not reads
                assert not exporter.get_finished_spans()
                if parse:
                    result = raw.parse()
                    assert raw.parse() is result
                    if streaming:
                        assert not reads
                        assert next(result).response.status == "completed"
                    else:
                        assert result.status == "completed"
    assert reads == (["read"] if parse else [])
    assert closes == ["close"]
    provider.force_flush()
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == (StatusCode.OK if parse else StatusCode.ERROR)
    if parse:
        assert span.attributes["gen_ai.usage.total_tokens"] == 7


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("close_http", [False, True])
def test_explicit_stream_cache_and_envelope_close(tracing, async_, modern, close_http):
    provider, exporter = tracing
    from openai.types.responses import ResponseStreamEvent

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse("completed")
        )

    async def consume(raw):
        if modern:
            default = await raw.parse()
            typed = await raw.parse(to=openai.AsyncStream[ResponseStreamEvent])
            assert await raw.parse(to=openai.AsyncStream[ResponseStreamEvent]) is typed
            assert await raw.parse() is default
        else:
            default = raw.parse()
            typed = raw.parse(to=openai.AsyncStream[ResponseStreamEvent])
            assert raw.parse(to=openai.AsyncStream[ResponseStreamEvent]) is typed
            assert raw.parse() is default
        assert typed is not default
        assert (await typed.__anext__()).response.status == "completed"
        assert not exporter.get_finished_spans()
        if close_http:
            await raw.http_response.aclose()
        elif modern:
            await raw.close()
        else:
            await typed.close()
        # Both cache entries and every close path share one terminal result.
        await default.close()
        await typed.close()

    async def run():
        async with client(handler, True) as c:
            if modern:
                async with c.responses.with_streaming_response.create(
                    model="parallel", input="question", stream=True
                ) as raw:
                    await consume(raw)
            else:
                raw = await c.responses.with_raw_response.create(
                    model="parallel", input="question", stream=True
                )
                await consume(raw)

    def consume_sync(raw):
        default = raw.parse()
        typed = raw.parse(to=openai.Stream[ResponseStreamEvent])
        assert raw.parse(to=openai.Stream[ResponseStreamEvent]) is typed
        assert raw.parse() is default
        assert typed is not default
        assert next(typed).response.status == "completed"
        assert not exporter.get_finished_spans()
        if close_http:
            raw.http_response.close()
        elif modern:
            raw.close()
        else:
            typed.close()
        default.close()
        typed.close()

    if async_:
        asyncio.run(run())
    else:
        with client(handler) as c:
            if modern:
                with c.responses.with_streaming_response.create(
                    model="parallel", input="question", stream=True
                ) as raw:
                    consume_sync(raw)
            else:
                raw = c.responses.with_raw_response.create(
                    model="parallel", input="question", stream=True
                )
                consume_sync(raw)
    provider.force_flush()
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.OK
    assert span.attributes["gen_ai.usage.total_tokens"] == 7


@pytest.mark.parametrize("cancel", [False, True])
def test_async_lazy_parse_failure_finishes_once(tracing, cancel):
    provider, exporter = tracing
    entered = None

    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            raise httpx.ReadError("synthetic unreadable response")
            yield b""  # Make the method an async generator.

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "application/json"}, stream=BrokenBody()
        )

    async def run():
        nonlocal entered
        entered = asyncio.Event()
        async with client(handler, True) as c:
            async with c.responses.with_streaming_response.create(
                model="parallel", input="question"
            ) as raw:
                task = asyncio.create_task(raw.parse())
                await asyncio.wait_for(entered.wait(), 3)
                if cancel:
                    task.cancel()
                with pytest.raises(
                    asyncio.CancelledError if cancel else httpx.ReadError
                ):
                    await task
                assert len(exporter.get_finished_spans()) == 1

    asyncio.run(run())
    provider.force_flush()
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR


def test_credential_context_is_reset_and_hooks_restore(tracing):
    from traceai_openai._request_context import _active_request

    original_sync = OpenAIInstrumentor()._original_process_response
    original_async = OpenAIInstrumentor()._original_async_process_response
    assert _active_request.get() is None
    with client(lambda request: httpx.Response(200, json=body())) as c:
        c.responses.create(model="parallel", input="question")
    assert _active_request.get() is None
    with client(
        lambda request: httpx.Response(
            401, json={"error": {"message": "synthetic error"}}
        )
    ) as c:
        with pytest.raises(openai.AuthenticationError):
            c.responses.create(model="parallel", input="question")
    assert _active_request.get() is None
    OpenAIInstrumentor().uninstrument()
    assert openai.OpenAI._process_response is original_sync
    assert openai.AsyncOpenAI._process_response is original_async
