"""AC-11 (export never blocks the reader) and V-B (the hooked symbol on this SDK)."""

from __future__ import annotations

import inspect
import time
from importlib.metadata import version

import openai
from fi_instrumentation import FITracer, TraceConfig
from openai.resources.realtime import realtime as rt
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from packaging.version import Version

from conftest import load
from traceai_openai_realtime._session import SessionRecorder, Settings
from traceai_openai_realtime.package import _instruments


class SlowExporter(SpanExporter):
    def __init__(self) -> None:
        self.exported = 0

    def export(self, spans):
        time.sleep(0.5)
        self.exported += len(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


def test_full_export_queue_drops_spans_and_never_blocks_the_reader():
    exporter = SlowExporter()
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(exporter, max_queue_size=2, max_export_batch_size=1, schedule_delay_millis=1))
    tracer = FITracer(provider.get_tracer("test"), config=TraceConfig())
    recorder = SessionRecorder(tracer, Settings(), model="gpt-realtime-2.1")
    recorder.start()
    slowest = 0.0
    finished = 1  # the root
    for row in load("j1_voice_call"):
        t0 = time.perf_counter()
        if row["dir"] == "server":
            recorder.on_server_event(row["event"])
        else:
            recorder.on_client_event(row["event"])
        slowest = max(slowest, time.perf_counter() - t0)
    recorder.finish("client_closed")
    slowest = max(slowest, 0.0)
    assert slowest < 0.05, f"an event took {slowest * 1000:.1f} ms while the exporter was stuck"
    provider.shutdown()
    finished += 3 + 4 + 1  # turns, responses, tool
    assert exporter.exported < finished  # the queue was full and spans were dropped, not waited on


def test_hooked_symbols_exist_on_this_sdk():
    """V-B: Realtime.connect / AsyncRealtime.connect exist and take ``model`` on the installed SDK,
    and the installed version is inside the declared range."""
    installed = Version(version("openai"))
    spec = _instruments[0].split(" ", 1)[1]
    low, high = [Version(part.strip().lstrip(">=<").strip()) for part in spec.split(",")]
    assert low <= installed < high, (installed, spec)
    for cls in (rt.Realtime, rt.AsyncRealtime):
        params = inspect.signature(inspect.unwrap(cls.connect)).parameters
        assert "model" in params
    for cls in (rt.RealtimeConnection, rt.AsyncRealtimeConnection):
        for name in ("recv_bytes", "send", "close"):
            assert callable(getattr(cls, name)), (cls.__name__, name)
    client = openai.OpenAI(api_key="sk-test-000000")
    assert isinstance(client.realtime, rt.Realtime)
