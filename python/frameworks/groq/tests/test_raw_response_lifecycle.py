"""Native response APIs against synthetic HTTP, with no provider credentials."""

import asyncio
from contextlib import asynccontextmanager, contextmanager

import pytest
import test_stream_lifecycle
from fi_instrumentation import TraceConfig
from groq import APIResponse, AsyncAPIResponse, AsyncStream, Stream
from groq.types.chat import ChatCompletionChunk
from test_stream_lifecycle import (
    EVENTS,
    OUTPUT,
    PARAMS,
    AsyncBody,
    ExportTraceServiceRequest,
    OTLPSpanExporter,
    SyncBody,
    async_client,
    instrumented,
    single_span,
    sync_client,
)
from traceai_groq._request_attributes_extractor import _RequestAttributesExtractor

collector = test_stream_lifecycle.collector


@contextmanager
def sync_response(client, api, **params):
    resource = getattr(client.chat.completions, api)
    if api == "with_streaming_response":
        with resource.create(**params) as response:
            yield response
    else:
        response = resource.create(**params)
        try:
            yield response
        finally:
            response.close()


@asynccontextmanager
async def async_response(client, api, **params):
    resource = getattr(client.chat.completions, api)
    if api == "with_streaming_response":
        async with resource.create(**params) as response:
            yield response
    else:
        response = await resource.create(**params)
        try:
            yield response
        finally:
            await response.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
async def test_transport_credentials_never_export(asynchronous, hidden):
    secret = "synthetic-transport-credential"
    headers = {
        "Authorization": "Bearer " + secret,
        "X-Api-Key": secret,
        "Cookie": secret,
        "custom-credential": secret,
    }
    params = dict(PARAMS, extra_headers=headers, extra_query={"api_key": secret})
    config = TraceConfig(hide_inputs=hidden, hide_outputs=hidden)
    with instrumented(config) as (memory, _):
        if asynchronous:
            async with async_client(AsyncBody()) as client:
                stream = await client.chat.completions.create(**params)
                assert stream.response.request.headers["custom-credential"] == secret
                assert stream.response.request.url.params["api_key"] == secret
                _ = [chunk async for chunk in stream]
        else:
            with sync_client(SyncBody()) as client:
                stream = client.chat.completions.create(**params)
                assert stream.response.request.headers["custom-credential"] == secret
                assert stream.response.request.url.params["api_key"] == secret
                list(stream)
        span = single_span(memory)
        assert secret not in span.to_json()
        assert "extra_headers" not in span.attributes["gen_ai.request.parameters"]
        assert "extra_query" not in span.attributes["gen_ai.request.parameters"]
    assert headers["custom-credential"] == secret  # caller data was not mutated


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("api", ["with_raw_response", "with_streaming_response"])
@pytest.mark.parametrize("consume", [None, 0, 1, 2, "all"])
async def test_raw_response_stream_lifecycle(asynchronous, api, consume):
    body = (AsyncBody if asynchronous else SyncBody)()
    with instrumented() as (memory, _):
        if asynchronous:
            async with async_client(body) as client:
                async with async_response(client, api, **PARAMS) as response:
                    assert isinstance(response, AsyncAPIResponse)
                    assert response.headers["x-request-id"] == "synthetic-request"
                    assert not response.is_closed
                    assert not memory.get_finished_spans()
                    if consume is not None:
                        stream = await response.parse()
                        assert isinstance(stream, AsyncStream)
                        assert await response.parse() is stream
                        assert not memory.get_finished_spans()
                        if consume == "all":
                            _ = [chunk async for chunk in stream]
                            single_span(memory)
                        else:
                            for _ in range(consume):
                                await stream.__anext__()
                            assert not memory.get_finished_spans()
                await response.close()
        else:
            with sync_client(body) as client:
                with sync_response(client, api, **PARAMS) as response:
                    assert isinstance(response, APIResponse)
                    assert response.headers["x-request-id"] == "synthetic-request"
                    assert not response.is_closed
                    assert not memory.get_finished_spans()
                    if consume is not None:
                        stream = response.parse()
                        assert isinstance(stream, Stream)
                        assert response.parse() is stream
                        assert not memory.get_finished_spans()
                        if consume == "all":
                            list(stream)
                            single_span(memory)
                        else:
                            for _ in range(consume):
                                next(stream)
                            assert not memory.get_finished_spans()
                response.close()
        span = single_span(memory)
        assert span.attributes["output.value"] == (
            OUTPUT + ("-tail" if consume in (2, "all") else "") if consume else ""
        )
        if consume in (2, "all"):
            assert span.attributes["gen_ai.usage.total_tokens"] == 5
            assert span.attributes["gen_ai.response.model"] == "synthetic-model"
        assert response.is_closed
        assert body.closes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("api", ["with_raw_response", "with_streaming_response"])
