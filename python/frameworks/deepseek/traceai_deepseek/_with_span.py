from typing import Any, Dict, Optional

from opentelemetry import trace as trace_api


class _WithSpan:
    """Helper class to manage span lifecycle and attributes."""

    __slots__ = ("_span", "_context_attributes", "_extra_attributes")

    def __init__(
        self,
        span: trace_api.Span,
        context_attributes: Optional[Dict[str, Any]] = None,
        extra_attributes: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._span = span
        self._context_attributes = context_attributes or {}
        self._extra_attributes = extra_attributes or {}

    def record_exception(self, exception: Exception) -> None:
        """Record an exception on the span."""
        if self._span.is_recording():
            self._span.record_exception(exception)

    def finish_tracing(
        self,
        status: Optional[trace_api.Status] = None,
    ) -> None:
        """Finish the span with optional status."""
        if self._span.is_recording():
            if status is not None:
                self._span.set_status(status)
            for key, value in self._context_attributes.items():
                if value is not None:
                    self._span.set_attribute(key, value)
            for key, value in self._extra_attributes.items():
                if value is not None:
                    self._span.set_attribute(key, value)
            self._span.end()
