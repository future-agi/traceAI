import logging
from typing import Optional

from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry import trace as trace_api
from opentelemetry.util.types import Attributes

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _WithSpan:
    __slots__ = (
        "_span",
        "_context_attributes",
        "_extra_attributes",
        "_priority_attributes",
        "_is_finished",
    )

    def __init__(
        self,
        span: trace_api.Span,
        context_attributes: Attributes = None,
        extra_attributes: Attributes = None,
        priority_attributes: Attributes = None,
    ) -> None:
        self._span = span
        self._context_attributes = context_attributes
        self._extra_attributes = extra_attributes
        self._priority_attributes = priority_attributes
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
        # Python's bounded attribute map evicts older entries. Response metadata
        # is extracted before optional tool-call leaves, so restore it alongside
        # request attribution after every flattened payload has been recorded.
        priority_attributes = dict(self._priority_attributes or {})
        for mapping in (attributes, extra_attributes):
            if not mapping:
                continue
            for key in (
                SpanAttributes.GEN_AI_RESPONSE_MODEL,
                SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS,
                SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS,
                SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS,
            ):
                if (value := mapping.get(key)) is not None:
                    priority_attributes[key] = value
        for mapping in (
            attributes,
            self._context_attributes,
            self._extra_attributes,
            extra_attributes,
            priority_attributes,
        ):
            if not mapping:
                continue
            for key, value in mapping.items():
                if value is None:
                    continue
                try:
                    self._span.set_attribute(key, value)
                except Exception:
                    logger.exception("Failed to set attribute on span")
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
