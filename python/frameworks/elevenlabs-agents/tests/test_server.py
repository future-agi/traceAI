"""The real HTTP server and the real OTLP/HTTP JSON exporter, both on loopback."""

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import FI_API_KEY, FI_SECRET_KEY, WEBHOOK_SECRET, load_fixture, make_config, raw
from traceai_elevenlabs_agents.exporter import ExportError, OTLPJsonExporter, traces_endpoint
from traceai_elevenlabs_agents.forwarder import Forwarder
from traceai_elevenlabs_agents.server import make_server
from traceai_elevenlabs_agents.store import ForwardedStore
from traceai_elevenlabs_agents.verify import sign


class Collector:
    """Loopback stand-in for the Future AGI collector's OTLP/HTTP route."""

    def __init__(self, status=200, body=b'{"partialSuccess":{}}'):
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                data = self.rfile.read(int(self.headers["Content-Length"]))
                owner.requests.append(
                    {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": data}
                )
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(owner.body)))
                self.end_headers()
                self.wfile.write(owner.body)

            def log_message(self, *args):
                pass

        self.status, self.body = status, body
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = "http://127.0.0.1:%d" % self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def stack():
    collector = Collector()
    config = make_config(fi_base_url=collector.base_url)
    forwarder = Forwarder(config, store=ForwardedStore(":memory:"))
    httpd = make_server(forwarder, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d" % httpd.server_port
    yield url, collector, forwarder
    httpd.shutdown()
    httpd.server_close()
    collector.close()
    forwarder.store.close()


def post(url, body, headers=None):
    request = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def test_end_to_end_over_loopback(stack):
    url, collector, _ = stack
    body = raw(load_fixture("post_call_transcription_otel.json"))
    status, payload = post(
        url + "/elevenlabs/webhook",
        body,
        {"Content-Type": "application/json", "ElevenLabs-Signature": sign(WEBHOOK_SECRET, body)},
    )
    assert status == 200 and payload["status"] == "forwarded"
    assert len(collector.requests) == 1
    sent = collector.requests[0]
    assert sent["path"] == "/tracer/v1/traces"
    assert sent["headers"]["x-api-key"] == FI_API_KEY
    assert sent["headers"]["x-secret-key"] == FI_SECRET_KEY
    assert sent["headers"]["content-type"] == "application/json"
    assert "content-encoding" not in sent["headers"]  # uncompressed: the collector rejects gzip
    exported = json.loads(sent["body"])
    assert len(exported["resourceSpans"][0]["scopeSpans"][0]["spans"]) == 8


def test_signature_failure_over_http(stack):
    url, collector, _ = stack
    body = raw(load_fixture("post_call_transcription_otel.json"))
    status, payload = post(url + "/elevenlabs/webhook", body, {"ElevenLabs-Signature": sign("nope", body)})
    assert (status, payload) == (401, {"error": "invalid_signature"})
    assert collector.requests == []


def test_routes_and_limits(stack):
    url, _, _ = stack
    with urllib.request.urlopen(url + "/healthz", timeout=5) as response:
        assert response.status == 200
    assert post(url + "/other", b"{}")[0] == 404
    request = urllib.request.Request(url + "/elevenlabs/webhook", data=b"x", method="PUT")
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(request, timeout=5)
    assert info.value.code == 405


def test_body_size_cap():
    forwarder = Forwarder(make_config(), store=ForwardedStore(":memory:"))
    httpd = make_server(forwarder, "127.0.0.1", 0, max_body_bytes=10)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        status, payload = post("http://127.0.0.1:%d/elevenlabs/webhook" % httpd.server_port, b"x" * 11)
        assert (status, payload) == (413, {"error": "payload_too_large"})
    finally:
        httpd.shutdown()
        httpd.server_close()
        forwarder.store.close()


def test_exporter_maps_collector_errors():
    collector = Collector(status=401, body=b'{"error":"bad key"}')
    try:
        exporter = OTLPJsonExporter(traces_endpoint(collector.base_url), "k", "s")
        with pytest.raises(ExportError) as info:
            exporter.export({"resourceSpans": []})
        assert info.value.status == 401
        collector.status, collector.body = 200, b'{"partialSuccess":{"rejectedSpans":"2","errorMessage":"x"}}'
        with pytest.raises(ExportError) as info:
            exporter.export({"resourceSpans": []})
        assert "rejected 2" in info.value.detail
    finally:
        collector.close()
    with pytest.raises(ExportError) as info:
        OTLPJsonExporter("http://127.0.0.1:9/tracer/v1/traces", "k", "s", timeout=2).export({})
    assert "unreachable" in info.value.detail


def test_traces_endpoint_matches_fi_instrumentation():
    assert traces_endpoint("https://api.futureagi.com") == "https://api.futureagi.com/tracer/v1/traces"
    assert traces_endpoint("http://localhost:14338/") == "http://localhost:14338/tracer/v1/traces"
    with pytest.raises(ValueError):
        traces_endpoint("localhost:14338")
