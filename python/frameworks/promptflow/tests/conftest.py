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
