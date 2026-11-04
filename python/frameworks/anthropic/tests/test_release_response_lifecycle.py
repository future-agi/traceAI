"""Real SDK + loopback HTTP regressions; no provider credentials or external calls."""
import asyncio
import inspect
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anthropic
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_anthropic import AnthropicInstrumentor

PARAMS = dict(model="synthetic", max_tokens=16, messages=[{"role": "user", "content": "INPUT_CANARY"}])
MESSAGE = dict(
    id="msg_mock", type="message", role="assistant", model="synthetic",
    content=[{"type": "text", "text": "OUTPUT_CANARY"}], stop_reason="end_turn",
    stop_sequence=None,
    usage=dict(input_tokens=3, output_tokens=2, cache_creation_input_tokens=4, cache_read_input_tokens=5),
)
EVENTS = [
    {"type": "message_start", "message": {**MESSAGE, "content": [], "stop_reason": None,
        "usage": {**MESSAGE["usage"], "output_tokens": 0}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OUTPUT_CANARY"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 2}},
    {"type": "message_stop"},
]


@pytest.fixture
def harness():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if body.get("stream"):
                events = EVENTS[:3] if body["model"] == "truncated" else EVENTS
                payload = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
                mime = "text/event-stream"
            else:
                payload = b"{invalid" if body["model"] == "invalid-json" else json.dumps(MESSAGE).encode()
                mime = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(payload) + (100 if body["model"] == "broken-body" else 0)))
            self.send_header("request-id", "synthetic-request")
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    kwargs = dict(api_key="SYNTHETIC", base_url=f"http://127.0.0.1:{server.server_port}", max_retries=0)
    if "open_telemetry" in inspect.signature(anthropic.Anthropic).parameters:
        kwargs["open_telemetry"] = {"tracer_provider": provider}
    instrumentor = AnthropicInstrumentor()
    try:
        yield provider, exporter, instrumentor, kwargs
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
        server.shutdown()
        server.server_close()
        thread.join()


def assert_usage(exporter, hidden=False):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.OK
    assert span.attributes["gen_ai.usage.input_tokens"] == 12
    assert span.attributes["gen_ai.usage.output_tokens"] == 2
    assert span.attributes["gen_ai.usage.total_tokens"] == 14
    assert span.attributes["gen_ai.usage.cache_write_tokens"] == 4
    assert span.attributes["gen_ai.usage.cache_read_tokens"] == 5
    if hidden:
        assert "INPUT_CANARY" not in span.to_json()
        assert "OUTPUT_CANARY" not in span.to_json()


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
def test_cache_usage(harness, monkeypatch, async_, stream, hidden):
    if hidden:
        monkeypatch.setenv("FI_HIDE_INPUTS", "true")
        monkeypatch.setenv("FI_HIDE_OUTPUTS", "true")
    provider, exporter, instrumentor, kwargs = harness
    instrumentor.instrument(tracer_provider=provider)

    async def request():
        async with anthropic.AsyncAnthropic(**kwargs) as client:
            result = await client.messages.create(**PARAMS, stream=stream)
            if stream:
                assert [e.type async for e in result][-1] == "message_stop"
                await result.close()

    if async_:
        asyncio.run(request())
    else:
        with anthropic.Anthropic(**kwargs) as client:
            result = client.messages.create(**PARAMS, stream=stream)
            if stream:
                assert list(result)[-1].type == "message_stop"
                result.close()
    assert_usage(exporter, hidden)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("raw_kind", ["with_raw_response", "with_streaming_response"])
