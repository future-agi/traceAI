"""Source-only internals: ordinary package CI runs these unchanged assertions.

Published consumer contracts intentionally use only public SDK/instrumentor APIs.
"""
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from langchain_core.runnables import RunnableLambda
from traceai_langchain import LangChainInstrumentor
from traceai_langchain._tracer import _error_description
from .test_error_status import _fail, ModelNotFound, FailingChatModel

def test_error_bookkeeping_is_drained():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        with pytest.raises(ValueError):
            RunnableLambda(_fail).invoke("x")
        with pytest.raises(ModelNotFound):
            FailingChatModel().invoke("hello")
        assert len(instrumentor._tracer._errors_by_run) == 0
    finally:
        instrumentor.uninstrument()


def test_error_description_fallback_drops_run_error_traceback():
    run_error = (
        "ValueError('boom')Traceback (most recent call last):\n\n\n"
        '  File "/private/tmp/agent.py", line 3, in run\n'
    )
    assert _error_description(None, run_error) == "ValueError('boom')"
    assert _error_description(ValueError("boom"), run_error) == "boom"
    assert _error_description(None, None) is None


def test_update_span_status_uses_error_object_not_run_error():
    """Checked on a bare span mock, so the FI span wrapper's own clean-up does
    not hide a regression in the LangChain tracer."""
    from datetime import datetime, timezone
    from unittest.mock import MagicMock
    from uuid import uuid4

    from langchain_core.tracers.schemas import Run

    from traceai_langchain._tracer import _update_span

    error = ValueError("boom")
    run = Run(
        id=uuid4(),
        name="chain",
        start_time=datetime.now(timezone.utc),
        run_type="chain",
        inputs={},
        serialized={},
        error="ValueError('boom')Traceback (most recent call last):\n\n\n"
        '  File "/private/tmp/agent.py", line 3, in run\n',
    )
    span = MagicMock()
    _update_span(span, run, {}, error=error)
    (status,), _ = span.set_status.call_args
    assert status.status_code is StatusCode.ERROR
    assert status.description == "boom"

    span = MagicMock()
    _update_span(span, run, {})
    (status,), _ = span.set_status.call_args
    assert status.description == "ValueError('boom')"
