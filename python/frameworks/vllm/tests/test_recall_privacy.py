"""Retained and shared vLLM routes: privacy and cancellation at SDK boundaries."""

import asyncio
import importlib.util
import json
from contextlib import contextmanager

import openai
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_vllm import VLLMInstrumentor

if int(openai.__version__.split(".")[0]) >= 3:
    import httpx2 as httpx
else:
    import httpx

SDK_VERSION = tuple(int(part) for part in openai.__version__.split(".")[:2])
ROUTES = ["legacy"] + (
    ["shared"]
    if SDK_VERSION >= (1, 69) and importlib.util.find_spec("traceai_openai") is not None
    else []
)
INPUT = "SYNTHETIC-PRIVATE-PROMPT"
OUTPUT = "SYNTHETIC-PRIVATE-ANSWER"
IMAGE = "https://example.invalid/SYNTHETIC-PRIVATE-IMAGE.png"
OPTIONS = {
    "base_url": "http://localhost:8000/v1",
    "api_key": "synthetic-key",
    "max_retries": 0,
}
PARAMS = {"model": "test-vllm-model", "messages": [{"role": "user", "content": INPUT}]}
COMPLETION = {
    "id": "c1",
    "object": "chat.completion",
    "created": 1,
    "model": "test-vllm-model",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": OUTPUT},
        }
    ],
    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
}


def chunk(content):
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test-vllm-model",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": content},
                "finish_reason": None,
            }
        ],
    }


@contextmanager
def instrumented(route, config):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    if route == "legacy":
        instrumentor = VLLMInstrumentor()
        with pytest.warns(DeprecationWarning):
            instrumentor.instrument(tracer_provider=provider, config=config)
    else:
        from traceai_openai import OpenAIInstrumentor

        instrumentor = OpenAIInstrumentor()
        instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        yield exporter
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def client_for(route, handler, use_async=False):
    if use_async:
        client = openai.AsyncOpenAI(
            **OPTIONS,
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler), trust_env=False
            ),
        )
    else:
        client = openai.OpenAI(
            **OPTIONS,
            http_client=httpx.Client(
                transport=httpx.MockTransport(handler), trust_env=False
            ),
        )
    if route == "shared":
        from traceai_openai import set_client_provider

        set_client_provider(client, provider="vllm")
    return client


def only_span(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    return spans[0]


def serialized(span):
    return json.dumps(
        {
            "attributes": dict(span.attributes),
            "events": [dict(e.attributes) for e in span.events],
            "status": span.status.description,
        }
    )


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "flag,sentinel",
    [
        ("hide_input_text", INPUT),
        ("hide_input_messages", INPUT),
        ("hide_output_text", OUTPUT),
        ("hide_output_messages", OUTPUT),
        ("hide_input_images", IMAGE),
    ],
)
def test_granular_privacy_covers_every_copy(
    route, use_async, streaming, flag, sentinel, monkeypatch
):
    # Exercise the documented environment flags, not just direct config construction.
    monkeypatch.setenv("FI_" + flag.upper(), "true")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": INPUT},
                {"type": "image_url", "image_url": {"url": IMAGE}},
            ],
        }
    ]
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        if streaming:
            body = f"data: {json.dumps(chunk(OUTPUT))}\n\ndata: [DONE]\n\n"
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=body
            )
        return httpx.Response(200, json=COMPLETION)

    async def run_async():
        async with client_for(route, handler, True) as client:
            response = await client.chat.completions.create(
                **{**PARAMS, "messages": messages}, stream=streaming
            )
            if streaming:
                assert [c.choices[0].delta.content async for c in response] == [OUTPUT]
            else:
                assert response.choices[0].message.content == OUTPUT

    with instrumented(route, TraceConfig()) as exporter:
        if use_async:
            asyncio.run(run_async())
        else:
            with client_for(route, handler) as client:
                response = client.chat.completions.create(
                    **{**PARAMS, "messages": messages}, stream=streaming
                )
                if streaming:
                    assert [c.choices[0].delta.content for c in response] == [OUTPUT]
                else:
                    assert response.choices[0].message.content == OUTPUT
        span = only_span(exporter)
        wire = serialized(span)
        assert sentinel not in wire
        assert span.attributes["gen_ai.provider.name"] == "vllm"
        assert span.status.status_code == StatusCode.OK
        if flag.startswith("hide_input"):
            assert OUTPUT in wire
        else:
            assert INPUT in wire
        if flag == "hide_input_images":
            assert INPUT in wire  # typed text stays useful when only images are hidden
        if flag == "hide_input_text":
            assert IMAGE in wire  # typed images are independent of text capture
        if flag == "hide_input_messages":
            assert IMAGE not in wire  # separate image copies are messages too
        assert seen[0]["messages"] == messages  # masking must not modify the request


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("hide_error_details", [False, True])
def test_task_cancellation_finishes_once_and_preserves_error(
    route, streaming, hide_error_details
):
    async def run(exporter):
        entered = asyncio.Event()

        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield f"data: {json.dumps(chunk(OUTPUT))}\n\n".encode()
                entered.set()
                await asyncio.Event().wait()

        async def handler(request):
            if streaming:
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=SlowStream(),
                )
            entered.set()
            await asyncio.Event().wait()

        async with client_for(route, handler, True) as client:
            stream = None

            cancellation_args = []

            async def request():
                nonlocal stream
                try:
                    response = await client.chat.completions.create(
                        **PARAMS, stream=streaming
                    )
                    if streaming:
                        stream = response
                        async for _ in stream:
                            pass
                except asyncio.CancelledError as error:
                    cancellation_args.append(error.args)
                    raise

            task = asyncio.create_task(request())
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel("SYNTHETIC-CANCELLATION-DETAIL")
            with pytest.raises(asyncio.CancelledError):
                await task
            # Python 3.10 may replace the error when awaiting a cancelled task;
            # assert preservation at the actual application call boundary.
            assert cancellation_args == [("SYNTHETIC-CANCELLATION-DETAIL",)]
            if stream is not None:
                await stream.response.aclose()
            span = only_span(exporter)
            assert span.status.status_code == StatusCode.ERROR
            assert span.attributes["error.type"].endswith("CancelledError")
            events = [e for e in span.events if e.name == "exception"]
            assert len(events) == 1
            if hide_error_details:
                assert "SYNTHETIC-CANCELLATION-DETAIL" not in serialized(span)
            else:
                assert "SYNTHETIC-CANCELLATION-DETAIL" in serialized(span)

    with instrumented(
        route, TraceConfig(hide_error_details=hide_error_details)
    ) as exporter:
        asyncio.run(run(exporter))