def test_response_helpers(harness, async_, stream, raw_kind):
    provider, exporter, instrumentor, kwargs = harness
    instrumentor.instrument(tracer_provider=provider)

    async def consume(raw):
        assert raw.headers["request-id"] == "synthetic-request"
        if raw_kind == "with_streaming_response":
            assert not raw.http_response.is_stream_consumed
        parsed = raw.parse()
        if inspect.isawaitable(parsed):
            parsed = await parsed
        if stream:
            assert [e.type async for e in parsed][-1] == "message_stop"
            await parsed.close()
        else:
            assert parsed.id == "msg_mock"
        again = raw.parse()
        if inspect.isawaitable(again):
            again = await again
        assert again is parsed

    async def request():
        async with anthropic.AsyncAnthropic(**kwargs) as client:
            resource = getattr(client.messages, raw_kind)
            if raw_kind == "with_streaming_response":
                async with resource.create(**PARAMS, stream=stream) as raw:
                    await consume(raw)
            else:
                await consume(await resource.create(**PARAMS, stream=stream))

    def consume_sync(raw):
        assert raw.headers["request-id"] == "synthetic-request"
        if raw_kind == "with_streaming_response":
            assert not raw.http_response.is_stream_consumed
        parsed = raw.parse()
        if stream:
            assert list(parsed)[-1].type == "message_stop"
            parsed.close()
        else:
            assert parsed.id == "msg_mock"
        assert raw.parse() is parsed

    if async_:
        asyncio.run(request())
    else:
        with anthropic.Anthropic(**kwargs) as client:
            resource = getattr(client.messages, raw_kind)
            if raw_kind == "with_streaming_response":
                with resource.create(**PARAMS, stream=stream) as raw:
                    consume_sync(raw)
            else:
                consume_sync(resource.create(**PARAMS, stream=stream))
    assert_usage(exporter)


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
def test_truncation_and_explicit_close(harness, async_, cancel):
    provider, exporter, instrumentor, kwargs = harness
    instrumentor.instrument(tracer_provider=provider)
    params = {**PARAMS, "model": "truncated", "stream": True}

    async def request():
        async with anthropic.AsyncAnthropic(**kwargs) as client:
            stream = await client.messages.create(**params)
            if cancel:
                await stream.__anext__()
            else:
                assert len([e async for e in stream]) == 3
            await stream.close()

    if async_:
        asyncio.run(request())
    else:
        with anthropic.Anthropic(**kwargs) as client:
            stream = client.messages.create(**params)
            if cancel:
                next(stream)
            else:
                assert len(list(stream)) == 3
            stream.close()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == (StatusCode.UNSET if cancel else StatusCode.ERROR)


@pytest.mark.parametrize("async_", [False, True])
def test_unparsed_streaming_response_closes_span(harness, async_):
    provider, exporter, instrumentor, kwargs = harness
    instrumentor.instrument(tracer_provider=provider)

    async def request():
        async with anthropic.AsyncAnthropic(**kwargs) as client:
            async with client.messages.with_streaming_response.create(**PARAMS) as raw:
                assert not raw.http_response.is_stream_consumed
                assert not exporter.get_finished_spans()

    if async_:
        asyncio.run(request())
    else:
        with anthropic.Anthropic(**kwargs) as client:
            with client.messages.with_streaming_response.create(**PARAMS) as raw:
                assert not raw.http_response.is_stream_consumed
                assert not exporter.get_finished_spans()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.UNSET


@pytest.mark.parametrize("async_", [False, True])
def test_raw_parse_error_preserved_and_finalized(harness, async_):
    provider, exporter, instrumentor, kwargs = harness
    instrumentor.instrument(tracer_provider=provider)

    async def request():
        async with anthropic.AsyncAnthropic(**kwargs) as client:
            async with client.messages.with_streaming_response.create(**{**PARAMS, "model": "invalid-json"}) as raw:
                with pytest.raises(json.JSONDecodeError):
                    await raw.parse()

    if async_:
        asyncio.run(request())
    else:
        with anthropic.Anthropic(**kwargs) as client:
            with client.messages.with_streaming_response.create(**{**PARAMS, "model": "invalid-json"}) as raw:
                with pytest.raises(json.JSONDecodeError):
                    raw.parse()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("method", ["parse", "read", "text", "json", "iter_bytes", "iter_text", "iter_lines"])
