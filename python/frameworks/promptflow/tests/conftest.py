"""Offline Prompt flow harness: real promptflow-tracing / promptflow-core, in-memory exporter."""
import json
import os
import textwrap

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

SECRET_INPUT = "synthetic-input-DO-NOT-EXPORT"
SECRET_ERROR = "synthetic-error-message-DO-NOT-EXPORT"


def attrs(span):
    return dict(span.attributes)


def blob(span):
    """Every attribute, event name and event attribute of a span, as one string."""
    events = [(e.name, dict(e.attributes or {})) for e in span.events]
    return json.dumps([attrs(span), events, span.status.description], default=str)


def by_name(spans, name):
    found = [s for s in spans if s.name == name or s.name.endswith("." + name)]
    assert len(found) == 1, (name, [s.name for s in spans])
    return found[0]


def write_dag_flow(root, fail=False):
    """A two-node DAG flow: greet -> shout. ``fail`` makes ``shout`` raise."""
    (root / "flow.dag.yaml").write_text(
        textwrap.dedent(
            """\
            inputs:
              question:
                type: string
            outputs:
              answer:
                type: string
                reference: ${shout.output}
            nodes:
            - name: greet
              type: python
              source:
                type: code
                path: greet.py
              inputs:
                question: ${inputs.question}
            - name: shout
              type: python
              source:
                type: code
                path: shout.py
              inputs:
                text: ${greet.output}
            """
        )
    )
    (root / "greet.py").write_text(
        "from promptflow.core import tool\n\n@tool\ndef greet(question: str) -> str:\n    return 'hello ' + question\n"
    )
    body = f"raise ValueError({SECRET_ERROR!r})" if fail else "return text.upper()"
    (root / "shout.py").write_text(
        f"from promptflow.core import tool\n\n@tool\ndef shout(text: str) -> str:\n    {body}\n"
    )
    return str(root / "flow.dag.yaml")


def write_flex_flow(root):
    """A flex flow whose entry calls one ``@trace`` helper."""
    (root / "flow.flex.yaml").write_text("entry: flexapp:answer\n")
    (root / "flexapp.py").write_text(
        "from promptflow.tracing import trace\n\n"
        "@trace\ndef helper(x):\n    return x * 2\n\n"
        "def answer(n: int) -> int:\n    return helper(n) + 1\n"
    )
    return str(root / "flow.flex.yaml")


@pytest.fixture
def provider():
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer_provider.exporter = exporter
    yield tracer_provider
    tracer_provider.shutdown()


@pytest.fixture(autouse=True)
def _tracing_env(monkeypatch):
    # promptflow ships with tracing off; the instrumentor must switch it on by itself.
    monkeypatch.delenv("PF_DISABLE_TRACING", raising=False)


@pytest.fixture
def instrument(provider):
    from traceai_promptflow import PromptflowInstrumentor

    instrumentors = []

    def start(**kwargs):
        instrumentor = PromptflowInstrumentor()
        instrumentor.instrument(tracer_provider=provider, **kwargs)
        instrumentors.append(instrumentor)
        return provider.exporter

    yield start
    for instrumentor in reversed(instrumentors):
        instrumentor.uninstrument()


@pytest.fixture
def spans(instrument):
    return instrument()


class _OpenAIStub:
    """A loopback chat-completions endpoint, for the Prompty client that builds its own HTTP client."""

    def __init__(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        stub = self
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                stub.requests.append(json.loads(self.rfile.read(size) or b"{}"))
                body = json.dumps({
                    "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "gpt-served",
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Tokyo"}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


TEMPLATE_MARKER = "synthetic-template-DO-NOT-EXPORT"


@pytest.fixture
def prompty(tmp_path):
    """A real Prompty file whose OpenAI endpoint is a loopback stub (no external network)."""
    stub = _OpenAIStub()
    path = tmp_path / "ask.prompty"
    path.write_text(
        textwrap.dedent(
            f"""\
            ---
            name: ask
            model:
              api: chat
              configuration:
                type: openai
                model: gpt-4o
                base_url: {stub.url}
                api_key: synthetic-openai-key-DO-NOT-EXPORT
              parameters:
                temperature: 0.2
                max_tokens: 16
            inputs:
              question:
                type: string
            ---
            system:
            {TEMPLATE_MARKER} answer briefly.
            user:
            {{{{question}}}}
            """
        )
    )
    yield str(path)
    stub.close()
