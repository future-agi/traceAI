"""Exercise SDK gRPC operations against an in-process synthetic stub."""

import pytest
from opentelemetry import trace
from opentelemetry.trace import SpanKind

from .test_framework_a2a import CURRENT, SECRET, call_options, request, task
from .test_framework_a2a import tracing as tracing  # noqa: PLC0414

pytestmark = pytest.mark.skipif(not CURRENT, reason="protobuf 1.x gRPC API")


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_grpc_native_span_and_metadata(tracing, streaming):
    pytest.importorskip("grpc")
    from a2a.client.transports.grpc import GrpcTransport
    from a2a.types import SendMessageResponse, StreamResponse
    from google.protobuf.json_format import ParseDict

    provider, exporter = tracing
    captured = []

    class Stub:
        async def SendMessage(self, request, **kwargs):
            captured.append(kwargs)
            return ParseDict(task(), SendMessageResponse())

        def SendStreamingMessage(self, request, **kwargs):
            captured.append(kwargs)

            class StreamCall:
                async def read(self):
                    return ParseDict(task(), StreamResponse())

            return StreamCall()

    # No channel/network: only the generated stub is replaced. The installed
    # SDK transport, telemetry decorator and protobuf messages are real.
    transport = object.__new__(GrpcTransport)
    transport.stub = Stub()
    options, headers = call_options()
    with provider.get_tracer("test").start_as_current_span("parent") as parent:
        if streaming:
            stream = transport.send_message_streaming(request(), **options)
            assert not exporter.get_finished_spans()
            await stream.__anext__()
            assert not exporter.get_finished_spans()
            assert trace.get_current_span() is parent
            await stream.aclose()
        else:
            await transport.send_message(request(), **options)
    outbound = [
        span for span in exporter.get_finished_spans() if span.kind == SpanKind.CLIENT
    ]
    assert len(outbound) == 1
    span = outbound[0]
    assert span.attributes["gen_ai.a2a.task.state"] == "completed"
    metadata = dict(captured[0]["metadata"])
    assert metadata["traceparent"].split("-")[2] == format(span.context.span_id, "016x")
    assert metadata["authorization"] == SECRET
    assert captured[0]["timeout"] == 7
    assert headers["Traceparent"] == "stale"
    assert SECRET not in span.to_json()
