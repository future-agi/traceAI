"""Held create spans end before the tracer provider flushes or shuts down (R1).

``instrument()`` hooks ``force_flush`` and ``shutdown`` on the provider
instance it was given, so a create span that is still held open on its
prediction is ended (as of create time, with the create-time status) and
reaches the processors before they flush or stop. ``uninstrument()`` puts
back exactly what was there before.
"""

from __future__ import annotations

import gc
import time

import pytest

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

from opentelemetry import trace as trace_api  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode  # noqa: E402

from _support import SLOW_MODEL, TEXT_MODEL, FakeReplicate, attrs, instrumented, make_client  # noqa: E402

_HOOKED = ("force_flush", "shutdown")


def test_force_flush_exports_a_held_create_span_without_waiting_for_release():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        prediction = client.predictions.create(model=SLOW_MODEL, input={})
        returned = time.time_ns()
        assert traced.replicate_spans() == []  # held open: wait() may follow

        assert traced.provider.force_flush() is True
        (span,) = traced.replicate_spans()  # the prediction is still held
        assert prediction.status == "starting"
        assert span.name == "replicate.predictions.create"
        assert attrs(span)["replicate.prediction.status"] == "starting"
        assert span.status.status_code is StatusCode.OK
        assert span.end_time <= returned  # as of create time

        # Never ended twice: not by a second flush, shutdown, release or uninstrument.
        traced.provider.force_flush()
        del prediction
        gc.collect()
        traced.instrumentor.uninstrument()
        assert len(traced.replicate_spans()) == 1


def test_shutdown_exports_a_held_create_span_before_the_processors_stop():
    with FakeReplicate() as fake, instrumented() as traced:
        prediction = make_client(fake).predictions.create(model=SLOW_MODEL, input={})
        assert traced.replicate_spans() == []

        traced.provider.shutdown()
        (span,) = traced.replicate_spans()
        assert attrs(span)["replicate.prediction.status"] == "starting"
        assert prediction.status == "starting"


def test_a_held_create_span_ended_by_force_flush_then_waited_gets_its_own_wait_span():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        prediction = client.predictions.create(model=TEXT_MODEL, input={})
        traced.provider.force_flush()
        prediction.wait()

    names = [span.name for span in traced.replicate_spans()]
    assert names == ["replicate.predictions.create", "replicate.prediction.wait"]
    create, wait = traced.replicate_spans()
    assert attrs(create)["replicate.prediction.status"] == "starting"
    assert attrs(wait)["replicate.prediction.status"] == "succeeded"
    assert attrs(wait)["replicate.prediction.id"] == attrs(create)["replicate.prediction.id"]


def _own(provider):
    return {name: vars(provider).get(name, "<class method>") for name in _HOOKED}


def test_uninstrument_restores_the_providers_flush_and_shutdown_by_identity():
    from traceai_replicate import ReplicateInstrumentor

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    assert _own(provider) == {name: "<class method>" for name in _HOOKED}
    instrumentor = ReplicateInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        for name in _HOOKED:
            assert name in vars(provider), name  # hooked on this instance only
            assert name not in vars(TracerProvider()), name
    finally:
        instrumentor.uninstrument()
    # Nothing left on the instance: the class methods are what is found again.
    assert _own(provider) == {name: "<class method>" for name in _HOOKED}
    for name in _HOOKED:
        assert getattr(provider, name).__func__ is getattr(TracerProvider, name)


def test_uninstrument_puts_back_methods_already_set_on_the_instance():
    from traceai_replicate import ReplicateInstrumentor

    provider = TracerProvider()
    calls = []

    def own_flush(timeout_millis=30000):
        calls.append("flush")
        return True

    def own_shutdown():
        calls.append("shutdown")

    provider.force_flush = own_flush
    provider.shutdown = own_shutdown
    instrumentor = ReplicateInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        assert provider.force_flush is not own_flush
        assert provider.force_flush() is True  # the original still runs, once
        provider.shutdown()
        assert calls == ["flush", "shutdown"]
    finally:
        instrumentor.uninstrument()
    assert provider.force_flush is own_flush
    assert provider.shutdown is own_shutdown


def test_a_hook_someone_else_installed_on_top_is_left_in_place():
    from traceai_replicate import ReplicateInstrumentor

    provider = TracerProvider()
    instrumentor = ReplicateInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    ours = provider.force_flush

    def theirs(timeout_millis=30000):
        return ours(timeout_millis)

    provider.force_flush = theirs
    instrumentor.uninstrument()
    assert provider.force_flush is theirs  # not clobbered
    assert "shutdown" not in vars(provider)
    assert provider.force_flush() is True  # ours is now a plain pass-through


class _SlottedProvider:
    """A duck-typed provider whose methods cannot be replaced on the instance."""

    __slots__ = ("_inner",)

    def __init__(self, inner):
        self._inner = inner

    def get_tracer(self, *args, **kwargs):
        return self._inner.get_tracer(*args, **kwargs)

    def force_flush(self, timeout_millis=30000):
        return self._inner.force_flush(timeout_millis)

    def shutdown(self):
        self._inner.shutdown()


@pytest.mark.parametrize(
    "make_provider",
    [
        trace_api.NoOpTracerProvider,
        trace_api.ProxyTracerProvider,
        lambda: _SlottedProvider(TracerProvider()),
    ],
    ids=["no-op", "proxy", "slotted"],
)
def test_providers_without_hookable_methods_are_instrumented_without_raising(make_provider):
    from traceai_replicate import ReplicateInstrumentor

    provider = make_provider()
    instrumentor = ReplicateInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        with FakeReplicate() as fake:
            make_client(fake).run(TEXT_MODEL, input={})
    finally:
        instrumentor.uninstrument()
    for name in _HOOKED:
        if hasattr(type(provider), name):
            assert getattr(provider, name).__func__ is getattr(type(provider), name)
