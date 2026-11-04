import time
from threading import Event, Thread

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from traceai_agentscope import _lifecycle, configure


class Exporter(SpanExporter):
    def __init__(self):
        self.spans = []
        self.closed = 0

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        self.closed += 1


def start(provider):
    return provider.get_tracer("agentscope").start_span(
        "chat", attributes={"gen_ai.operation.name": "chat"}
    )


def test_new_provider_and_idempotence():
    dest = Exporter()
    one = configure(project_name="one", exporter=dest)
    two = configure(project_name="one", exporter=dest)
    assert one.owns_provider
    assert one.provider is trace.get_tracer_provider()
    assert one._pipeline is two._pipeline
    assert len(one.provider._active_span_processor._span_processors) == 1
    start(one.provider).end()
    assert one.force_flush()
    assert len(dest.spans) == 1
    assert two.shutdown()
    assert one.shutdown()
    assert dest.closed == 1


def test_shared_provider_is_not_replaced_or_stopped(shared):
    provider, raw = shared
    dest = Exporter()
    handle = configure(project_name="one", exporter=dest)
    assert not handle.owns_provider
    assert handle.provider is trace.get_tracer_provider() is provider
    start(provider).end()
    assert handle.shutdown()
    start(provider).end()
    assert len(raw.get_finished_spans()) == 2
    assert len(dest.spans) == 1
    assert dest.closed == 1


def test_inactive_provider_rejected(shared):
    unused = TracerProvider(shutdown_on_exit=False)
    dest = Exporter()
    with pytest.raises(ValueError, match="global"):
        configure(project_name="one", exporter=dest, provider=unused)
    assert dest.closed == 0
    assert not unused._active_span_processor._span_processors
    unused.shutdown()


def test_non_sdk_provider_rejected():
    trace.set_tracer_provider(trace.NoOpTracerProvider())
    with pytest.raises(TypeError, match="SDK"):
        configure(project_name="one", exporter=Exporter())


def test_explicit_provider_install():
    provider = TracerProvider(shutdown_on_exit=False)
    handle = configure(project_name="one", exporter=Exporter(), provider=provider)
    assert trace.get_tracer_provider() is provider
    assert not handle.owns_provider
    assert handle.shutdown()


def test_reconfigure_cannot_adopt_old_spans_or_grow_processor_list(shared):
    provider, _ = shared
    before = start(provider)
    first = configure(project_name="one", exporter=Exporter())
    old = start(provider)
    before.end()
    assert first.force_flush()
    assert not first._pipeline.processor.span_exporter.spans
    assert first.shutdown()
    for i in range(10):
        exporter = Exporter()
        current = configure(project_name=f"project-{i}", exporter=exporter)
        if i == 0:
            old.end()
        assert first.shutdown()  # an old handle cannot detach the new destination
        start(provider).end()
        assert current.force_flush()
        assert len(exporter.spans) == 1
        assert exporter.spans[0].resource.attributes["project_name"] == f"project-{i}"
        assert current.shutdown()
    assert len(provider._active_span_processor._span_processors) == 2


def test_options_conflict_and_shutdown_provider(shared):
    provider, _ = shared
    exporter = Exporter()
    handle = configure(project_name="one", exporter=exporter)
    with pytest.raises(ValueError, match="different options"):
        configure(project_name="two", exporter=exporter)
    provider.shutdown()
    assert exporter.closed == 1
    assert handle.shutdown()
    with pytest.raises(RuntimeError, match="shut down"):
        configure(project_name="one", exporter=exporter)


def test_bounded_open_span_tracking(shared, monkeypatch):
    provider, _ = shared
    monkeypatch.setattr(_lifecycle, "_MAX_OPEN", 2)
    dest = Exporter()
    handle = configure(project_name="one", exporter=dest)
    spans = [start(provider) for _ in range(3)]
    assert len(handle._slot.open) == 2
    for span in spans:
        span.end()
    assert handle.force_flush()
    assert len(dest.spans) == 2
    assert not handle._slot.open
    assert handle.shutdown()


def test_privacy_projection_happens_before_queue(shared):
    provider, _ = shared
    handle = configure(project_name="one", exporter=Exporter())
    with provider.get_tracer("agentscope").start_as_current_span(
        "PRIVATE_NAME",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.input.messages": '[{"content":"PRIVATE_PROMPT"}]',
        },
    ):
        pass
    batch = handle._pipeline.processor
    internal = batch._batch_processor
    (queued,) = internal._queue
    assert "PRIVATE_" not in queued.to_json()
    assert handle.shutdown()


