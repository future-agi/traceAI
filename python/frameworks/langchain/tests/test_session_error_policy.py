"""Regression coverage for the Deep Agents / core error-policy merge."""
import pytest
from fi_instrumentation import TraceConfig, using_session
from langchain_core.runnables import RunnableLambda
from langchain_core.tracers.context import collect_runs

from .test_error_status import (
    GatewayError,
    GatewayFailingChatModel,
    SECRET,
    _instrumented,
    _config,
    NATIVE_ERROR_POLICY,
    assert_native_error_output,
)


@pytest.mark.parametrize(
    "config,hidden",
    [
        ({}, False),
        ({"hide_inputs": True}, True),
        ({"hide_error_details": True}, True),
        ({"hide_inputs": True, "hide_error_details": False}, False),
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

    with collect_runs() as native, _instrumented(_config(**config)) as exporter:
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
        if NATIVE_ERROR_POLICY:
            assert span.attributes["error.type"]
        else:
            assert "error.type" not in span.attributes
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
        root, = native.traced_runs
        child, = root.child_runs
        assert_native_error_output(model_span, child)
