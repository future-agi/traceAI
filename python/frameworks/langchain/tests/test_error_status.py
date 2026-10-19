"""Error status on LangChain spans.

LangChain's `run.error` is `repr(error)` followed by the full Python traceback
(local file paths, source lines). The span status description must be the
exception message only; the stacktrace lives only in the exception event, and
with error details hidden there is no description, message or stacktrace.
"""
from __future__ import annotations

import contextlib
from typing import Any, List, Optional

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult
from langchain_core.runnables import RunnableLambda
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from fi_instrumentation import TraceConfig
from traceai_langchain import LangChainInstrumentor
from traceai_langchain._tracer import _error_description

SECRET = "the user's prompt: hunter2"


class ModelNotFound(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = "model_not_found"


class FailingChatModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "failing"

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise ModelNotFound(f"Error code: 404 - {SECRET}")


class _ErrorResponse:
    """httpx.Response-like: langchain-core copies body and headers of an
    exception's `response` into the failed run's outputs."""

    status_code = 400
    headers = {"x-request-id": "req_provider_123", "x-gateway-session": "sess_456"}

    def json(self) -> Any:
        return {"error": {"message": f"unknown model, prompt was: {SECRET}"}}


class GatewayError(Exception):
    def __init__(self) -> None:
        super().__init__("Error code: 400")
        self.response = _ErrorResponse()
        self.request_id = "req_provider_123"


class GatewayFailingChatModel(FailingChatModel):
    @property
    def _llm_type(self) -> str:
        return "gateway-failing"

    def _generate(self, *args: Any, **kwargs: Any) -> ChatResult:
        raise GatewayError()


def _fail(_: Any) -> Any:
    raise ValueError(SECRET)


@contextlib.contextmanager
def _instrumented(config: Optional[TraceConfig] = None):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LangChainInstrumentor()
    kwargs = {"tracer_provider": provider}
    if config is not None:
        kwargs["config"] = config
    instrumentor.instrument(**kwargs)
    try:
        yield exporter
    finally:
        instrumentor.uninstrument()


def _span(exporter, name):
    spans = [s for s in exporter.get_finished_spans() if s.name == name]
    assert len(spans) == 1, [s.name for s in exporter.get_finished_spans()]
    return spans[0]


def _exception_event(span):
    (event,) = [e for e in span.events if e.name == "exception"]
    return event


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "FI_HIDE_INPUTS",
        "FI_HIDE_OUTPUTS",
        "FI_HIDE_INPUT_MESSAGES",
        "FI_HIDE_OUTPUT_MESSAGES",
        "FI_HIDE_INPUT_TEXT",
        "FI_HIDE_OUTPUT_TEXT",
        "FI_HIDE_ERROR_DETAILS",
    ):
        monkeypatch.delenv(var, raising=False)


def test_chain_error_description_is_message_without_traceback():
    with _instrumented() as exporter:
        with pytest.raises(ValueError):
            RunnableLambda(_fail).with_config(run_name="failing_chain").invoke("x")
    span = _span(exporter, "failing_chain")
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == SECRET
    assert "Traceback" not in span.status.description
    assert span.attributes["error.type"] == "ValueError"
    event = _exception_event(span)
    assert event.attributes["exception.message"] == SECRET
    assert "Traceback (most recent call last)" in event.attributes["exception.stacktrace"]


def test_llm_error_description_is_message_without_traceback():
    with _instrumented() as exporter:
        with pytest.raises(ModelNotFound):
            FailingChatModel().invoke("hello")
    span = _span(exporter, "FailingChatModel")
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == f"Error code: 404 - {SECRET}"
    assert span.attributes["error.type"] == "model_not_found"


@pytest.mark.parametrize(
    "config",
    [TraceConfig(hide_inputs=True), TraceConfig(hide_error_details=True)],
    ids=["derived-from-hide_inputs", "explicit"],
)
def test_hidden_error_details(config):
    with _instrumented(config) as exporter:
        with pytest.raises(ModelNotFound):
            FailingChatModel().invoke("hello")
        with pytest.raises(ValueError):
            RunnableLambda(_fail).with_config(run_name="failing_chain").invoke("x")
    for name, error_type in (
        ("FailingChatModel", "model_not_found"),
        ("failing_chain", "ValueError"),
    ):
        span = _span(exporter, name)
        assert span.status.status_code is StatusCode.ERROR
        assert span.status.description is None
        assert span.attributes["error.type"] == error_type
        event = _exception_event(span)
        assert "exception.message" not in event.attributes
        assert "exception.stacktrace" not in event.attributes
        assert event.attributes["exception.type"]
        for value in span.attributes.values():
            assert SECRET not in str(value)


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


def test_explicit_false_keeps_details_with_hide_inputs():
    config = TraceConfig(hide_inputs=True, hide_error_details=False)
    with _instrumented(config) as exporter:
        with pytest.raises(ValueError):
            RunnableLambda(_fail).with_config(run_name="failing_chain").invoke("x")
    span = _span(exporter, "failing_chain")
    assert span.status.description == SECRET
    assert _exception_event(span).attributes["exception.message"] == SECRET


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


def test_failed_llm_run_outputs_kept_by_default():
    """The failed run's outputs hold the provider error response; with error
    details shown they stay (behaviour unchanged)."""
    with _instrumented() as exporter:
        with pytest.raises(GatewayError):
            GatewayFailingChatModel().invoke("hello")
    span = _span(exporter, "GatewayFailingChatModel")
    assert "req_provider_123" in span.attributes["output.value"]


@pytest.mark.parametrize(
    "config",
    [TraceConfig(hide_inputs=True), TraceConfig(hide_error_details=True)],
    ids=["derived-from-hide_inputs", "explicit"],
)
def test_failed_llm_run_outputs_dropped_when_hidden(config):
    """Error body, response headers and request ids are error details."""
    with _instrumented(config) as exporter:
        with pytest.raises(GatewayError):
            GatewayFailingChatModel().invoke("hello")
    span = _span(exporter, "GatewayFailingChatModel")
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description is None
    for key, value in span.attributes.items():
        assert SECRET not in str(value), key
        assert "req_provider_123" not in str(value), key
        assert "sess_456" not in str(value), key
