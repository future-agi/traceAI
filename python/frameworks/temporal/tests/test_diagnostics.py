"""Failures stay observable without leaking exporter or application exceptions."""

import pytest
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_temporal import SafeSpanExporter, configure
from traceai_temporal._session import activity

SECRET = "PRIVATE-EXCEPTION-CONTENT"


def fail(*_):
    raise RuntimeError(SECRET)


def assert_redacted(caplog, message):
    assert message in caplog.text
    assert SECRET not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("failure", ["filter", "export"])
def test_export_failure_is_reported_without_exception_text(
    monkeypatch, caplog, failure
):
    output = InMemorySpanExporter()
    safe = SafeSpanExporter(output)
    if failure == "filter":
        monkeypatch.setattr(safe, "_scrub", fail)
        spans = [object()]
    else:
        monkeypatch.setattr(output, "export", fail)
        spans = []
    assert safe.export(spans) == SpanExportResult.FAILURE
    assert not output.get_finished_spans()
    assert_redacted(caplog, "batch dropped, details redacted")


def test_flush_failure_is_reported_and_shutdown_still_works(
    monkeypatch, caplog, tracing
):
    handle, _ = tracing()
    monkeypatch.setattr(handle.provider, "force_flush", fail)
    assert handle.force_flush() is False
    assert_redacted(caplog, "flush failed; details redacted")
    assert handle.shutdown()


def test_shutdown_failure_is_reported_and_not_retried(monkeypatch, caplog):
    handle = configure(project_name="synthetic", exporter=InMemorySpanExporter())
    original_shutdown = handle.provider.shutdown
    calls = []

    def stop():
        calls.append(1)
        original_shutdown()
        fail()

    monkeypatch.setattr(handle.provider, "shutdown", stop)
    assert handle.shutdown() is False
    assert handle.closed
    assert handle.shutdown() is False
    assert len(calls) == 1
    assert_redacted(caplog, "shutdown failed; details redacted")


def test_context_failure_keeps_span_and_reports_redacted_diagnostic(
    monkeypatch, caplog, tracing
):
    handle, output = tracing()
    monkeypatch.setattr(activity, "in_activity", fail)
    with handle.provider.get_tracer("synthetic").start_as_current_span("work"):
        pass
    assert handle.force_flush()
    assert len(output.get_finished_spans()) == 1
    assert_redacted(caplog, "session mapping failed; span kept, details redacted")
