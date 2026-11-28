"""Schema/wire conformance, not a claim the JSON-only receiver exports protobuf/gzip."""

import base64
import copy
import gzip
import json
import urllib.request

import pytest
from google.protobuf.json_format import MessageToDict, ParseDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from test_rag import CONTENT, configured_forwarder, deliver, rag_api  # noqa: F401
from test_server import Collector


def protobuf_json(payload, *, to_base64):
    """OTLP JSON uses hex IDs, unlike the protobuf library's standard JSON codec."""
    payload = copy.deepcopy(payload)

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("traceId", "spanId", "parentSpanId") and item:
                    value[key] = (
                        base64.b64encode(bytes.fromhex(item)).decode() if to_base64
                        else base64.b64decode(item).hex()
                    )
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    return payload


@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize("encoding", ["json", "protobuf"])
@pytest.mark.parametrize("compressed", [False, True])
def test_enriched_payload_and_privacy_survive_otlp_wire(rag_api, hidden, encoding, compressed):
    source, wire = Collector(), Collector()
    forwarder = configured_forwarder(rag_api, source, FI_HIDE_OUTPUTS=str(hidden).lower())
    try:
        assert deliver(forwarder, rag_api)[0] == 200
        payload = json.loads(source.requests[0]["body"])
        # Validate every generated field against the real OTLP schema in every leg.
        expected = ParseDict(protobuf_json(payload, to_base64=True), ExportTraceServiceRequest())
        data = json.dumps(payload).encode() if encoding == "json" else expected.SerializeToString()
        headers = {"Content-Type": "application/json" if encoding == "json" else "application/x-protobuf"}
        if compressed:
            data = gzip.compress(data)
            headers["Content-Encoding"] = "gzip"
        request = urllib.request.Request(wire.base_url + "/v1/traces", data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
        received = wire.requests[0]["body"]
        if compressed:
            received = gzip.decompress(received)
        if encoding == "protobuf":
            message = ExportTraceServiceRequest.FromString(received)
            assert message == expected
            decoded = protobuf_json(MessageToDict(message), to_base64=False)
        else:
            decoded = json.loads(received)
            assert ParseDict(protobuf_json(decoded, to_base64=True), ExportTraceServiceRequest()) == expected
        rendered = json.dumps(decoded)
        assert (CONTENT in rendered) is not hidden
        if hidden:
            assert "retrieval.documents" not in rendered
            assert rag_api.requests == []
        # Keep the production transport contract unchanged.
        assert source.requests[0]["headers"]["content-type"] == "application/json"
        assert "content-encoding" not in source.requests[0]["headers"]
    finally:
        forwarder.store.close()
        source.close()
        wire.close()
