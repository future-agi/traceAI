import logging
import traceback
from typing import Any, Optional, Tuple

from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry import trace as trace_api
from opentelemetry.util.types import Attributes
from traceai_openai._privacy import redact

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
        "_secrets",
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
        secrets: Tuple[str, ...] = (),
    ) -> None:
        self._span = span
        self._secrets = secrets
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

    def add_secrets(self, secrets: Tuple[str, ...]) -> None:
        # Credentials resolved by an SDK token provider are available only after
        # sending the request. Retain all known values for this span's lifetime.
        self._secrets = tuple(sorted(set((*self._secrets, *secrets)), key=len, reverse=True))

    def record_exception(self, exception: BaseException) -> None:
        if self._is_finished:
            return
        try:
            # Emit the standard event through FITracer's masking API, without
            # handing raw credential-bearing exception text to an exporter.
            cls = type(exception)
            self._span.add_event("exception", attributes={
                "exception.type": f"{cls.__module__}.{cls.__qualname__}",
                "exception.message": redact(str(exception), self._secrets),
                "exception.stacktrace": redact("".join(traceback.format_exception(
                    cls, exception, exception.__traceback__)), self._secrets),
                "exception.escaped": "False",
            })
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
                description = redact(status.description, self._secrets)
                if description:
                    # Core removes tracebacks by consulting the in-flight SDK
                    # exception. Its original message may contain credentials.
                    # Remove the traceback here so core cannot reconstruct that
                    # unsanitized message; the redacted event keeps the stack.
                    description = description.partition("Traceback (most recent call last)")[0]
                    description = description.rstrip() or None
                self._span.set_status(status=trace_api.Status(status.status_code, description))
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
            value = redact(value, self._secrets)
            self._span.set_attribute(key, value)
            if key in self._primary_attributes and value is not None:
                self._primary_attributes[key] = value
        except Exception:
            logger.exception(f"Failed to set attribute {key} on span")
