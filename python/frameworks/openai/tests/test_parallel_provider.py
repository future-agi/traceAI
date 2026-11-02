"""Parallel Responses uses the shared OpenAI instrumentor, not a second patch.

Upstream route: https://docs.parallel.ai/responses-api/responses-quickstart
SDK calls use MockTransport. Only the loopback OTLP receiver opens a socket.
JSON is a test exporter using the upstream encoder, not a new product transport.
"""

import asyncio
import base64
import gzip
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import openai
import pytest
import requests
from fi_instrumentation import TraceConfig
from google.protobuf.json_format import MessageToDict
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from traceai_openai import OpenAIInstrumentor
from traceai_openai._providers import provider_from_host

from .test_openai_compatible_spans import async_client, client, responses_body, unauthorized

PROMPT = "synthetic-parallel-private-question"
ANSWER = "synthetic-parallel-private-answer"
BASE_URL = "https://api.parallel.ai/v1"


def otlp_json(message):
    payload = MessageToDict(message, use_integers_for_enums=True)

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"traceId", "spanId", "parentSpanId"}:
                    value[key] = base64.b64decode(child).hex()
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return payload


class JsonExporter(SpanExporter):
    def __init__(self, endpoint, session, compress):
        self.endpoint, self.session, self.compress = endpoint, session, compress

    def export(self, spans):
        body = json.dumps(otlp_json(encode_spans(spans))).encode()
        headers = {"Content-Type": "application/json"}
        if self.compress:
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
        response = self.session.post(self.endpoint, data=body, headers=headers, timeout=3)
        response.raise_for_status()
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


@pytest.fixture
def wire_tracer():
    payloads = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            payload = (json.loads(body) if self.headers["Content-Type"] == "application/json"
                       else otlp_json(ExportTraceServiceRequest.FromString(body)))
            payloads.append((self.path, self.headers, payload))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    provider = TracerProvider()
    session = requests.Session()
    session.trust_env = False
    instrumentor = OpenAIInstrumentor()

    def configure(encoding, compress, hidden):
        endpoint = f"http://127.0.0.1:{server.server_port}/v1/traces"
        exporter = (JsonExporter(endpoint, session, compress) if encoding == "json" else
                    OTLPSpanExporter(endpoint=endpoint, session=session, timeout=3,
                                     compression=Compression.Gzip if compress else Compression.NoCompression))
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        instrumentor.instrument(tracer_provider=provider, config=TraceConfig(
            hide_inputs=hidden, hide_outputs=hidden, hide_llm_invocation_parameters=hidden,
        ))
        return payloads

    try:
        yield configure
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
        session.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def stream_response():
    completed = responses_body(text=ANSWER, model="parallel")
    events = [
        {"type": "response.created", "sequence_number": 0,
         "response": {**completed, "status": "in_progress", "output": []}},
        {"type": "response.output_text.delta", "sequence_number": 1, "item_id": "msg_1",
         "output_index": 0, "content_index": 0, "delta": ANSWER, "logprobs": []},
        {"type": "response.completed", "sequence_number": 2, "response": completed},
    ]
    body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


