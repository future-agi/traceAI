"""Fixtures shared by the traceai-livekit tests.

Nothing here imports livekit at module level: `test_mapping.py` needs no LiveKit runtime.
"""

import signal
from typing import Any, Iterator, List, Optional

import pytest
from fi_instrumentation.otel import Transport, register
from opentelemetry.sdk.trace.export import SpanExportResult

from traceai_livekit import MappedGRPCSpanExporter, MappedHTTPSpanExporter


@pytest.fixture()
def exported_spans(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """Spans handed to the Future AGI exporters, captured instead of sent."""
    captured: List[Any] = []

    def fake_export(self: Any, spans: Any) -> SpanExportResult:
        captured.extend(spans)
        return SpanExportResult.SUCCESS

    monkeypatch.setattr(MappedHTTPSpanExporter, "export", fake_export)
    monkeypatch.setattr(MappedGRPCSpanExporter, "export", fake_export)
    return captured


@pytest.fixture()
def make_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Build a provider with `register()` and make the integration pick it up.

    The provider is returned to the integration through a patched
    `trace_api.get_tracer_provider`, so the process-wide provider is never set. `register()`
    replaces the SIGINT/SIGTERM handlers; they are put back, and every provider is shut down.
    `enable_*` points LiveKit's module tracer at the provider; it is put back on the provider it
    held before, so no later test finds it on a shut-down one.
    """
    from livekit.agents.telemetry import traces

    monkeypatch.setenv("FI_API_KEY", "test-api-key")
    monkeypatch.setenv("FI_SECRET_KEY", "test-secret-key")
    saved_handlers = {
        sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    saved_tracer_provider = traces.tracer._tracer_provider
    providers: List[Any] = []

    def _make(
        transport: Transport = Transport.HTTP,
        batch: bool = True,
        schedule_delay_millis: Optional[float] = None,
    ) -> Any:
        provider = register(
            project_name="livekit-test",
            set_global_tracer_provider=False,
            verbose=False,
            transport=transport,
            batch=batch,
            schedule_delay_millis=schedule_delay_millis,
        )
        providers.append(provider)
        monkeypatch.setattr(
            "traceai_livekit.integration.trace_api.get_tracer_provider",
            lambda: provider,
        )
        return provider

    yield _make

    traces.tracer.set_provider(saved_tracer_provider)
    for provider in providers:
        provider.shutdown()
    for sig, handler in saved_handlers.items():
        signal.signal(sig, handler)
