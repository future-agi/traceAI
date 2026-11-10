"""Offline Runtime contracts through real boto3/aiobotocore clients."""

import asyncio
from contextlib import AsyncExitStack
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import boto3
import pytest
import pytest_asyncio
import requests
from botocore.stub import Stubber
from opentelemetry import context, trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import SpanAttributes as SA
from traceai_bedrock import BedrockInstrumentor

REQUEST = {
    "modelId": "offline-model",
    "messages": [{"role": "user", "content": [{"text": "private prompt"}]}],
}
EVENTS = [
    {"messageStart": {"role": "assistant"}},
    {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "private "}}},
    {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "answer"}}},
    {"contentBlockStop": {"contentBlockIndex": 0}},
    {
        "contentBlockStart": {
            "contentBlockIndex": 1,
            "start": {"toolUse": {"toolUseId": "call-1", "name": "lookup"}},
        }
    },
    {
        "contentBlockDelta": {
            "contentBlockIndex": 1,
            "delta": {"toolUse": {"input": '{"city":"private '}},
        }
    },
    {
        "contentBlockDelta": {
            "contentBlockIndex": 1,
            "delta": {"toolUse": {"input": 'city"}'}},
        }
    },
    {"contentBlockStop": {"contentBlockIndex": 1}},
    {"messageStop": {"stopReason": "tool_use"}},
    {
        "metadata": {
            "usage": {
                "inputTokens": 0,
                "outputTokens": 3,
                "totalTokens": 3,
                "cacheReadInputTokens": 2,
                "cacheWriteInputTokens": 0,
            },
            "metrics": {"latencyMs": 1},
        }
    },
]
RESPONSE = {
    "output": {
        "message": {
            "role": "assistant",
            "content": [
                {"text": "private answer"},
                {
                    "toolUse": {
                        "toolUseId": "call-1",
                        "name": "lookup",
                        "input": {"city": "private city"},
                    }
                },
            ],
        }
    },
    "stopReason": "tool_use",
    "usage": EVENTS[-1]["metadata"]["usage"],
    "metrics": {"latencyMs": 1},
}


class Stream:
    def __init__(self, events=(), error=None, close_error=None):
        self.events = list(events)
        self.error = error
        self.close_error = close_error
        self.reads = 0
        self.closes = 0
        self.contexts = []

    def __iter__(self):
        for event in self.events:
            self.reads += 1
            self.contexts.append(trace.get_current_span().get_span_context())
            yield event
        if self.error:
            raise self.error

    async def __aiter__(self):
        for event in self:
            await asyncio.sleep(0)
            yield event

    def close(self):
        self.closes += 1
        if self.close_error:
            raise self.close_error
        return "closed"


@pytest_asyncio.fixture
async def runtime(monkeypatch):
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    async with AsyncExitStack() as stack:

        async def invoke(
            mode,
            raw=None,
            *,
            operation="converse_stream",
            config=None,
            error=None,
            request=None,
        ):
            BedrockInstrumentor().instrument(
                tracer_provider=provider, config=config or TraceConfig()
            )
            options = dict(
                region_name="us-east-1",
                aws_access_key_id="synthetic",
                aws_secret_access_key="synthetic",
                endpoint_url="https://offline.invalid",
            )
            if mode == "async":
                pytest.importorskip("aiobotocore")
                from aiobotocore.session import get_session

                client = await stack.enter_async_context(
                    get_session().create_client("bedrock-runtime", **options)
                )
            else:
                client = boto3.session.Session().client("bedrock-runtime", **options)
                stack.callback(client.close)
            # Stubber validates requests and service response shapes with the real SDK.
            # Substitute a consumable stream after validation, which Stubber cannot model.
            response = (
                deepcopy(RESPONSE)
                if operation == "converse"
                else {"stream": {"messageStop": {"stopReason": "end_turn"}}}
            )
            stub = Stubber(client)
            params = request if request is not None else REQUEST
            if error:
                stub.add_client_error(
                    operation,
                    service_error_code="ValidationException",
                    service_message="private failure",
                    expected_params=params,
                )
            else:
                stub.add_response(operation, response, params)
                if operation == "converse_stream":
                    response["stream"] = raw
            stack.enter_context(stub)
            result = getattr(client, operation)(**params)
            if mode == "async":
                result = await result
            stub.assert_no_pending_responses()
            return result

        yield invoke, provider, exporter
    BedrockInstrumentor().uninstrument()
    provider.shutdown()