def test_stuck_exporter_flush_and_shutdown_have_deadlines(shared):
    provider, _ = shared
    entered, release = Event(), Event()

    class Blocking(Exporter):
        def export(self, spans):
            entered.set()
            assert release.wait(5)
            return super().export(spans)

    exporter = Blocking()
    handle = configure(project_name="one", exporter=exporter)
    start(provider).end()
    began = time.monotonic()
    assert not handle.force_flush(20)
    assert entered.wait(1)
    for _ in range(3):
        assert not handle.force_flush(1)
    assert not handle.shutdown(20)
    assert time.monotonic() - began < 1
    with pytest.raises(RuntimeError, match="still closing"):
        configure(project_name="two", exporter=Exporter())
    release.set()
    assert handle.shutdown(2000)
    assert exporter.closed == 1


def test_shutdown_failures_return_false(shared):
    class Broken(Exporter):
        def shutdown(self):
            raise RuntimeError("synthetic shutdown failure")

    handle = configure(project_name="one", exporter=Broken())
    assert not handle.shutdown()
    assert not handle.force_flush()


@pytest.mark.parametrize(
    "kwargs,exception",
    [
        ({"project_name": ""}, ValueError),
        ({"project_name": " "}, ValueError),
        ({"capture_content": "yes"}, TypeError),
        ({"headers": {1: "x"}}, TypeError),
        ({"endpoint": ""}, ValueError),
        ({"exporter": object()}, TypeError),
        ({"exporter": Exporter(), "endpoint": "http://localhost"}, ValueError),
    ],
)
def test_input_validation(kwargs, exception):
    with pytest.raises(exception):
        configure(**{"project_name": "test", **kwargs})


@pytest.mark.parametrize("timeout", [-1, 300001, True, float("nan"), 1.5])
def test_timeout_validation(shared, timeout):
    handle = configure(project_name="one", exporter=Exporter())
    with pytest.raises(ValueError):
        handle.force_flush(timeout)
    with pytest.raises(ValueError):
        handle.shutdown(timeout)
    assert handle.shutdown()


def test_attach_then_raise_reuses_inactive_slot():
    class Provider(TracerProvider):
        def add_span_processor(self, processor):
            super().add_span_processor(processor)
            raise RuntimeError("attach failed")

    provider = Provider(shutdown_on_exit=False)
    trace.set_tracer_provider(provider)
    first = Exporter()
    with pytest.raises(RuntimeError, match="attach failed"):
        configure(project_name="one", exporter=first)
    slot = _lifecycle._slots[provider]
    # Failed setup closes the transferred exporter asynchronously.
    assert slot.closing.done.wait(2)
    assert first.closed == 1
    second = configure(project_name="one", exporter=Exporter())
    assert len(provider._active_span_processor._span_processors) == 1
    assert second.shutdown()


def test_global_provider_registration_race_is_explicit(monkeypatch):
    winner = TracerProvider(shutdown_on_exit=False)
    original = trace.set_tracer_provider
    monkeypatch.setattr(trace, "set_tracer_provider", lambda _: original(winner))
    with pytest.raises(RuntimeError, match="won global registration"):
        configure(project_name="one", exporter=Exporter())
    assert trace.get_tracer_provider() is winner


def test_concurrent_configuration_fails_fast(shared):
    provider, _ = shared
    entered, release = Event(), Event()
    original = provider.add_span_processor

    def add(processor):
        entered.set()
        assert release.wait(5)
        original(processor)

    provider.add_span_processor = add
    results = []
    thread = Thread(
        target=lambda: results.append(
            configure(project_name="one", exporter=Exporter())
        )
    )
    thread.start()
    assert entered.wait(1)
    with pytest.raises(RuntimeError, match="in progress"):
        configure(project_name="one", exporter=Exporter())
    release.set()
    thread.join(2)
    assert results[0].shutdown()


@pytest.mark.parametrize("attribute", ["_disabled", "_default_processor"])
def test_incompatible_provider_configuration_is_explicit(shared, attribute):
    provider, _ = shared
    setattr(provider, attribute, True)
    with pytest.raises(ValueError):
        configure(project_name="test", exporter=Exporter())


def test_flush_exception_is_reported_without_sensitive_details(
    shared, monkeypatch, caplog
):
    handle = configure(project_name="test", exporter=Exporter())

    def fail(timeout):
        raise RuntimeError("PRIVATE_FAILURE")

    monkeypatch.setattr(handle._pipeline.processor, "force_flush", fail)
    assert not handle.force_flush()
    assert "flush failed" in caplog.text
    assert "PRIVATE_FAILURE" not in caplog.text
    assert handle.shutdown()