@pytest.mark.parametrize("encoding", ["protobuf", "json"])
@pytest.mark.parametrize("compress", [False, True], ids=["plain", "gzip"])
@pytest.mark.parametrize("hidden", [False, True], ids=["content", "masked"])
@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("outcome", ["success", "stream", "error"])
def test_parallel_responses_wire(wire_tracer, encoding, compress, hidden, use_async, outcome):
    payloads = wire_tracer(encoding, compress, hidden)
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.path == "/v1/responses"
        assert json.loads(request.content)["input"] == PROMPT
        if outcome == "error":
            return unauthorized(request)
        if outcome == "stream":
            return stream_response()
        return httpx.Response(200, json=responses_body(text=ANSWER, model="parallel"))

    kwargs = dict(model="parallel", input=PROMPT, stream=outcome == "stream")

    async def run_async():
        async with async_client(handler, base_url=BASE_URL) as sdk:
            response = await sdk.responses.create(**kwargs)
            if outcome == "stream":
                assert "".join([event.delta async for event in response
                                if event.type == "response.output_text.delta"]) == ANSWER
            else:
                assert response.output_text == ANSWER

    def run_sync():
        with client(handler, base_url=BASE_URL) as sdk:
            response = sdk.responses.create(**kwargs)
            if outcome == "stream":
                assert "".join(event.delta for event in response
                               if event.type == "response.output_text.delta") == ANSWER
            else:
                assert response.output_text == ANSWER

    def run():
        asyncio.run(run_async()) if use_async else run_sync()

    if outcome == "error":
        with pytest.raises(openai.AuthenticationError):
            run()
    else:
        run()
    assert len(seen) == len(payloads) == 1
    path, headers, payload = payloads[0]
    assert path == "/v1/traces"
    assert headers.get("Content-Encoding") == ("gzip" if compress else None)
    assert headers["Content-Type"] == ("application/json" if encoding == "json" else "application/x-protobuf")
    spans = [span for resource in payload["resourceSpans"]
             for scope in resource["scopeSpans"] for span in scope["spans"]]
    assert len(spans) == 1
    span = spans[0]
    attrs = {a["key"]: a["value"] for a in span["attributes"]}
    assert attrs["gen_ai.provider.name"] == {"stringValue": "parallel"}
    assert attrs["gen_ai.request.model"] == {"stringValue": "parallel"}
    assert attrs["gen_ai.span.kind"] == {"stringValue": "LLM"}
    assert span["status"]["code"] == (2 if outcome == "error" else 1)
    if outcome != "error":
        assert attrs["gen_ai.usage.input_tokens"] == {"intValue": "5"}
        assert attrs["gen_ai.usage.output_tokens"] == {"intValue": "2"}
    wire = json.dumps(payload)
    assert "test-vendor-key" not in wire
    assert (PROMPT in wire) == (not hidden)
    if hidden:
        assert ANSWER not in wire


@pytest.mark.parametrize("host,expected", [
    ("api.parallel.ai", "parallel"), ("API.PARALLEL.AI.", "parallel"),
    ("api.parallel.ai.customer.example", "openai"), ("proxy.api.parallel.ai", "openai"),
    ("notapi.parallel.ai", "openai"), ("parallel.ai", "openai"),
    ("localhost", "openai"), ("api.openai.com", "openai"), (None, "openai"),
])
def test_parallel_exact_host(host, expected):
    assert provider_from_host(host) == expected


def test_parallel_readme_responses_snippet():
    import os
    import subprocess
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
    from harness import Receiver, _flatten_attributes

    readme = Path(__file__).resolve().parents[2] / "parallel" / "README.md"
    snippet = readme.read_text().split("## Trace the Responses API")[1].split("```python\n")[1].split("```")[0]
    # Inject only an HTTP transport, then execute the README verbatim. The real
    # SDK, instrumentor, register() and OTLP exporter all run in the subprocess.
    bootstrap = '''
import httpx
import openai
_original_init = openai.OpenAI.__init__
def handler(request):
    assert request.url.host == "api.parallel.ai"
    assert request.url.path == "/v1/responses"
    return httpx.Response(200, json=RESPONSE_BODY)
def init(self, *args, **kwargs):
    kwargs.update(http_client=httpx.Client(transport=httpx.MockTransport(handler), trust_env=False), max_retries=0)
    _original_init(self, *args, **kwargs)
openai.OpenAI.__init__ = init
'''.replace("RESPONSE_BODY", repr(responses_body(text=ANSWER, model="parallel")))
    with Receiver() as receiver:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FI_", "PARALLEL_", "OPENAI_", "OTEL_"))}
        env.update(FI_BASE_URL=receiver.origin, FI_API_KEY="synthetic-fi-key",
                   FI_SECRET_KEY="synthetic-fi-secret", PARALLEL_API_KEY="synthetic-parallel-key")
        result = subprocess.run([sys.executable, "-I", "-c", bootstrap + "\n" + snippet],
                                env=env, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr.decode()
        spans = receiver.spans()
        assert len(spans) == 1
        attrs = _flatten_attributes(spans[0]["attributes"])
        assert attrs["gen_ai.provider.name"] == "parallel"
        assert attrs["gen_ai.request.model"] == "parallel"
        assert ANSWER in json.dumps(spans)
        assert "synthetic-parallel-key" not in json.dumps(spans)
        assert all(r["path"] == "/tracer/v1/traces" for r in receiver.requests())