@pytest.mark.parametrize("where", ["iteration", "read", "parse", "close", "cancel"])
async def test_raw_response_error_identity_and_span(asynchronous, api, where):
    if where == "cancel" and not asynchronous:
        pytest.skip("Cancellation is an async operation")
    error = (
        asyncio.CancelledError()
        if where == "cancel"
        else RuntimeError("synthetic-read-error")
    )
    body = (AsyncBody if asynchronous else SyncBody)(
        EVENTS[:1],
        error=error if where in ("iteration", "read", "cancel") else None,
        close_error=error if where == "close" else None,
    )
    with instrumented() as (memory, _):
        expected_type = TypeError if where == "parse" else type(error)
        with pytest.raises(expected_type) as raised:
            if asynchronous:
                async with async_client(body) as client:
                    async with async_response(client, api, **PARAMS) as response:
                        assert not memory.get_finished_spans()
                        if where == "parse":
                            await response.parse(to=int)
                        elif where == "read":
                            await response.read()
                        elif where != "close":
                            stream = await response.parse()
                            _ = [chunk async for chunk in stream]
            else:
                with sync_client(body) as client:
                    with sync_response(client, api, **PARAMS) as response:
                        assert not memory.get_finished_spans()
                        if where == "parse":
                            response.parse(to=int)
                        elif where == "read":
                            response.read()
                        elif where != "close":
                            list(response.parse())
        if where != "parse":
            assert raised.value is error
        span = single_span(memory, error=True)
        assert len(span.events) == 1
        if where in ("iteration", "cancel"):
            assert span.attributes["output.value"] == OUTPUT
        assert body.closes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_raw_response_custom_parse_and_prefetched_body(asynchronous):
    with instrumented() as (memory, _):
        if asynchronous:
            async with async_client(AsyncBody()) as client:
                async with async_response(
                    client, "with_raw_response", **PARAMS
                ) as response:
                    assert await response.read() == b"".join(EVENTS)
                    assert single_span(memory).attributes["output.value"] == OUTPUT + "-tail"
                    stream = await response.parse(to=AsyncStream[ChatCompletionChunk])
                    assert (
                        await response.parse(to=AsyncStream[ChatCompletionChunk])
                        is stream
                    )
                    _ = [chunk async for chunk in stream]
        else:
            with sync_client(SyncBody()) as client:
                with sync_response(client, "with_raw_response", **PARAMS) as response:
                    assert response.read() == b"".join(EVENTS)
                    assert single_span(memory).attributes["output.value"] == OUTPUT + "-tail"
                    stream = response.parse(to=Stream[ChatCompletionChunk])
                    assert response.parse(to=Stream[ChatCompletionChunk]) is stream
                    list(stream)
        assert single_span(memory).attributes["output.value"] == OUTPUT + "-tail"


@pytest.mark.parametrize("messages", [None, [], [{"role": "user", "content": "hello"}]])
def test_transport_omission_covers_every_input_attribute(messages):
    extractor = _RequestAttributesExtractor()
    params = dict(
        PARAMS,
        messages=messages,
        extra_headers={"Authorization": "synthetic-secret"},
        extra_query={"key": "synthetic-secret"},
        temperature=0.25,
    )
    # Inspect every yielded value, not a dict: input.value is emitted twice.
    attributes = list(extractor.get_attributes_from_request(params))
    attributes += list(extractor.get_extra_attributes_from_request(params))
    assert "synthetic-secret" not in str(attributes)
    assert '"temperature": 0.25' in dict(attributes)["gen_ai.request.parameters"]
    assert params["extra_headers"]["Authorization"] == "synthetic-secret"


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "method", ["iter_bytes", "iter_text", "iter_lines", "text", "json"]
)
@pytest.mark.parametrize("fails", [False, True])
async def test_raw_response_low_level_reads(asynchronous, method, fails):
    error = RuntimeError("synthetic-low-level-error")
    body = (AsyncBody if asynchronous else SyncBody)(error=error if fails else None)
    with instrumented() as (memory, _):

        async def consume():
            if asynchronous:
                async with async_client(body) as client:
                    async with async_response(
                        client, "with_streaming_response", **PARAMS
                    ) as response:
                        if method.startswith("iter_"):
                            return [part async for part in getattr(response, method)()]
                        return await getattr(response, method)()
            else:
                with sync_client(body) as client:
                    with sync_response(
                        client, "with_streaming_response", **PARAMS
                    ) as response:
                        result = getattr(response, method)()
                        return list(result) if method.startswith("iter_") else result

        if fails or method == "json":
            # The successful transport carries SSE, not JSON. Preserve that
            # decoding error rather than silently recording a successful call.
            with pytest.raises(RuntimeError if fails else ValueError) as raised:
                await consume()
            if fails:
                assert raised.value is error
        else:
            assert await consume()
        span = single_span(memory, error=fails or method == "json")
        assert len(span.events) == int(fails or method == "json")
        assert body.closes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
async def test_raw_response_partial_failure_otlp_privacy(
    collector, asynchronous, hidden
):
    endpoint, requests = collector
    error = RuntimeError("synthetic-raw-read-error")
    body = (AsyncBody if asynchronous else SyncBody)(EVENTS[:1], error=error)
    options = {"hide_inputs": hidden, "hide_outputs": hidden}
    if hasattr(TraceConfig, "hide_error_details"):
        options["hide_error_details"] = hidden
    config = TraceConfig(**options)
    params = dict(PARAMS, extra_headers={"Authorization": "synthetic-secret"})
    with instrumented(config, OTLPSpanExporter(endpoint=endpoint)):
        with pytest.raises(RuntimeError) as raised:
            if asynchronous:
                async with async_client(body) as client:
                    async with async_response(
                        client, "with_streaming_response", **params
                    ) as response:
                        stream = await response.parse()
                        _ = [chunk async for chunk in stream]
            else:
                with sync_client(body) as client:
                    with sync_response(
                        client, "with_streaming_response", **params
                    ) as response:
                        list(response.parse())
        assert raised.value is error
    assert len(requests) == 1
    wire = ExportTraceServiceRequest.FromString(requests[0])
    spans = [
        span
        for resource in wire.resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
    ]
    assert len(spans) == 1
    assert spans[0].status.code == 2
    assert len(spans[0].events) == 1
    assert b"synthetic-secret" not in requests[0]
    assert (OUTPUT.encode() in requests[0]) is not hidden
    assert (str(error).encode() in requests[0]) is not getattr(
        config, "hide_error_details", False
    )
    assert body.closes == 1
