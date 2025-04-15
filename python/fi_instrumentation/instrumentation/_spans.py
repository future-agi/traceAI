from typing import Any, Callable, Dict, Optional, Union, cast

import wrapt  # type: ignore[import-untyped]
from opentelemetry.trace import Span
from opentelemetry.util.types import AttributeValue

from fi_instrumentation.fi_types import (
    FiSpanKindValues,
    SpanAttributes,
)

from ._attributes import (
    get_input_attributes,
    get_output_attributes,
    get_tool_attributes,
)
from ._types import FiMimeType
from .config import (
    TraceConfig,
)

_IMPORTANT_ATTRIBUTES = [
    SpanAttributes.FI_SPAN_KIND,
]


class FiSpan(wrapt.ObjectProxy):  # type: ignore[misc]
    def __init__(self, wrapped: Span, config: TraceConfig) -> None:
        super().__init__(wrapped)
        self._self_config = config
        self._self_important_attributes: Dict[str, AttributeValue] = {}

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
        span.end(end_time)

    def set_input(
        self,
        value: Any,
        *,
        mime_type: Optional[FiMimeType] = None,
    ) -> None:
        if FI_SPAN_KIND not in self._self_important_attributes:
            raise ValueError("Cannot set input attributes on a non-Fi span")
        self.set_attributes(get_input_attributes(value, mime_type=mime_type))

    def set_output(
        self,
        value: Any,
        *,
        mime_type: Optional[FiMimeType] = None,
    ) -> None:
        if FI_SPAN_KIND not in self._self_important_attributes:
            raise ValueError("Cannot set output attributes on a non-Fi span")
        self.set_attributes(get_output_attributes(value, mime_type=mime_type))

    def set_tool(
        self,
        *,
        name: str,
        description: Optional[str] = None,
        parameters: Union[str, Dict[str, Any]],
    ) -> None:
        if self._self_important_attributes.get(FI_SPAN_KIND) != TOOL:
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
FI_SPAN_KIND = SpanAttributes.FI_SPAN_KIND
