"""Offline Aleph Alpha SDK harness: the real SDK clients against a loopback HTTP stub, no external network."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from aleph_alpha_client import AsyncClient, Client
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

TOKEN = "synthetic-aleph-token-DO-NOT-EXPORT"


class Reply:
    def __init__(self, status=200, body=None, raw=None, content_type="application/json"):
        self.status, self.content_type = status, content_type
        self.raw = raw if raw is not None else json.dumps(body if body is not None else {}).encode()


def sse(*events, tail=b"data: [DONE]\n\n"):
    out = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events) + tail
    return Reply(raw=out, content_type="text/event-stream")


class Stub:
    """A loopback server; ``handler(path, body)`` returns a Reply (or a dict for a 200 JSON reply)."""

    def __init__(self):
        self.handler = lambda path, body: Reply(404, {"error": "unrouted " + path})
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(size) or b"{}")
                path = self.path.split("?")[0]
                stub.requests.append((path, body, dict(self.headers)))
                reply = stub.handler(path, body)
                if not isinstance(reply, Reply):
                    reply = Reply(200, reply)
                self.send_response(reply.status)
                self.send_header("Content-Type", reply.content_type)
                self.send_header("Content-Length", str(len(reply.raw)))
                self.end_headers()
                self.wfile.write(reply.raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def reply(self, value):
        self.handler = lambda path, body: value

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub():
    server = Stub()
    yield server
    server.close()


@pytest.fixture
def client(stub):
    c = Client(token=TOKEN, host=stub.url, total_retries=0)
    yield c
    c.session.close()


@pytest.fixture
def aclient(stub):
    return lambda: AsyncClient(token=TOKEN, host=stub.url, total_retries=0)


def completion_body(text="Tokyo", finish="maximum_tokens"):
    return {
        "model_version": "luminous-served",
        "completions": [{"completion": text, "finish_reason": finish}],
        "num_tokens_prompt_total": 4,
        "num_tokens_generated": 2,
    }


def chat_body(text="Tokyo", finish="stop", **message):
    msg = {"role": "assistant", "content": text}
    msg.update(message)
    return {
        "id": "chat-1",
        "model": "pharia-served",
        "choices": [{"index": 0, "finish_reason": finish, "message": msg}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def chat_chunk(content="", role=None, finish=None, usage=None, tool_calls=None):
    delta = {"content": content}
    if role:
        delta["role"] = role
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    body = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        body = {"choices": [], "usage": usage}
    return body


def attrs(span):
    return dict(span.attributes)


def blob(span):
    return json.dumps(attrs(span), default=str)


@pytest.fixture
def provider():
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer_provider.exporter = exporter
    yield tracer_provider
    tracer_provider.shutdown()


@pytest.fixture
def instrument(provider):
    from traceai_alephalpha import AlephAlphaInstrumentor

    instrumentors = []

    def start(**kwargs):
        instrumentor = AlephAlphaInstrumentor()
        instrumentor.instrument(tracer_provider=provider, **kwargs)
        instrumentors.append(instrumentor)
        return provider.exporter

    yield start
    for instrumentor in reversed(instrumentors):
        instrumentor.uninstrument()


@pytest.fixture
def spans(instrument):
    return instrument()