def test_raw_body_transport_failure_is_error(harness, async_, method):
    provider, exporter, instrumentor, kwargs = harness
    instrumentor.instrument(tracer_provider=provider)
    params = {**PARAMS, "model": "broken-body"}

    def assert_error_span(error):
        # SDK 2.4 uses httpx2; older versions use httpx. Both expose this error.
        assert type(error).__name__ == "RemoteProtocolError"
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.status.status_code == StatusCode.ERROR
        exceptions = [event for event in span.events if event.name == "exception"]
        assert len(exceptions) == 1
        assert exceptions[0].attributes["exception.message"] == str(error)
        return span

    async def request():
        async with anthropic.AsyncAnthropic(**kwargs) as client:
            async with client.messages.with_streaming_response.create(**params) as raw:
                assert not raw.http_response.is_stream_consumed
                with pytest.raises(Exception, match="peer closed connection") as caught:
                    result = getattr(raw, method)()
                    if method.startswith("iter_"):
                        # Obtaining an iterator must not consume the response.
                        assert not raw.http_response.is_stream_consumed
                        assert not exporter.get_finished_spans()
                        async for _ in result:
                            pass
                    else:
                        await result
                span = assert_error_span(caught.value)
                await raw.close()
            assert exporter.get_finished_spans() == (span,)

    if async_:
        asyncio.run(request())
    else:
        with anthropic.Anthropic(**kwargs) as client:
            with client.messages.with_streaming_response.create(**params) as raw:
                assert not raw.http_response.is_stream_consumed
                with pytest.raises(Exception, match="peer closed connection") as caught:
                    result = getattr(raw, method)()
                    if method.startswith("iter_"):
                        assert not raw.http_response.is_stream_consumed
                        assert not exporter.get_finished_spans()
                        list(result)
                span = assert_error_span(caught.value)
                raw.close()
            assert exporter.get_finished_spans() == (span,)
    assert len(exporter.get_finished_spans()) == 1
    assert exporter.get_finished_spans()[0].status.status_code == StatusCode.ERROR


@pytest.mark.parametrize("async_", [False, True])
@pytest.mark.parametrize("method", ["read", "text", "json", "iter_bytes", "iter_text", "iter_lines"])
def test_raw_body_readers_preserve_success(harness, async_, method):
    provider, exporter, instrumentor, kwargs = harness
    instrumentor.instrument(tracer_provider=provider)

    def assert_body(value):
        if method == "iter_bytes":
            value = b"".join(value)
        elif method.startswith("iter_"):
            value = "".join(value)
        assert (value if method == "json" else json.loads(value)) == MESSAGE
        # Raw reads do not parse a message or finalize the span as successful.
        assert not exporter.get_finished_spans()

    async def request():
        async with anthropic.AsyncAnthropic(**kwargs) as client:
            async with client.messages.with_streaming_response.create(**PARAMS) as raw:
                result = getattr(raw, method)()
                if method.startswith("iter_"):
                    assert not raw.http_response.is_stream_consumed
                    value = [chunk async for chunk in result]
                else:
                    value = await result
                assert_body(value)

    if async_:
        asyncio.run(request())
    else:
        with anthropic.Anthropic(**kwargs) as client:
            with client.messages.with_streaming_response.create(**PARAMS) as raw:
                result = getattr(raw, method)()
                if method.startswith("iter_"):
                    assert not raw.http_response.is_stream_consumed
                    result = list(result)
                assert_body(result)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.UNSET
    assert not spans[0].events


