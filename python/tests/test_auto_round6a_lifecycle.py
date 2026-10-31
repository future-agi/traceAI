"""Round 6 pass A (r6a verifier OTHER #4): ``shutdown(0)`` stops every FI export route at once.

A zero deadline leaves no time for teardown (patches stay installed; restart requires a new
process), but the agent is stopped, so no span may export through a FI route afterwards.
"""

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fi_instrumentation.auto import _manager
from test_auto_manager import fresh_state  # noqa: F401


def test_zero_timeout_shutdown_disables_the_route_immediately(fresh_state):
    exporter = InMemorySpanExporter()
    provider = _manager.init(span_exporter=exporter, batch=False, instrumentations=[], wire=False)
    tracer = provider.get_tracer("app")
    with tracer.start_as_current_span("before"):
        pass
    overlay = _manager._state.overlays[provider]
    _manager.shutdown(0)
    assert _manager._state.stopped and overlay.enabled is False
    with tracer.start_as_current_span("after") as span:
        span.set_attribute("k", "v")  # the application's own span API is unaffected
    assert [s.name for s in exporter.get_finished_spans()] == ["before"]
    _manager.shutdown(0)  # repeated shutdown stays a no-op
    assert [s.name for s in exporter.get_finished_spans()] == ["before"]
