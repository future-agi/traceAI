"""Installed-wheel zero-code client; --decode reads captured OTLP protobuf offline."""
import base64
import http.client
import json
import os
import sys
from urllib.parse import urlsplit


def decode():
    from google.protobuf.json_format import MessageToDict
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    # The collector returns success to the real exporter before this offline decode.
    # Use the installed OTLP schema, not a test-only exporter or a hand-written codec.
    envelopes = []
    for body in json.load(sys.stdin):
        request = ExportTraceServiceRequest.FromString(base64.b64decode(body))
        envelope = MessageToDict(request, use_integers_for_enums=True)
        for resource in envelope.get("resourceSpans", []):
            for scope in resource.get("scopeSpans", []):
                for span in scope.get("spans", []):
                    for key in ("traceId", "spanId", "parentSpanId"):
                        if key in span:
                            span[key] = base64.b64decode(span[key]).hex()
        envelopes.append(envelope)
    print(json.dumps(envelopes))


def client():
    from opentelemetry import trace
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    from fi_instrumentation.auto import flush, shutdown
    from fi_instrumentation.auto import _manager

    provider = trace.get_tracer_provider()
    # Do not call init/register here: a missing wheel/.pth must fail, not silently
    # turn this into the one-line bootstrap test. No sampler/exporter is replaced.
    assert _manager._state.owned_provider and _manager._state.provider is provider, (
        "FI_AUTO_INSTRUMENT did not bootstrap: install the local Python wheel (not just PYTHONPATH)"
    )
    trace_id = int(os.environ["TEST_TRACE_ID"], 16)
    original = provider.id_generator.generate_trace_id
    provider.id_generator.generate_trace_id = lambda: trace_id
    target = urlsplit(sys.argv[1])
    assert target.scheme == "http" and target.hostname == "127.0.0.1"
    connection = http.client.HTTPConnection(target.hostname, target.port, timeout=10)
    try:
        # An AI root is admitted by the agent's normal AI-only export route.
        with provider.get_tracer("ratio-cross-language").start_as_current_span(
            "python-root", attributes={"gen_ai.span.kind": "AGENT"}
        ) as span:
            context = span.get_span_context()
            headers = {}
            TraceContextTextMapPropagator().inject(headers)
            connection.request("GET", "/ratio", headers=headers)
            response = connection.getresponse()
            received = json.loads(response.read())
            assert response.status == 200, received
            result = {"traceId": format(context.trace_id, "032x"),
                      "spanId": format(context.span_id, "016x"),
                      "sampled": bool(context.trace_flags.sampled),
                      "recording": span.is_recording(), "sent": headers, "received": received}
        assert flush(timeout_millis=5000), "Python exporter did not flush"
    finally:
        connection.close()
        provider.id_generator.generate_trace_id = original
        shutdown(timeout_millis=5000)
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "--decode":
        decode()
    else:
        client()
