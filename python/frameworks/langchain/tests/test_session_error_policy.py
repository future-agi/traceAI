"""Regression coverage for the Deep Agents / core error-policy merge."""
import pytest
from fi_instrumentation import TraceConfig, using_session
from langchain_core.runnables import RunnableLambda

from .test_error_status import (
    GatewayError,
    GatewayFailingChatModel,
    SECRET,
    _instrumented,
)


@pytest.mark.parametrize(
    "config,hidden",
    [
        (TraceConfig(), False),
        (TraceConfig(hide_inputs=True), True),
        (TraceConfig(hide_error_details=True), True),
        (TraceConfig(hide_inputs=True, hide_error_details=False), False),
    ],
)
@pytest.mark.parametrize("session_source", ["thread", "context", "metadata"])
def test_failed_nested_run_preserves_session_and_error_policy(config, hidden, session_source):
    metadata = {"thread_id": "thread-session"}
    if session_source == "metadata":
        metadata["session_id"] = "metadata-session"
    expected = f"{session_source}-session"

    def invoke_model(_):
        return GatewayFailingChatModel().invoke("hello")

    with _instrumented(config) as exporter:
        if session_source == "context":
            with using_session("context-session"), pytest.raises(GatewayError):
                RunnableLambda(invoke_model).invoke("x", {"metadata": metadata})
        else:
            with pytest.raises(GatewayError):
                RunnableLambda(invoke_model).invoke("x", {"metadata": metadata})
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert {s.attributes.get("session.id") for s in spans} == {expected}
    assert len({s.context.trace_id for s in spans}) == 1
    model_span = next(s for s in spans if s.name == "GatewayFailingChatModel")
    assert model_span.parent.span_id in {s.context.span_id for s in spans}
    for span in spans:
        assert span.status.status_code.name == "ERROR"
        assert span.attributes["error.type"]
        event = next(e for e in span.events if e.name == "exception")
        if hidden:
            assert span.status.description is None
            assert "exception.message" not in event.attributes
            assert "exception.stacktrace" not in event.attributes
            text = repr(dict(span.attributes))
            for private in (SECRET, "req_provider_123", "sess_456"):
                assert private not in text
        else:
            assert span.status.description == "Error code: 400"
            assert "exception.stacktrace" in event.attributes
    if not hidden:
        assert "req_provider_123" in model_span.attributes["output.value"]