@pytest.mark.parametrize("async_", [False, True])
def test_raw_text_preserves_legacy_property_and_modern_method(harness, async_):
    provider, exporter, instrumentor, kwargs = harness
    instrumentor.instrument(tracer_provider=provider)

    async def request():
        async with anthropic.AsyncAnthropic(**kwargs) as client:
            raw = await client.messages.with_raw_response.create(**PARAMS)
            # Older SDKs expose text as a property, even for async clients.
            assert callable(raw.text) == callable(raw.__wrapped__.text)
            text = raw.text() if callable(raw.text) else raw.text
            if inspect.isawaitable(text):
                text = await text
            assert json.loads(text) == MESSAGE
            parsed = raw.parse()
            if inspect.isawaitable(parsed):
                parsed = await parsed
            assert parsed.id == MESSAGE["id"]

    if async_:
        asyncio.run(request())
    else:
        with anthropic.Anthropic(**kwargs) as client:
            raw = client.messages.with_raw_response.create(**PARAMS)
            assert callable(raw.text) == callable(raw.__wrapped__.text)
            text = raw.text() if callable(raw.text) else raw.text
            assert json.loads(text) == MESSAGE
            assert raw.parse().id == MESSAGE["id"]
    assert_usage(exporter)


@pytest.mark.parametrize("async_", [False, True])
def test_output_extraction_failure_does_not_change_response_or_usage(harness, monkeypatch, async_):
    from traceai_anthropic import _wrappers

    def broken_output(response):
        raise ValueError("telemetry only")

    monkeypatch.setattr(_wrappers, "_get_outputs", broken_output)
    monkeypatch.setattr(_wrappers, "_get_async_outputs", broken_output)
    provider, exporter, instrumentor, kwargs = harness
    instrumentor.instrument(tracer_provider=provider)

    async def request():
        async with anthropic.AsyncAnthropic(**kwargs) as client:
            return await client.messages.create(**PARAMS)

    if async_:
        result = asyncio.run(request())
    else:
        with anthropic.Anthropic(**kwargs) as client:
            result = client.messages.create(**PARAMS)
    assert result.id == "msg_mock"
    assert_usage(exporter)


def test_stream_usage_snapshots_preserve_cache_counts():
    from anthropic.types import RawMessageStartEvent, RawMessageDeltaEvent
    from traceai_anthropic._stream import _MessageResponseAccumulator, _MessageResponseExtractor

    accumulator = _MessageResponseAccumulator()
    start = RawMessageStartEvent.model_validate(EVENTS[0])
    accumulator.merge(accumulator.extract(start))
    for output in (1, 2):
        delta = RawMessageDeltaEvent.model_validate({
            **EVENTS[-2], "usage": {"output_tokens": output, "cache_read_input_tokens": None}
        })
        accumulator.merge(accumulator.extract(delta))
    attrs = dict(_MessageResponseExtractor(accumulator).get_extra_attributes())
    assert attrs["gen_ai.usage.input_tokens"] == 12
    assert attrs["gen_ai.usage.output_tokens"] == 2
    assert attrs["gen_ai.usage.cache_write_tokens"] == 4
    assert attrs["gen_ai.usage.cache_read_tokens"] == 5


@pytest.mark.parametrize("async_", [False, True])
def test_raw_context_close_finalizes_partial_parsed_stream(harness, async_):
    provider, exporter, instrumentor, kwargs = harness
    instrumentor.instrument(tracer_provider=provider)

    async def request():
        async with anthropic.AsyncAnthropic(**kwargs) as client:
            async with client.messages.with_streaming_response.create(**PARAMS, stream=True) as raw:
                stream = await raw.parse()
                assert (await stream.__anext__()).type == "message_start"
                assert not exporter.get_finished_spans()
            assert len(exporter.get_finished_spans()) == 1
            await stream.close()

    if async_:
        asyncio.run(request())
    else:
        with anthropic.Anthropic(**kwargs) as client:
            with client.messages.with_streaming_response.create(**PARAMS, stream=True) as raw:
                stream = raw.parse()
                assert next(stream).type == "message_start"
                assert not exporter.get_finished_spans()
            assert len(exporter.get_finished_spans()) == 1
            stream.close()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.UNSET
    assert spans[0].attributes["gen_ai.usage.input_tokens"] == 12
