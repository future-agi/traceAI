"""Regression coverage for T1-PY-CORE-006 manual session attributes."""

import json
from unittest.mock import Mock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from fi_instrumentation.instrumentation import (
    get_context_attributes,
    get_session_attributes,
)


@pytest.mark.parametrize(
    "helper",
    [get_session_attributes, get_context_attributes],
    ids=["session", "context"],
)
@pytest.mark.parametrize("session_id", ["session-A", ""], ids=["nonempty", "empty"])
def test_manual_session_helpers_emit_both_keys(helper, session_id):
    assert helper(session_id=session_id) == {
        "session.id": session_id,
        "gen_ai.conversation.id": session_id,
    }


def test_context_session_preserves_other_attributes():
    attributes = get_context_attributes(
        session_id="session-A",
        user_id="user-A",
        metadata={"source": "unit-test"},
        tags=["manual"],
    )

    assert attributes.get("session.id") == "session-A"
    assert attributes["gen_ai.conversation.id"] == "session-A"
    assert attributes["user.id"] == "user-A"
    assert json.loads(attributes["metadata"]) == {"source": "unit-test"}
    assert attributes["tag.tags"] == ["manual"]


@pytest.mark.parametrize("kwargs", [{}, {"session_id": None}], ids=["omitted", "none"])
def test_context_without_session_omits_both_keys(kwargs):
    assert get_context_attributes(user_id="user-A", **kwargs) == {"user.id": "user-A"}


@pytest.mark.parametrize(
    "helper",
    [get_session_attributes, get_context_attributes],
    ids=["session", "context"],
)
def test_manual_session_helpers_export_native_session_in_normal_scope(helper):
    # Exercise ordinary OTel spans without a collector or network exporter.
    exporter = Mock(spec=SpanExporter)
    exporter.export.return_value = SpanExportResult.SUCCESS
    provider = TracerProvider(sampler=ALWAYS_ON, shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        with provider.get_tracer("my-app").start_as_current_span(
            "manual-operation", attributes=helper(session_id="session-A")
        ):
            pass
    finally:
        provider.shutdown()

    exporter.export.assert_called_once()
    (span,) = exporter.export.call_args.args[0]
    assert span.instrumentation_scope.name == "my-app"
    assert span.attributes["gen_ai.conversation.id"] == "session-A"
    # Native FutureAGI session identity uses session.id, not the conversation key.
    assert span.attributes.get("session.id") == "session-A"
