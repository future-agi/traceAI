import sys
from types import TracebackType
from typing import Any, Callable, Dict, Optional, Type, Union, cast

import wrapt  # type: ignore[import-untyped]
from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes
from opentelemetry.trace import Span, Status, StatusCode
from opentelemetry.util.types import Attributes, AttributeValue

from ._attributes import (
    get_input_attributes,
    get_output_attributes,
    get_tool_attributes,
)
from ._errors import (
    ERROR_TYPE,
    EXCEPTION_ESCAPED,
    EXCEPTION_EVENT_NAME,
    EXCEPTION_TYPE,
    clean_error_description,
    get_error_type,
    qualified_exception_type,
    strip_error_details,
)
from ._types import FiMimeType
from .config import TraceConfig

_IMPORTANT_ATTRIBUTES = [
    SpanAttributes.GEN_AI_SPAN_KIND,
]


class FiSpan(wrapt.ObjectProxy):  # type: ignore[misc]
    """Span proxy that applies ``TraceConfig`` to everything written to the span.

    Error handling is enforced here for every caller, including the OpenTelemetry
    API's own ``use_span`` / ``start_as_current_span`` exception handling:

    - ``error.type`` is set from the recorded exception (provider error code or
      fully qualified class name).
    - An ERROR status description is reduced to the exception message; a Python
      traceback never ends up in it.
    - With ``hide_error_details`` on, the ERROR status has no description and
      exception events keep only ``exception.type`` / ``exception.escaped``.
    """

    def __init__(self, wrapped: Span, config: TraceConfig) -> None:
        super().__init__(wrapped)
        self._self_config = config
        self._self_important_attributes: Dict[str, AttributeValue] = {}
        self._self_last_exception: Optional[BaseException] = None
        self._self_error_type_set = False

    def __enter__(self) -> "FiSpan":
        # The wrapped span's __enter__ returns the raw span, which would bypass
        # this proxy inside `with tracer.start_span(...) as span:`.
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        span = cast(Span, self.__wrapped__)
        if exc_val is not None and span.is_recording():
            if getattr(span, "_record_exception", True):
                self.record_exception(exc_val, escaped=True)
            if getattr(span, "_set_status_on_exception", True):
                self.set_status(
                    Status(
                        status_code=StatusCode.ERROR,
                        description=f"{type(exc_val).__name__}: {exc_val}",
                    )
                )
        self.end()

    def record_exception(
        self,
        exception: BaseException,
        attributes: Attributes = None,
        timestamp: Optional[int] = None,
        escaped: bool = False,
    ) -> None:
        span = cast(Span, self.__wrapped__)
        if not span.is_recording():
            return
        self._self_last_exception = exception
        self._set_error_type(get_error_type(exception))
        if not self._self_config.hide_error_details:
            span.record_exception(
                exception, attributes=attributes, timestamp=timestamp, escaped=escaped
            )
            return
        event_attributes: Dict[str, AttributeValue] = {
            EXCEPTION_TYPE: qualified_exception_type(exception),
            EXCEPTION_ESCAPED: str(escaped),
        }
        event_attributes.update(strip_error_details(attributes) or {})
        span.add_event(
            name=EXCEPTION_EVENT_NAME, attributes=event_attributes, timestamp=timestamp
        )

    def add_event(
        self,
        name: str,
        attributes: Attributes = None,
        timestamp: Optional[int] = None,
    ) -> None:
        span = cast(Span, self.__wrapped__)
        if name == EXCEPTION_EVENT_NAME and attributes:
            exception_type = attributes.get(EXCEPTION_TYPE)
            if isinstance(exception_type, str) and exception_type:
                self._set_error_type(exception_type)
        if self._self_config.hide_error_details:
            attributes = strip_error_details(attributes)
        span.add_event(name, attributes=attributes, timestamp=timestamp)

    def set_status(
        self,
        status: Union[Status, StatusCode],
        description: Optional[str] = None,
    ) -> None:
        span = cast(Span, self.__wrapped__)
        if isinstance(status, Status):
            status_code, status_description = status.status_code, status.description
        else:
            status_code, status_description = status, description
        if status_code is not StatusCode.ERROR:
            span.set_status(status, description)
            return
        # Called from an `except` block (use_span, most instrumentors), the
        # handled exception is the one this status is about.
        in_flight = sys.exc_info()[1]
        exception = in_flight or self._self_last_exception
        if exception is not None:
            self._set_error_type(get_error_type(exception))
        if self._self_config.hide_error_details:
            status_description = None
        else:
            status_description = clean_error_description(
                status_description, (in_flight, self._self_last_exception)
            )
        span.set_status(Status(StatusCode.ERROR, status_description))

    def _set_error_type(self, error_type: str) -> None:
        # The first error recorded on the span wins; an instrumentor that set
        # error.type itself is not overwritten.
        if self._self_error_type_set:
            return
        span = cast(Span, self.__wrapped__)
        existing = getattr(span, "attributes", None) or {}
        if ERROR_TYPE not in existing:
            self.set_attribute(ERROR_TYPE, error_type)
        self._self_error_type_set = True

    def set_attributes(self, attributes: Dict[str, AttributeValue]) -> None:
        for k, v in attributes.items():
            self.set_attribute(k, v)

    def set_attribute(
        self,
        key: str,
        value: Union[AttributeValue, Callable[[], AttributeValue]],
    ) -> None:
        masked_value = self._self_config.mask(key, value)
        if masked_value is not None:
            if key in _IMPORTANT_ATTRIBUTES:
                self._self_important_attributes[key] = masked_value
            else:
                span = cast(Span, self.__wrapped__)
                span.set_attribute(key, masked_value)

    def end(self, end_time: Optional[int] = None) -> None:
        span = cast(Span, self.__wrapped__)
        for k, v in reversed(self._self_important_attributes.items()):
            span.set_attribute(k, v)
        if hasattr(span, "is_recording") and span.is_recording():
            span.end(end_time)

    def set_input(
        self,
        value: Any,
        *,
        mime_type: Optional[FiMimeType] = None,
    ) -> None:
        if GEN_AI_SPAN_KIND not in self._self_important_attributes:
            raise ValueError("Cannot set input attributes on a non-Fi span")
        self.set_attributes(get_input_attributes(value, mime_type=mime_type))

    def set_output(
        self,
        value: Any,
        *,
        mime_type: Optional[FiMimeType] = None,
    ) -> None:
        if GEN_AI_SPAN_KIND not in self._self_important_attributes:
            raise ValueError("Cannot set output attributes on a non-Fi span")
        self.set_attributes(get_output_attributes(value, mime_type=mime_type))

    def set_tool(
        self,
        *,
        name: str,
        description: Optional[str] = None,
        parameters: Union[str, Dict[str, Any]],
    ) -> None:
        if self._self_important_attributes.get(GEN_AI_SPAN_KIND) != TOOL:
            raise ValueError("Cannot set tool attributes on a non-tool span")
        self.set_attributes(
            get_tool_attributes(
                name=name,
                description=description,
                parameters=parameters,
            )
        )


# span kinds
TOOL = FiSpanKindValues.TOOL.value

# span attributes
GEN_AI_SPAN_KIND = SpanAttributes.GEN_AI_SPAN_KIND
