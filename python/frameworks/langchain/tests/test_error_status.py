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
from langchain_core.tracers.context import collect_runs
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from fi_instrumentation import TraceConfig
from traceai_langchain import LangChainInstrumentor

SECRET = "the user's prompt: hunter2"
NATIVE_ERROR_POLICY = hasattr(TraceConfig(), "hide_error_details")


def _config(**kwargs):
    # Missing public API is explicit failed/unsupported released evidence, never
    # a skipped privacy contract or a collection-time private-helper import.
    return TraceConfig(**kwargs)


def _assert_error_type(span, native_type, legacy_type):
    event = _exception_event(span)
    assert event.attributes["exception.type"] == legacy_type
    if NATIVE_ERROR_POLICY:
        assert span.attributes["error.type"] == native_type
    else:
        # Provider-code normalization is owned by core >=1.2, not LangChain.
        assert "error.type" not in span.attributes



class ModelNotFound(Exception):
    """Shaped like openai.NotFoundError. Provider error codes are read only from
    exceptions of known provider SDKs, so it claims the openai module."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = "model_not_found"


ModelNotFound.__module__ = "openai"


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
    _assert_error_type(span, "ValueError", "ValueError")
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
    _assert_error_type(span, "model_not_found", "openai.ModelNotFound")


@pytest.mark.parametrize(
    "config",
    [{"hide_inputs": True}, {"hide_error_details": True}],
    ids=["derived-from-hide_inputs", "explicit"],
)
def test_hidden_error_details(config):
    with _instrumented(_config(**config)) as exporter:
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
        _assert_error_type(span, error_type, "openai.ModelNotFound" if name == "FailingChatModel" else "ValueError")
        event = _exception_event(span)
        assert "exception.message" not in event.attributes
        assert "exception.stacktrace" not in event.attributes
        assert event.attributes["exception.type"]
        for value in span.attributes.values():
            assert SECRET not in str(value)




def test_explicit_false_keeps_details_with_hide_inputs():
    config = {"hide_inputs": True, "hide_error_details": False}
    with _instrumented(_config(**config)) as exporter:
        with pytest.raises(ValueError):
            RunnableLambda(_fail).with_config(run_name="failing_chain").invoke("x")
    span = _span(exporter, "failing_chain")
    assert span.status.description == SECRET
    assert _exception_event(span).attributes["exception.message"] == SECRET






def test_failed_llm_run_outputs_kept_by_default():
    """The failed run's outputs hold the provider error response; with error
    details shown they stay (behaviour unchanged)."""
    with collect_runs() as native, _instrumented() as exporter:
        with pytest.raises(GatewayError):
            GatewayFailingChatModel().invoke("hello")
    span = _span(exporter, "GatewayFailingChatModel")
    assert len(native.traced_runs) == 1
    assert_native_error_output(span, native.traced_runs[0])


@pytest.mark.parametrize(
    "config",
    [{"hide_inputs": True}, {"hide_error_details": True}],
    ids=["derived-from-hide_inputs", "explicit"],
)
def test_failed_llm_run_outputs_dropped_when_hidden(config):
    """Error body, response headers and request ids are error details."""
    with _instrumented(_config(**config)) as exporter:
        with pytest.raises(GatewayError):
            GatewayFailingChatModel().invoke("hello")
    span = _span(exporter, "GatewayFailingChatModel")
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description is None
    for key, value in span.attributes.items():
        assert SECRET not in str(value), key
        assert "req_provider_123" not in str(value), key
        assert "sess_456" not in str(value), key


def assert_native_error_output(span, native_run):
    """Exactly preserve the installed SDK's observation, not newer SDK fixtures.

    langchain-core 0.2.43 has no failed-response outputs; modern cores supply
    headers/body. Privacy-denial assertions remain unconditional in all eras.
    """
    from fi_instrumentation import safe_json_dumps

    if native_run.outputs:
        assert span.attributes["output.value"] == safe_json_dumps(native_run.outputs)
        assert "req_provider_123" in span.attributes["output.value"]
    else:
        assert "output.value" not in span.attributes
