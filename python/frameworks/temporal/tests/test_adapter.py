import threading
import time

import pytest
from fi_instrumentation import using_session, using_user
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import (
    Link,
    SpanContext,
    Status,
    StatusCode,
    TraceFlags,
    TraceState,
)
from temporalio.contrib.opentelemetry import ReplaySafeTracerProvider

from traceai_temporal import SafeSpanExporter, configure

SECRET = "PRIVATE-CONTENT-MARKER"


def test_native_provider_and_no_auto_instrumentation(tracing):
    handle, exporter = tracing()
    assert isinstance(handle.provider, ReplaySafeTracerProvider)
    assert trace.get_tracer_provider() is handle.provider
    with handle.provider.get_tracer("application").start_as_current_span("work"):
        pass
    assert handle.force_flush()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.OK
    assert dict(spans[0].resource.attributes) == {
        "project_name": "synthetic-temporal",
        "project_type": "observe",
        "semantic_convention": "fi",
        "service.name": "temporal",
    }
    with pytest.raises(RuntimeError, match="already exists"):
        configure(project_name="another-project", exporter=InMemorySpanExporter())
    assert len(exporter.get_finished_spans()) == 1


def test_reject_existing_provider_before_owning_exporter():
    existing = TracerProvider(shutdown_on_exit=False)
    trace.set_tracer_provider(existing)
    with pytest.raises(RuntimeError, match="already exists"):
        configure(project_name="project")
    assert trace.get_tracer_provider() is existing
    existing.shutdown()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"project_name": ""},
        {"max_queue_size": 0},
        {"max_queue_size": 3.5},
        {"max_queue_size": 2, "max_export_batch_size": 3},
        {"export_timeout": float("inf")},
        {"export_timeout": -1},
        {"shutdown_timeout_millis": 0},
        {"max_content_length": 0},
        {"compression": "deflate"},
        {"capture_content": "true"},
        {"base_url": "http://remote.invalid"},
        {"base_url": "https://user:password@remote.invalid"},
        {"base_url": "https://remote.invalid?token=private"},
    ],
)
def test_validation_before_provider_install(kwargs):
    options = {"project_name": "synthetic"}
    options.update(kwargs)
    with pytest.raises((ValueError, TypeError)):
        configure(**options)
    assert isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider)


def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("FI_SECRET_KEY")
    with pytest.raises(ValueError, match="both FI_API_KEY"):
        configure(project_name="synthetic")


def test_session_user_and_usage_mapping_without_rollup(tracing):
    handle, exporter = tracing()
    tracer = handle.provider.get_tracer("application")
    with tracer.start_as_current_span(
        "RunWorkflow:Order", attributes={"temporalWorkflowID": "order-42"}
    ):
        with using_user("opaque-user"):
            with tracer.start_as_current_span("RunActivity:Model"):
                with tracer.start_as_current_span(
                    "model",
                    attributes={
                        "fi.span.kind": "LLM",
                        "llm.model_name": "synthetic-model",
                        "llm.token_count.prompt": 8,
                        "llm.token_count.completion": 2,
                        "llm.token_count.total": 10,
                        "gen_ai.usage.input_tokens": 9,  # canonical wins, no duplicate aliases
                    },
                ):
                    pass
    with using_session("explicit-session"):
        with tracer.start_as_current_span(
            "custom", attributes={"temporalWorkflowID": "order-42"}
        ):
            pass
    assert handle.force_flush()
    spans = {s.name: s for s in exporter.get_finished_spans()}
    model = spans["model"]
    assert model.attributes["session.id"] == "order-42"
    assert model.attributes["user.id"] == "opaque-user"
    assert model.attributes["gen_ai.span.kind"] == "llm"
    assert model.attributes["gen_ai.request.model"] == "synthetic-model"
    assert model.attributes["gen_ai.usage.input_tokens"] == 9
    assert not any(k.startswith("llm.token_count") for k in model.attributes)
    assert "gen_ai.usage.input_tokens" not in spans["RunWorkflow:Order"].attributes
    assert spans["custom"].attributes["session.id"] == "explicit-session"
    assert model.parent.span_id == spans["RunActivity:Model"].context.span_id


