import sys
from types import TracebackType
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union, cast

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
    ERROR_TYPE_OTHER,
    EXCEPTION_ESCAPED,
    EXCEPTION_EVENT_NAME,
    EXCEPTION_MESSAGE,
    EXCEPTION_TYPE,
    ExceptionSummary,
    clean_error_description,
    get_error_type,
    qualified_exception_type,
    strip_error_details,
    summarize_exception,
)
from ._types import FiMimeType
from .config import TraceConfig

_IMPORTANT_ATTRIBUTES = [
    SpanAttributes.GEN_AI_SPAN_KIND,
]

# Recorded exceptions kept for the ERROR status description cleanup.
_MAX_EXCEPTION_SUMMARIES = 8


class FiSpan(wrapt.ObjectProxy):  # type: ignore[misc]
    """Span proxy that applies ``TraceConfig`` to everything written to the span.

    Error handling is enforced here for every caller, including the OpenTelemetry
    API's own ``use_span`` / ``start_as_current_span`` exception handling:

    - ``error.type`` is set when the span ends with status ERROR, from the
      exception that failed it (provider error code or fully qualified class
      name), else ``_OTHER``. An exception recorded on a span that still ends
      OK, such as a recovered retry, does not set it, and neither does a
      recovered exception set it on a later unrelated ERROR status.
    - An ERROR status description is reduced to the exception message; a Python
      traceback never ends up in it.
    - With ``hide_error_details`` on, the ERROR status has no description and
      exception events keep only ``exception.type`` / ``exception.escaped``.
    """

    def __init__(self, wrapped: Span, config: TraceConfig) -> None:
        super().__init__(wrapped)
        self._self_config = config
        self._self_important_attributes: Dict[str, AttributeValue] = {}
        # Only summaries (class names, message) of recorded exceptions are kept,
        # never the exceptions, so their traceback frames and locals are freed.
        self._self_exception_summaries: List[ExceptionSummary] = []
        # error.type of the last exception recorded as escaped, and of the
        # exception that was being handled when the status was set to ERROR.
        self._self_escaped_error_type: Optional[str] = None
        # Recently recorded exceptions (error.type, summary) and the description
        # of the last ERROR status, to tell which recorded exception a status
        # set outside an `except` block is about (LangChain records the error in
        # a callback and sets the status later).
        self._self_recorded: List[Tuple[str, ExceptionSummary]] = []
        self._self_status_description: Optional[str] = None
        self._self_status_error_type: Optional[str] = None
        self._self_status_code: Optional[StatusCode] = None

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

    def _remember_recorded(self, error_type: str, summary: ExceptionSummary) -> None:
        self._self_recorded.append((error_type, summary))
        del self._self_recorded[:-_MAX_EXCEPTION_SUMMARIES]

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
        error_type = get_error_type(exception)
        self._remember_recorded(error_type, summarize_exception(exception))
        if escaped:
            self._self_escaped_error_type = error_type
        if not self._self_config.hide_error_details:
            self._self_exception_summaries.append(summarize_exception(exception))
            del self._self_exception_summaries[:-_MAX_EXCEPTION_SUMMARIES]
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
            escaped = attributes.get(EXCEPTION_ESCAPED)
            if isinstance(exception_type, str) and exception_type:
                message = attributes.get(EXCEPTION_MESSAGE)
                self._remember_recorded(
                    exception_type,
                    ExceptionSummary(
                        (exception_type,), message if isinstance(message, str) else None
                    ),
                )
                if str(escaped).lower() == "true":
                    self._self_escaped_error_type = exception_type
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
        # Same rules as the SDK: UNSET is ignored and OK is final.
        if status_code is not StatusCode.UNSET and (
            self._self_status_code is not StatusCode.OK
        ):
            self._self_status_code = status_code
        if status_code is not StatusCode.ERROR:
            span.set_status(status, description)
            return
        # Called from an `except` block (use_span, most instrumentors), the
        # handled exception is the one this status is about.
        in_flight = sys.exc_info()[1]
        self._self_status_error_type = (
            get_error_type(in_flight) if in_flight is not None else None
        )
        self._self_status_description = status_description
        if self._self_config.hide_error_details:
            status_description = None
        else:
            status_description = clean_error_description(
                status_description,
                (in_flight, *reversed(self._self_exception_summaries)),
            )
        span.set_status(Status(StatusCode.ERROR, status_description))

    def _ended_with_error(self) -> bool:
        span = cast(Span, self.__wrapped__)
        status_code = getattr(getattr(span, "status", None), "status_code", None)
        if status_code is None:
            status_code = self._self_status_code
        return status_code is StatusCode.ERROR

    def _recorded_error_type_for_status(self) -> Optional[str]:
        # The newest recorded exception the ERROR status is about: its message
        # or class name is in the status description, or the status has no
        # description. A recovered exception the status does not mention (a
        # timeout retried successfully, then a guardrail rejection) is skipped.
        description = self._self_status_description
        for error_type, summary in reversed(self._self_recorded):
            if not description:
                return error_type
            if summary.message and summary.message in description:
                return error_type
            if any(name in description for name in summary.type_names):
                return error_type
        return None

    def _set_error_type(self) -> None:
        # Only a span that ends with status ERROR gets error.type: from the
        # exception handled when the status was set, else the last exception
        # recorded as escaped, else "_OTHER" (OpenTelemetry semantic
        # conventions). A recovered exception recorded earlier is not used: the
        # status may be about something else. An instrumentor that set
        # error.type itself is not overwritten.
        if not self._ended_with_error():
            return
        error_type = (
            self._self_status_error_type
            or self._self_escaped_error_type
            or self._recorded_error_type_for_status()
            or ERROR_TYPE_OTHER
        )
        span = cast(Span, self.__wrapped__)
        existing = getattr(span, "attributes", None) or {}
        if ERROR_TYPE not in existing:
            self.set_attribute(ERROR_TYPE, error_type)

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
            self._set_error_type()
            span.end(end_time)
        self._self_exception_summaries.clear()
        self._self_recorded.clear()

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
