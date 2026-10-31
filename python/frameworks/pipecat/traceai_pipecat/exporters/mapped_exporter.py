"""Map native Pipecat spans without rebuilding their configured exporter."""

from copy import copy
from typing import Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from .base_exporter import BaseMappedSpanExporter


class _DelegatingSpanExporter(SpanExporter):
    """Transport end of the same cooperative chain used by the mapped exporters."""

    def __init__(self, exporter: SpanExporter) -> None:
        self.exporter = exporter

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self.exporter.export(spans)

    def shutdown(self) -> None:
        self.exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.exporter.force_flush(timeout_millis=timeout_millis)


class MappedSpanExporter(BaseMappedSpanExporter, _DelegatingSpanExporter):
    """Wrap a configured exporter, preserving its credentials and transport options.

    Pass this wrapper to a standard OpenTelemetry span processor instead of the
    original exporter. The processor owns both lifecycles through this wrapper;
    do not also attach or shut down the original exporter separately.

    Mapping uses the same policy as the existing mapped HTTP/gRPC exporters.
    Native content is preserved by default; mapping is not content redaction.
    """

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        # BaseMappedSpanExporter transforms span attributes. Give it independent
        # snapshots so another processor still sees the original native spans.
        return super().export([copy(span) for span in spans])