async def consume(mode, stream):
    return [event async for event in stream] if mode == "async" else list(stream)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_complete_stream(runtime, mode):
    invoke, provider, exporter = runtime
    raw = Stream(EVENTS)
    with provider.get_tracer("test").start_as_current_span("parent") as parent:
        response = await invoke(mode, raw)
        assert raw.reads == 0
        assert not exporter.get_finished_spans()
        assert trace.get_current_span() is parent
        actual = await consume(mode, response["stream"])
        assert all(a is b for a, b in zip(actual, raw.events))
        assert trace.get_current_span() is parent
        (span,) = exporter.get_finished_spans()
        assert span.parent.span_id == parent.get_span_context().span_id
        assert all(c.span_id == span.context.span_id for c in raw.contexts)
        assert span.name == "bedrock.converse_stream"
        assert span.status.status_code == StatusCode.OK
        attrs = span.attributes
        assert attrs[SA.GEN_AI_PROVIDER_NAME] == "aws"
        assert attrs[SA.GEN_AI_REQUEST_MODEL] == "offline-model"
        assert attrs[SA.OUTPUT_VALUE] == "private answer"
        assert attrs[SA.GEN_AI_USAGE_INPUT_TOKENS] == 0
        assert attrs[SA.GEN_AI_USAGE_OUTPUT_TOKENS] == 3
        assert attrs[SA.GEN_AI_USAGE_TOTAL_TOKENS] == 3
        assert attrs[SA.GEN_AI_USAGE_CACHE_READ_TOKENS] == 2
        assert attrs[SA.GEN_AI_USAGE_CACHE_WRITE_TOKENS] == 0
        assert attrs[SA.GEN_AI_RESPONSE_FINISH_REASONS] == ("tool_use",)
        prefix = f"{SA.GEN_AI_OUTPUT_MESSAGES}.0.message.tool_calls.0.tool_call"
        assert attrs[f"{prefix}.id"] == "call-1"
        assert attrs[f"{prefix}.function.name"] == "lookup"
        assert attrs[f"{prefix}.function.arguments"] == '{"city":"private city"}'
        response["stream"].close()
        assert len(exporter.get_finished_spans()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("consumed", [0, 2, 9])
async def test_close_without_draining(runtime, mode, consumed):
    invoke, _, exporter = runtime
    raw = Stream(EVENTS)
    stream = (await invoke(mode, raw))["stream"]
    iterator = aiter(stream) if mode == "async" else iter(stream)
    for _ in range(consumed):
        await anext(iterator) if mode == "async" else next(iterator)
    assert not exporter.get_finished_spans()  # messageStop precedes usage metadata
    assert stream.close() == "closed"
    stream.close()
    assert raw.reads == consumed
    (span,) = exporter.get_finished_spans()
    assert span.attributes.get(SA.OUTPUT_VALUE) == (
        None if consumed == 0 else "private " if consumed == 2 else "private answer"
    )
    assert SA.GEN_AI_USAGE_INPUT_TOKENS not in span.attributes


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("where", ["request", "iterate", "close", "cancel"])
async def test_errors_end_once_and_propagate(runtime, mode, where):
    invoke, _, exporter = runtime
    error = (
        asyncio.CancelledError("private failure")
        if where == "cancel"
        else RuntimeError("private failure")
    )
    raw = Stream(
        EVENTS[:3],
        error=error if where in ("iterate", "cancel") else None,
        close_error=error if where == "close" else None,
    )
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError if where == "request" else type(error)) as caught:
        stream = (await invoke(mode, raw, error=where == "request"))["stream"]
        if where == "close":
            stream.close()
        else:
            await consume(mode, stream)
    if where != "request":
        assert caught.value is error
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert len(span.events) == 1
    assert not trace.get_current_span().get_span_context().is_valid
    if where in ("iterate", "cancel"):
        stream.close()
        assert len(exporter.get_finished_spans()) == 1
        assert span.attributes[SA.OUTPUT_VALUE] == "private answer"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_nonstream_converse(runtime, mode):
    invoke, _, exporter = runtime
    response = await invoke(mode, operation="converse")
    assert response == RESPONSE
    (span,) = exporter.get_finished_spans()
    assert span.name == "bedrock.converse"
    assert span.attributes[SA.GEN_AI_USAGE_INPUT_TOKENS] == 0
    assert span.attributes[SA.OUTPUT_VALUE] == "private answer"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_empty_and_prompt_management_request(runtime, mode):
    invoke, _, exporter = runtime
    stream = (await invoke(mode, Stream(), request={"modelId": "offline-prompt-arn"}))[
        "stream"
    ]
    assert await consume(mode, stream) == []
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.OK


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_suppression(runtime, mode):
    invoke, _, exporter = runtime
    raw = Stream(EVENTS)
    token = context.attach(context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        assert (await invoke(mode, raw))["stream"] is raw
    finally:
        context.detach(token)
    assert not exporter.get_finished_spans()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize("outcome", ["complete", "close", "error"])
async def test_otlp_wire_and_privacy(runtime, mode, hidden, outcome):
    invoke, provider, _ = runtime
    payloads = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payloads.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = requests.Session()
    session.trust_env = False
    provider.add_span_processor(
        SimpleSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces",
                session=session,
                timeout=3,
            )
        )
    )
    try:
        raw = Stream(
            EVENTS,
            error=RuntimeError("private failure") if outcome == "error" else None,
        )
        stream = (
            await invoke(
                mode, raw, config=TraceConfig(hide_inputs=hidden, hide_outputs=hidden)
            )
        )["stream"]
        if outcome == "close":
            iterator = aiter(stream) if mode == "async" else iter(stream)
            for _ in range(7):
                await anext(iterator) if mode == "async" else next(iterator)
            stream.close()
        elif outcome == "error":
            with pytest.raises(RuntimeError):
                await consume(mode, stream)
        else:
            await consume(mode, stream)
        provider.force_flush()
        assert len(payloads) == 1
        wire = ExportTraceServiceRequest.FromString(payloads[0])
        spans = [
            s
            for r in wire.resource_spans
            for scope in r.scope_spans
            for s in scope.spans
        ]
        assert len(spans) == 1
        assert spans[0].name == "bedrock.converse_stream"
        assert spans[0].status.code == (2 if outcome == "error" else 1)
        if hidden:
            assert b"private" not in payloads[0]
        else:
            for value in (b"private prompt", b"private answer", b"private city"):
                assert value in payloads[0]
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_real_eventstream_parser(runtime, mode):
    """Exercise the SDK's actual event framing/parsing, not just a list iterator."""
    import json
    import struct
    from zlib import crc32
    from botocore.eventstream import EventStream
    from botocore.parsers import EventStreamJSONParser
    from botocore.session import get_session

    def frame(event):
        name, value = next(iter(event.items()))
        headers = b""
        for key, value_string in (
            (":message-type", "event"),
            (":event-type", name),
            (":content-type", "application/json"),
        ):
            key_bytes, value_bytes = key.encode(), value_string.encode()
            headers += (
                bytes([len(key_bytes)])
                + key_bytes
                + b"\x07"
                + struct.pack("!H", len(value_bytes))
                + value_bytes
            )
        payload = json.dumps(value).encode()
        prelude = struct.pack("!II", len(headers) + len(payload) + 16, len(headers))
        message = prelude + struct.pack("!I", crc32(prelude)) + headers + payload
        return message + struct.pack("!I", crc32(message))

    class Raw:
        closed = False

        def stream(self):
            for event in EVENTS:
                encoded = frame(event)
                # Split frame headers and bodies across transport reads.
                yield encoded[:9]
                yield encoded[9:]

        async def iter_chunks(self):
            for data in self.stream():
                yield data, False

        @property
        def content(self):
            return self

        def close(self):
            self.closed = True

    shape = (
        get_session()
        .get_service_model("bedrock-runtime")
        .operation_model("ConverseStream")
        .output_shape.members["stream"]
    )
    raw = Raw()
    if mode == "async":
        pytest.importorskip("aiobotocore")
        from aiobotocore.eventstream import AioEventStream
        from aiobotocore.parsers import AioEventStreamJSONParser

        source = AioEventStream(
            raw, shape, AioEventStreamJSONParser(), "ConverseStream"
        )
    else:
        source = EventStream(raw, shape, EventStreamJSONParser(), "ConverseStream")
    invoke, _, exporter = runtime
    stream = (await invoke(mode, source))["stream"]
    assert await consume(mode, stream) == EVENTS
    stream.close()
    assert raw.closed
    (span,) = exporter.get_finished_spans()
    assert span.attributes[SA.OUTPUT_VALUE] == "private answer"
    assert span.attributes[SA.GEN_AI_USAGE_TOTAL_TOKENS] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_malformed_or_new_events_do_not_change_sdk_output(runtime, mode):
    invoke, _, exporter = runtime
    events = [
        {"futureEvent": {"private": "data"}},
        {"contentBlockDelta": {"delta": {"text": "malformed"}}},
        *EVENTS,
    ]
    raw = Stream(events)
    stream = (await invoke(mode, raw))["stream"]
    assert await consume(mode, stream) == events
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.OK
    assert span.attributes[SA.OUTPUT_VALUE] == "private answer"


