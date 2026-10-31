from concurrent.futures import ThreadPoolExecutor

import pytest
from fi_instrumentation.mirascope import MirascopeSpanExporter, configure
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def native():
    return pytest.importorskip("mirascope.ops")


def test_concurrent_initialization_attaches_one_destination(native):
    target = InMemorySpanExporter()
    with ThreadPoolExecutor(max_workers=8) as pool:
        handles = list(
            pool.map(
                lambda _: configure(project_name="project", exporter=target), range(16)
            )
        )
    assert all(handle is handles[0] for handle in handles)
    with native.span("one"):
        pass
    assert handles[0].force_flush()
    assert len(target.get_finished_spans()) == 1


def test_env_destination_change_is_not_silently_ignored(native, monkeypatch):
    handle = configure(project_name="project")
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:8")
    with pytest.raises(ValueError, match="different options"):
        configure(project_name="project")
    handle.shutdown()


def test_capture_environment_and_explicit_false(native, monkeypatch):
    monkeypatch.setenv("FI_CAPTURE_CONTENT", "true")
    target = InMemorySpanExporter()
    handle = configure(project_name="project", exporter=target, capture_content=False)
    with native.span("one", private="PRIVATE_VALUE"):
        pass
    assert handle.force_flush()
    assert "private" not in target.get_finished_spans()[0].attributes


def test_configuration_failure_does_not_create_exporter(native, monkeypatch):
    from fi_instrumentation import mirascope

    def failed(**kwargs):
        raise ImportError("missing ops extras")

    def unexpected(**kwargs):
        pytest.fail("must validate Mirascope before creating an exporter")

    monkeypatch.setattr(native, "configure", failed)
    monkeypatch.setattr(mirascope, "HTTPSpanExporter", unexpected)
    with pytest.raises(ImportError, match="missing ops extras"):
        configure(project_name="project")


def test_reject_automatic_default_export_provider(native):
    provider = TracerProvider()
    provider._default_processor = True
    trace.set_tracer_provider(provider)
    with pytest.raises(ValueError, match="plain SDK provider"):
        configure(project_name="project", exporter=InMemorySpanExporter())


def test_invalid_exporter_before_provider_mutation(native):
    current = trace.get_tracer_provider()
    with pytest.raises(TypeError, match="exporter must implement"):
        configure(project_name="project", exporter=object())
    assert trace.get_tracer_provider() is current


def test_empty_export_and_flush():
    target = InMemorySpanExporter()
    wrapper = MirascopeSpanExporter(target, project_name="project")
    assert wrapper.export([]) == SpanExportResult.SUCCESS
    assert wrapper.force_flush()
    assert not target.get_finished_spans()


def test_shutdown_drains_completed_spans(native):
    # InMemorySpanExporter clears on shutdown, so record before delegating.
    class RecordingExporter(InMemorySpanExporter):
        def shutdown(self):
            self.at_shutdown = self.get_finished_spans()

    target = RecordingExporter()
    handle = configure(project_name="project", exporter=target)
    with native.span("one"):
        pass
    handle.shutdown()
    assert len(target.at_shutdown) == 1
