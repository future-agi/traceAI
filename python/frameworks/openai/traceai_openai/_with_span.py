import logging
from typing import Any, Optional

from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry import trace as trace_api
from opentelemetry.util.types import Attributes

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Non-streamed responses expose usage as extra attributes rather than primary
# attributes. Keep these alongside the primary request/response data.
_USAGE_ATTRIBUTES = frozenset(
    (
        SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS,
        SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS,
        SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS,
    )
)


class _WithSpan:
    __slots__ = (
        "_span",
        "_primary_attributes",
        "_context_attributes",
        "_extra_attributes",
        "_is_finished",
    )

    def __init__(
        self,
        span: trace_api.Span,
        context_attributes: Attributes = None,
        extra_attributes: Attributes = None,
        initial_attributes: Attributes = None,
    ) -> None:
        self._span = span
        self._primary_attributes = dict(initial_attributes or {})
        self._context_attributes = context_attributes
        self._extra_attributes = extra_attributes
        try:
            self._is_finished = not self._span.is_recording()
        except Exception:
            logger.exception("Failed to check if span is recording")
            self._is_finished = True

    @property
    def is_finished(self) -> bool:
        return self._is_finished

    def record_exception(self, exception: Exception) -> None:
        if self._is_finished:
            return
        try:
            self._span.record_exception(exception)
        except Exception:
            logger.exception("Failed to record exception on span")

    def add_event(self, name: str) -> None:
        if self._is_finished:
            return
        try:
            self._span.add_event(name)
        except Exception:
            logger.exception("Failed to add event to span")

    def finish_tracing(
        self,
        status: Optional[trace_api.Status] = None,
        attributes: Attributes = None,
        extra_attributes: Attributes = None,
    ) -> None:
        if self._is_finished:
            return
        primary_attributes = {**self._primary_attributes, **(attributes or {})}
        for mapping in (
            attributes,
            self._context_attributes,
            self._extra_attributes,
            extra_attributes,
        ):
            if not mapping:
                continue
            for key, value in mapping.items():
                if value is None:
                    continue
                if key in primary_attributes or key in _USAGE_ATTRIBUTES:
                    # Preserve response-over-request precedence (e.g. served model).
                    primary_attributes[key] = value
                self.set_attribute(key, value)
        # Restore primary data last, through the same privacy-masking span API.
        # Indexed tool/message details may exceed even a custom attribute limit.
        for key, value in primary_attributes.items():
            if value is not None:
                self.set_attribute(key, value)
        if status is not None:
            try:
                self._span.set_status(status=status)
            except Exception:
                logger.exception("Failed to set status code on span")
        try:
            self._span.end()
        except Exception:
            logger.exception("Failed to end span")
        self._is_finished = True

    def set_attribute(self, key: str, value: Any) -> None:
        """Set a single attribute on the span."""
        if self._is_finished:
            return
        try:
            self._span.set_attribute(key, value)
            if key in self._primary_attributes and value is not None:
                self._primary_attributes[key] = value
        except Exception:
            logger.exception(f"Failed to set attribute {key} on span")
