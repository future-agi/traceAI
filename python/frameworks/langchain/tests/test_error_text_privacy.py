"""Legacy and current cores hide error details without losing session mapping.

Visible failures retain stacktraces only in exception events, matching core's
error policy. Hidden failures must not leak through status, events or attributes.
"""

from __future__ import annotations

import contextlib

import pytest
from fi_instrumentation import TraceConfig
from langchain_core.runnables import RunnableLambda
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

MARKER = "secret-marker-9c41"


@pytest.fixture
def traced(request):
    from traceai_langchain import LangChainInstrumentor

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**request.param))
    try:
        yield exporter
    finally:
        with contextlib.suppress(Exception):
            instrumentor.uninstrument()


def _fail():
    def boom(_):
        raise RuntimeError(f"backend down {MARKER}")

    with pytest.raises(RuntimeError):
        RunnableLambda(boom).invoke(1)


def _dump(span) -> str:
    events = [(e.name, dict(e.attributes or {})) for e in span.events]
    return repr((span.status.description, events, dict(span.attributes or {})))


@pytest.mark.parametrize("traced", [{}, {"hide_inputs": True, "hide_outputs": True}], indirect=True)
def test_status_never_contains_stack_trace_or_local_path(traced):
    _fail()
    spans = traced.get_finished_spans()
    assert spans
    for span in spans:
        text = span.status.description or ""
        assert "Traceback" not in text
        assert "stacktrace" not in text
        assert "site-packages" not in text
        assert 'File "' not in text


@pytest.mark.parametrize("traced", [{}], indirect=True)
def test_default_keeps_type_and_message(traced):
    _fail()
    [span] = traced.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert span.status.description == f"backend down {MARKER}"
    [event] = [e for e in span.events if e.name == "exception"]
    assert event.attributes["exception.type"] == "RuntimeError"
    assert MARKER in event.attributes["exception.message"]
    assert "Traceback" in event.attributes["exception.stacktrace"]


@pytest.mark.parametrize(
    "traced",
    [{"hide_inputs": True}, {"hide_outputs": True}, {"hide_inputs": True, "hide_outputs": True}],
    indirect=True,
)
def test_hidden_config_keeps_only_the_exception_type(traced):
    _fail()
    [span] = traced.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert span.status.description is None
    [event] = [e for e in span.events if e.name == "exception"]
    assert event.attributes["exception.type"] == "RuntimeError"
    assert str(event.attributes["exception.escaped"]).lower() == "false"
    assert set(event.attributes) == {"exception.type", "exception.escaped"}
    assert "Traceback" not in _dump(span)
    assert "exception.stacktrace" not in _dump(span)
    assert MARKER not in _dump(span)
