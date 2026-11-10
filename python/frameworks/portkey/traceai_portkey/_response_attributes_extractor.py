import logging
from typing import Any, Iterable, Iterator, Mapping, Tuple

from opentelemetry.util.types import AttributeValue

from traceai_portkey._utils import _as_output_attributes, _io_value_and_type
from fi_instrumentation.fi_types import SpanAttributes
from traceai_portkey._request_attributes_extractor import (
    _RequestAttributesExtractor,
    get_attribute,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _ResponseAttributesExtractor:
    def get_attributes(self, response: Any) -> Iterator[Tuple[str, AttributeValue]]:
        yield from _as_output_attributes(
            _io_value_and_type(response),
        )

    def get_extra_attributes(
        self,
        response: Any,
        request_parameters: Mapping[str, Any],
    ) -> Iterator[Tuple[str, AttributeValue]]:
        yield from self._get_attributes_from_chat_completion(
            completion=response,
            request_parameters=request_parameters,
        )

    def _get_attributes_from_chat_completion(
        self,
        completion: Any,
        request_parameters: Mapping[str, Any],
    ) -> Iterator[Tuple[str, AttributeValue]]:
        if model := get_attribute(completion, "model", None):
            yield SpanAttributes.GEN_AI_REQUEST_MODEL, model
        if usage := get_attribute(completion, "usage", None):
            yield from self._get_attributes_from_completion_usage(usage)
        if (choices := get_attribute(completion, "choices", None)) and isinstance(
            choices, Iterable
        ):
            for choice in choices:
                if (index := get_attribute(choice, "index", None)) is None:
                    continue
                if message := get_attribute(choice, "message", None):
                    for key, value in self._get_attributes_from_chat_completion_message(
                        message
                    ):
                        yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}.{key}", value

    def _get_attributes_from_chat_completion_message(
        self,
        message: object,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        yield from _RequestAttributesExtractor()._get_attributes_from_message_param(
            message
        )

    def _get_attributes_from_completion_usage(
        self,
        usage: object,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        if (total_tokens := get_attribute(usage, "total_tokens", None)) is not None:
            yield SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS, total_tokens
        if (prompt_tokens := get_attribute(usage, "prompt_tokens", None)) is not None:
            yield SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, prompt_tokens
        if (
            completion_tokens := get_attribute(usage, "completion_tokens", None)
        ) is not None:
            yield SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, completion_tokens
