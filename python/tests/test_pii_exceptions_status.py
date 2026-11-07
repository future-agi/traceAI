"""pii_redaction covers the whole exported span: span and event attributes, the recorded
exception's message and stack trace, and the ERROR status description. With pii_redaction off,
the exception event and the status are exported exactly as before (the SDK's own event).
"""

import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from fi_instrumentation.instrumentation._tracers import FITracer
from fi_instrumentation.instrumentation.config import TraceConfig

EMAIL = "jane.doe@example.com"


class ProviderError(Exception):
    pass


ProviderError.__module__ = "openai"


@pytest.fixture
def exporter():
    return InMemorySpanExporter()


@pytest.fixture
def failing_call(exporter, clean_env):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def _call(**config):
        tracer = FITracer(provider.get_tracer(__name__), config=TraceConfig(**config))
        exporter.clear()
        span = tracer.start_span("llm", attributes={"input.value": f"mail {EMAIL}"})
        span.add_event("retry", {"retry.reason": f"rate limited for {EMAIL}", "retry.attempt": 2})
        try:
            raise ProviderError(f"400 unknown user {EMAIL}")
        except ProviderError as error:
            span.record_exception(error, attributes={"extra": f"for {EMAIL}"}, escaped=True)
            span.set_status(Status(StatusCode.ERROR, str(error)))
        span.end()
        (exported,) = exporter.get_finished_spans()
        return exported

    return _call


def _text(span):
    return json.dumps(
        {
            "attributes": dict(span.attributes),
            "events": [{"name": e.name, "attributes": dict(e.attributes or {})} for e in span.events],
            "status": span.status.description,
        }
    )


def _exception(span):
    events = [e for e in span.events if e.name == "exception"]
    # X1: one exception event; the redacted one replaces the original, it is not added next to it
    assert len(events) == 1, f"{len(events)} exception events"
    return events[0].attributes


def test_email_is_in_no_attribute_event_or_status(failing_call):
    span = failing_call(pii_redaction=True, hide_error_details=False)
    assert EMAIL not in _text(span)
    assert "<EMAIL_ADDRESS>" in _text(span)


def test_exception_event_keeps_the_sdk_shape_with_redacted_text(failing_call):
    attributes = _exception(failing_call(pii_redaction=True, hide_error_details=False))
    assert attributes["exception.type"] == "openai.ProviderError"
    assert attributes["exception.message"] == "400 unknown user <EMAIL_ADDRESS>"
    assert "<EMAIL_ADDRESS>" in attributes["exception.stacktrace"]
    assert "Traceback (most recent call last)" in attributes["exception.stacktrace"]
    assert attributes["exception.escaped"] == "True"
    assert attributes["extra"] == "for <EMAIL_ADDRESS>"


def test_status_description_is_redacted(failing_call):
    span = failing_call(pii_redaction=True, hide_error_details=False)
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "400 unknown user <EMAIL_ADDRESS>"


def test_hide_error_details_still_drops_error_text(failing_call):
    span = failing_call(pii_redaction=True, hide_error_details=True)
    assert EMAIL not in _text(span)
    assert span.status.description is None
    assert "exception.message" not in _exception(span)


def test_pii_off_exports_the_sdk_event_and_status_as_before(failing_call):
    span = failing_call(hide_error_details=False)
    attributes = _exception(span)
    assert attributes["exception.message"] == f"400 unknown user {EMAIL}"
    assert EMAIL in attributes["exception.stacktrace"]
    assert span.status.description == f"400 unknown user {EMAIL}"
    retry = [e for e in span.events if e.name == "retry"][0]
    assert dict(retry.attributes) == {"retry.reason": f"rate limited for {EMAIL}", "retry.attempt": 2}


def test_unchanged_text_keeps_the_native_sdk_event(failing_call, exporter):
    """Positive control: with pii_redaction on but nothing to redact, the exception event is
    the SDK's own, attribute for attribute."""
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    fi = FITracer(provider.get_tracer(__name__), config=TraceConfig(pii_redaction=True, hide_error_details=False))
    raw = provider.get_tracer("raw")
    try:
        raise ProviderError("plain failure")
    except ProviderError as error:
        caught = error
    exporter.clear()
    for tracer in (fi, raw):
        span = tracer.start_span("x")
        span.record_exception(caught, attributes={"extra": "ok"}, escaped=True)
        span.end()
    fi_span, raw_span = exporter.get_finished_spans()
    assert dict(_exception(fi_span)) == dict(_exception(raw_span))