@pytest.mark.parametrize("capture", [False, True])
@pytest.mark.parametrize("keep_errors", [False, True])
def test_privacy_all_surfaces_and_no_mutation(capture, keep_errors):
    output = InMemorySpanExporter()
    original = InMemorySpanExporter()
    provider = TracerProvider(
        resource=Resource({"project_name": "synthetic", "host.name": SECRET}),
        shutdown_on_exit=False,
    )
    provider.add_span_processor(
        SimpleSpanProcessor(
            SafeSpanExporter(
                output,
                capture_content=capture,
                keep_error_text=keep_errors,
            )
        )
    )
    provider.add_span_processor(SimpleSpanProcessor(original))
    ctx = SpanContext(10, 20, False, TraceFlags(1), TraceState([("vendor", SECRET)]))
    with provider.get_tracer(
        "application", attributes={"scope.secret": SECRET}
    ).start_as_current_span(
        "static-name",
        links=[Link(ctx, {"link.secret": SECRET})],
        attributes={
            "input.value": SECRET,
            "output.value": SECRET,
            "unknown.future.attribute": SECRET,
            "exception.stacktrace": SECRET,
            "session.id": "opaque-session",
            "gen_ai.span.kind": "tool",
            "gen_ai.usage.input_tokens": 7,
            "gen_ai.request.parameters": SECRET,
        },
    ) as span:
        span.add_event(SECRET, {"payload": SECRET})
        span.record_exception(ValueError(SECRET))
        span.set_status(Status(StatusCode.ERROR, SECRET))
    result = output.get_finished_spans()[0]
    assert "unknown.future.attribute" not in result.attributes
    assert "exception.stacktrace" not in result.attributes
    assert (result.attributes.get("input.value") == SECRET) == capture
    assert (result.status.description == SECRET) == (capture and keep_errors)
    assert len(result.events) == 1 and result.events[0].name == "exception"
    assert (result.events[0].attributes.get("exception.message") == SECRET) == (
        capture and keep_errors
    )
    assert "exception.stacktrace" not in result.events[0].attributes
    assert not result.links[0].attributes and not result.links[0].context.trace_state
    assert not result.instrumentation_scope.attributes
    assert "host.name" not in result.resource.attributes
    assert (
        original.get_finished_spans()[0].attributes["unknown.future.attribute"]
        == SECRET
    )
    assert result.context.span_id == original.get_finished_spans()[0].context.span_id
    provider.shutdown()


def test_content_opt_in_respects_core_privacy_flags(monkeypatch, tracing):
    for name in (
        "FI_HIDE_INPUTS",
        "FI_HIDE_OUTPUTS",
        "FI_HIDE_LLM_INVOCATION_PARAMETERS",
        "FI_HIDE_ERROR_DETAILS",
    ):
        monkeypatch.setenv(name, "true")
    handle, exporter = tracing(capture_content=True, keep_error_text=True)
    with handle.provider.get_tracer("application").start_as_current_span(
        "model",
        attributes={
            "input.value": SECRET,
            "output.value": SECRET,
            "gen_ai.input.messages": SECRET,
            "gen_ai.output.messages": SECRET,
            "gen_ai.tool.call.arguments": SECRET,
            "gen_ai.tool.call.result": SECRET,
            "gen_ai.request.parameters": SECRET,
        },
    ) as span:
        span.record_exception(ValueError(SECRET))
        span.set_status(Status(StatusCode.ERROR, SECRET))
    assert handle.force_flush()
    assert SECRET not in exporter.get_finished_spans()[0].to_json()


def test_fail_closed_on_filter_or_export_exception(monkeypatch):
    exporter = InMemorySpanExporter()
    safe = SafeSpanExporter(exporter)

    def fail(_):
        raise ValueError(SECRET)

    monkeypatch.setattr(safe, "_scrub", fail)
    assert safe.export([ReadableSpan("synthetic")]) == SpanExportResult.FAILURE
    assert not exporter.get_finished_spans()


def test_bounded_content_and_invalid_usage(tracing):
    handle, exporter = tracing(capture_content=True, max_content_length=16)
    with handle.provider.get_tracer("app").start_as_current_span(
        "model",
        attributes={
            "input.value": "a" * 100,
            "gen_ai.usage.input_tokens": SECRET,
            "gen_ai.usage.output_tokens": -1,
            "gen_ai.usage.total_tokens": True,
        },
    ):
        pass
    assert handle.force_flush()
    attrs = exporter.get_finished_spans()[0].attributes
    assert attrs["input.value"] == "a" * 16
    assert not any(key.startswith("gen_ai.usage") for key in attrs)


class BlockingExporter(SpanExporter):
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.shutdowns = 0

    def export(self, _):
        self.entered.set()
        self.release.wait(10)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        self.shutdowns += 1


def test_non_blocking_end_and_bounded_lifecycle(tracing):
    blocked = BlockingExporter()
    handle, _ = tracing(exporter=blocked, max_export_batch_size=1)
    started = time.monotonic()
    with handle.provider.get_tracer("app").start_as_current_span("work"):
        pass
    assert time.monotonic() - started < 0.5
    assert blocked.entered.wait(2)
    try:
        started = time.monotonic()
        assert handle.force_flush(20) is False
        thread = handle._flush_thread
        assert handle.force_flush(20) is False
        assert handle._flush_thread is thread
        assert handle.shutdown(20) is False
        stopped_thread = handle._shutdown_thread
        assert handle.shutdown(20) is False
        assert handle._shutdown_thread is stopped_thread
        assert time.monotonic() - started < 1
    finally:
        blocked.release.set()
    assert handle.shutdown(3000)
    assert blocked.shutdowns == 1
    assert handle.force_flush() is False


def test_shutdown_drains_last_batch_without_ending_open_spans(tracing):
    handle, exporter = tracing(schedule_delay_millis=60000)
    tracer = handle.provider.get_tracer("app")
    unfinished = tracer.start_span("unfinished-workflow")
    with tracer.start_as_current_span("completed-activity"):
        pass
    assert handle.shutdown()
    assert handle.shutdown()
    assert {s.name for s in exporter.get_finished_spans()} == {"completed-activity"}
    assert unfinished.is_recording()