@pytest.mark.skipif(
    SDK_VERSION < (1, 69), reason="SDK 1.0 declares List, not Iterable messages"
)
@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("iterable_container", [False, True])
def test_legacy_generator_messages_reach_transport(use_async, iterable_container):
    seen = []
    yielded = []

    def messages():
        for message in PARAMS["messages"]:
            yielded.append(message)
            yield message

    def handler(request):
        seen.append(json.loads(request.content)["messages"])
        return httpx.Response(200, json=COMPLETION)

    iterator = messages()

    class SingleUseIterable:
        def __iter__(self):
            return iterator

    request = {
        **PARAMS,
        "messages": SingleUseIterable() if iterable_container else iterator,
    }

    async def run():
        async with client_for("legacy", handler, True) as client:
            await client.chat.completions.create(**request)

    with instrumented("legacy", TraceConfig()) as exporter:
        if use_async:
            asyncio.run(run())
        else:
            with client_for("legacy", handler) as client:
                client.chat.completions.create(**request)
        assert seen == [PARAMS["messages"]]
        assert yielded == PARAMS["messages"]
        assert INPUT in serialized(only_span(exporter))


@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("consume_first", [False, True])
def test_legacy_explicit_close_releases_response_and_finishes_once(
    use_async, consume_first
):
    closed = []

    class SyncBody(httpx.SyncByteStream):
        def __iter__(self):
            yield f"data: {json.dumps(chunk(OUTPUT))}\n\n".encode()
            yield b"data: [DONE]\n\n"

        def close(self):
            closed.append(True)

    class AsyncBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield f"data: {json.dumps(chunk(OUTPUT))}\n\n".encode()
            yield b"data: [DONE]\n\n"

        async def aclose(self):
            closed.append(True)

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=AsyncBody() if use_async else SyncBody(),
        )

    async def run():
        async with client_for("legacy", handler, True) as client:
            stream = await client.chat.completions.create(**PARAMS, stream=True)
            if consume_first:
                await stream.__anext__()
            await stream.close()
            await stream.close()
            assert stream.response.is_closed

    with instrumented("legacy", TraceConfig()) as exporter:
        if use_async:
            asyncio.run(run())
        else:
            with client_for("legacy", handler) as client:
                stream = client.chat.completions.create(**PARAMS, stream=True)
                if consume_first:
                    next(stream)
                stream.close()
                stream.close()
                assert stream.response.is_closed
        assert closed == [True]
        assert only_span(exporter).status.status_code == StatusCode.OK