@pytest.mark.asyncio
async def test_actual_task_cancellation(runtime):
    pytest.importorskip("aiobotocore")
    invoke, _, exporter = runtime
    reading = asyncio.Event()

    class Waiting(Stream):
        async def __aiter__(self):
            yield EVENTS[1]
            reading.set()
            await asyncio.Event().wait()

    stream = (await invoke("async", Waiting()))["stream"]
    task = asyncio.create_task(consume("async", stream))
    await asyncio.wait_for(reading.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stream.close()
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes[SA.OUTPUT_VALUE] == "private "
    assert not trace.get_current_span().get_span_context().is_valid


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_multiple_interleaved_blocks(runtime, mode):
    invoke, _, exporter = runtime
    events = [
        {
            "contentBlockStart": {
                "contentBlockIndex": 2,
                "start": {"toolUse": {"toolUseId": "b", "name": "second"}},
            }
        },
        {
            "contentBlockStart": {
                "contentBlockIndex": 1,
                "start": {"toolUse": {"toolUseId": "a", "name": "first"}},
            }
        },
        {
            "contentBlockDelta": {
                "contentBlockIndex": 2,
                "delta": {"toolUse": {"input": '{"b":'}},
            }
        },
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "text"}}},
        {
            "contentBlockDelta": {
                "contentBlockIndex": 1,
                "delta": {"toolUse": {"input": '{"a":1}'}},
            }
        },
        {
            "contentBlockDelta": {
                "contentBlockIndex": 2,
                "delta": {"toolUse": {"input": "2}"}},
            }
        },
    ]
    stream = (await invoke(mode, Stream(events)))["stream"]
    assert await consume(mode, stream) == events
    (span,) = exporter.get_finished_spans()
    prefix = f"{SA.GEN_AI_OUTPUT_MESSAGES}.0.message.tool_calls"
    assert span.attributes[f"{prefix}.0.tool_call.function.arguments"] == '{"a":1}'
    assert span.attributes[f"{prefix}.1.tool_call.function.arguments"] == '{"b":2}'


def test_uninstrument_restores_client_creators():
    from botocore.client import ClientCreator

    original = ClientCreator.create_client
    try:
        from aiobotocore.client import AioClientCreator

        aio_original = AioClientCreator.create_client
    except ImportError:
        AioClientCreator = None
    provider = TracerProvider()
    try:
        instrumentor = BedrockInstrumentor()
        instrumentor.instrument(tracer_provider=provider)
        assert ClientCreator.create_client is not original
        if AioClientCreator:
            assert AioClientCreator.create_client is not aio_original
        instrumentor.uninstrument()
        assert ClientCreator.create_client is original
        if AioClientCreator:
            assert AioClientCreator.create_client is aio_original
    finally:
        BedrockInstrumentor().uninstrument()
        provider.shutdown()
