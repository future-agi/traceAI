"""Failed runs never export a stack trace; hiding inputs/outputs hides the error text.

LangChain's ``run.error`` is ``repr(error)`` plus the formatted traceback (local
file paths). traceai-langchain keeps the exception type and message, drops the
traceback, and with ``TraceConfig(hide_inputs/hide_outputs=True)`` keeps only the
exception type name.
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
def test_no_stack_trace_or_local_path_in_any_mode(traced):
    _fail()
    spans = traced.get_finished_spans()
    assert spans
    for span in spans:
        text = _dump(span)
        assert "Traceback" not in text
        assert "stacktrace" not in text
        assert "site-packages" not in text
        assert 'File "' not in text


@pytest.mark.parametrize("traced", [{}], indirect=True)
def test_default_keeps_type_and_message(traced):
    _fail()
    [span] = traced.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert span.status.description == f"RuntimeError('backend down {MARKER}')"
    [event] = [e for e in span.events if e.name == "exception"]
    assert event.attributes["exception.type"] == "RuntimeError"
    assert MARKER in event.attributes["exception.message"]


@pytest.mark.parametrize(
    "traced",
    [{"hide_inputs": True}, {"hide_outputs": True}, {"hide_inputs": True, "hide_outputs": True}],
    indirect=True,
)
def test_hidden_config_keeps_only_the_exception_type(traced):
    _fail()
    [span] = traced.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert span.status.description == "RuntimeError"
    [event] = [e for e in span.events if e.name == "exception"]
    assert dict(event.attributes) == {"exception.type": "RuntimeError", "exception.escaped": False}
    assert MARKER not in _dump(span)