@pytest.mark.skipif(
    not hasattr(openai.Stream, "__enter__"),
    reason="SDK 1.0 streams do not implement context management",
)
@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("consume_first", [False, True])
@pytest.mark.parametrize("exception_type", [None, RuntimeError, asyncio.CancelledError])
def test_legacy_context_exit_finishes_once(use_async, consume_first, exception_type):
    def handler(request):
        body = f"data: {json.dumps(chunk(OUTPUT))}\n\ndata: [DONE]\n\n".encode()
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(body),
        )

    def exit_body():
        if exception_type is not None:
            raise exception_type("SYNTHETIC-CONSUMER-ERROR")

    async def run_async():
        async with client_for("legacy", handler, True) as client:
            stream = await client.chat.completions.create(**PARAMS, stream=True)
            try:
                async with stream as entered:
                    assert entered is stream
                    if consume_first:
                        await stream.__anext__()
                    exit_body()
            finally:
                assert stream.response.is_closed
                await stream.close()

    def run_sync():
        with client_for("legacy", handler) as client:
            stream = client.chat.completions.create(**PARAMS, stream=True)
            try:
                with stream as entered:
                    assert entered is stream
                    if consume_first:
                        next(stream)
                    exit_body()
            finally:
                assert stream.response.is_closed
                stream.close()

    with instrumented("legacy", TraceConfig(hide_error_details=True)) as exporter:

        def run():
            if use_async:
                asyncio.run(run_async())
            else:
                run_sync()

        if exception_type is not None:
            with pytest.raises(exception_type, match="SYNTHETIC-CONSUMER-ERROR"):
                run()
        else:
            run()
        span = only_span(exporter)
        expected = StatusCode.ERROR if exception_type is not None else StatusCode.OK
        assert span.status.status_code == expected
        assert "SYNTHETIC-CONSUMER-ERROR" not in serialized(span)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("consume_first", [False, True])
@pytest.mark.parametrize("cleanup", ["direct", "contextlib"])
def test_aclose_finishes_once_and_releases_response(route, consume_first, cleanup):
    from contextlib import aclosing

    async def run(exporter):
        closed = []
        responses = []

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield f"data: {json.dumps(chunk(OUTPUT))}\n\n".encode()
                yield b"data: [DONE]\n\n"

            async def aclose(self):
                closed.append(True)

        async def handler(request):
            response = httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Body()
            )
            responses.append(response)
            return response

        async with client_for(route, handler, True) as client:
            stream = await client.chat.completions.create(**PARAMS, stream=True)
            assert isinstance(stream, openai.AsyncStream)
            if cleanup == "contextlib":
                async with aclosing(stream) as entered:
                    assert entered is stream
                    if consume_first:
                        assert (await entered.__anext__()).choices[
                            0
                        ].delta.content == OUTPUT
            else:
                if consume_first:
                    assert (await stream.__anext__()).choices[0].delta.content == OUTPUT
                await stream.aclose()
            await stream.aclose()
            only_span(exporter)  # aclose itself must finalize, without a later close().
            await stream.close()
            assert responses[0].is_closed
            assert closed == [True]
            span = only_span(exporter)
            assert span.status.status_code == StatusCode.OK
            if consume_first:
                assert OUTPUT in serialized(span)

    with instrumented(route, TraceConfig()) as exporter:
        asyncio.run(run(exporter))


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("use_async", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("policy", ["environment", "explicit", "override"])
def test_invocation_privacy_covers_raw_copy(
    route, use_async, streaming, policy, monkeypatch
):
    secret_stop = "SYNTHETIC-PRIVATE-STOP-PARAMETER"
    secret_extra = "SYNTHETIC-PRIVATE-EXTRA-PARAMETER"
    monkeypatch.setenv("FI_HIDE_LLM_INVOCATION_PARAMETERS", "true")
    config = (
        TraceConfig()
        if policy == "environment"
        else TraceConfig(hide_llm_invocation_parameters=policy == "explicit")
    )
    hidden = policy != "override"
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        if streaming:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=f"data: {json.dumps(chunk(OUTPUT))}\n\ndata: [DONE]\n\n",
            )
        return httpx.Response(200, json=COMPLETION)

    kwargs = dict(
        **PARAMS,
        stop=[secret_stop],
        temperature=0.7,
        extra_body={"custom_parameter": secret_extra},
        stream=streaming,
    )

    async def run_async():
        async with client_for(route, handler, True) as client:
            result = await client.chat.completions.create(**kwargs)
            if streaming:
                assert [c async for c in result]

    with instrumented(route, config) as exporter:
        if use_async:
            asyncio.run(run_async())
        else:
            with client_for(route, handler) as client:
                result = client.chat.completions.create(**kwargs)
                if streaming:
                    assert list(result)
        span = only_span(exporter)
        assert seen[0]["stop"] == [secret_stop]
        assert seen[0]["temperature"] == 0.7
        assert seen[0]["custom_parameter"] == secret_extra
        assert seen[0]["messages"] == PARAMS["messages"]
        assert INPUT in serialized(span)
        assert OUTPUT in serialized(span)
        if hidden:
            assert "gen_ai.request.parameters" not in span.attributes
            assert secret_stop not in serialized(span)
            assert secret_extra not in serialized(span)
            if route == "legacy":
                raw = json.loads(span.attributes["input.value"])
                assert raw == {"model": PARAMS["model"], "messages": PARAMS["messages"]}
        else:
            assert secret_stop in span.attributes["gen_ai.request.parameters"]
            if route == "legacy":
                raw = json.loads(span.attributes["input.value"])
                assert raw["temperature"] == 0.7
                assert raw["extra_body"] == {"custom_parameter": secret_extra}
