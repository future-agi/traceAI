import warnings
from collections import defaultdict
from copy import deepcopy
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    DefaultDict,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Protocol,
    Tuple,
    Type,
)

from fi_instrumentation import TraceConfig, safe_json_dumps
from fi_instrumentation.fi_types import FiMimeTypeValues, SpanAttributes
from opentelemetry import trace as trace_api
from opentelemetry.util.types import AttributeValue
from opentelemetry.trace import Status, StatusCode
from traceai_openai._privacy import response_output
from traceai_openai._utils import _as_output_attributes, _to_dict, _ValueAndType, _io_value_and_type

if TYPE_CHECKING:
    from openai.types import Completion
    from openai.types.chat import ChatCompletion
    from openai.types.responses.response import Response
    from openai.types.responses import ResponseStreamEvent

__all__ = (
    "_CompletionAccumulator",
    "_ChatCompletionAccumulator",
    "_ResponsesAccumulator",
)


class _CanGetAttributesFromResponse(Protocol):
    def get_attributes_from_response(
        self,
        response: Any,
        request_parameters: Mapping[str, Any],
    ) -> Iterator[Tuple[str, AttributeValue]]: ...


def _response_status(response: Any) -> Status:
    status = (
        response.get("status")
        if isinstance(response, Mapping)
        else getattr(response, "status", None)
    )
    if isinstance(status, str) and status in {"failed", "cancelled", "incomplete"}:
        # The vendor's error message can contain credentials or request content.
        return Status(StatusCode.ERROR, "Response did not complete")
    return Status(StatusCode.OK)


def _raw_response_status(body: Any) -> Status:
    """Raw HTTP success alone does not establish a completed generation."""
    status = body.get("status") if isinstance(body, Mapping) else None
    if isinstance(status, str) and status in {"completed", "failed", "cancelled", "incomplete"}:
        return _response_status(body)
    return Status(StatusCode.UNSET)


class _ResponsesAccumulator:
    __slots__ = (
        "_config",
        "_is_null",
        "_values",
        "_cached_result",
        "_request_parameters",
        "_response_attributes_extractor",
        "_chat_completion_type",
        "_on_completed",
        "_project_output",
    )

    def __init__(
        self,
        request_parameters: Mapping[str, Any],
        chat_completion_type: Type["Response"],
        response_attributes_extractor: Optional[_CanGetAttributesFromResponse] = None,
        config: Optional[TraceConfig] = None,
    ) -> None:
        self._config = config
        self._chat_completion_type = chat_completion_type
        self._request_parameters = request_parameters
        self._response_attributes_extractor = response_attributes_extractor
        self._is_null = True
        self._cached_result: Optional[Dict[str, Any]] = None
        self._values: Optional["ResponseStreamEvent"] = None
        self._on_completed: Optional[Callable[[Any], Any]] = None
        self._project_output: Optional[Callable[[Any], Any]] = None

    def observe_media(self, on_completed: Callable[[Any], Any], project_output: Callable[[Any], Any]) -> None:
        """Attachment hooks: capture from the consumed terminal event, project telemetry output."""
        self._on_completed, self._project_output = on_completed, project_output

    def process_chunk(self, chunk: Any) -> None:
        if getattr(chunk, "type", None) in {
            "response.completed", "response.failed", "response.incomplete", "error"
        }:
            self._is_null = False
            self._cached_result = None
            self._values = chunk
            if self._on_completed is not None and chunk.type == "response.completed":
                try:
                    self._on_completed(getattr(chunk, "response", None))
                except Exception:
                    pass

    def _result(self) -> Any:
        if self._is_null:
            return None
        if self._values:
            return getattr(self._values, "response", None)

    def get_status(self) -> Status:
        if self._values is None:
            return Status(StatusCode.ERROR, "Response stream ended before a terminal event")
        if self._values.type != "response.completed":
            return Status(StatusCode.ERROR, "Response stream did not complete")
        return _response_status(self._result())

    def get_attributes(self) -> Iterator[Tuple[str, AttributeValue]]:
        if not (result := self._result()):
            return
        # Extract just the assistant's response content, not the full API response
        output_content = None
        if hasattr(result, "output") and result.output:
            # For Responses API, extract output items
            output_items = []
            for item in result.output:
                if (
                    getattr(item, "type", None) == "message"
                    and getattr(item, "role", None) == "assistant"
                ):
                    for content in item.content or []:
                        if getattr(content, "type", None) == "output_text":
                            output_items.append(content.text)
            if output_items:
                output_content = " ".join(output_items)

        if output_content and not any(
            getattr(item, "type", None) in {
                "mcp_call", "mcp_list_tools", "mcp_approval_request", "mcp_approval_response"
            }
            for item in result.output
        ):
            yield SpanAttributes.OUTPUT_VALUE, output_content
        else:
            # Text alone would discard MCP results in a mixed response.
            # Keep response-owned data, never echoed request fields, and lead
            # with the answer just as non-streaming Responses previews do.
            output = response_output(result, self._config)
            if self._project_output is not None:
                output = self._project_output(output)
            if output_content:
                output = {"output_text": output_content, **output}
            yield from _as_output_attributes(_io_value_and_type(output))

    def get_extra_attributes(self) -> Iterator[Tuple[str, AttributeValue]]:
        if not (result := self._result()):
            return
        if self._response_attributes_extractor:
            yield from self._response_attributes_extractor.get_attributes_from_response(
                response=result,
                request_parameters=self._request_parameters,
            )


class _ChatCompletionAccumulator:
    __slots__ = (
        "_is_null",
        "_values",
        "_cached_result",
        "_request_parameters",
        "_response_attributes_extractor",
        "_chat_completion_type",
        "_content",
        "_raw_data",
    )

    def __init__(
        self,
        request_parameters: Mapping[str, Any],
        chat_completion_type: Type["ChatCompletion"],
        response_attributes_extractor: Optional[_CanGetAttributesFromResponse] = None,
    ) -> None:
        self._chat_completion_type = chat_completion_type
        self._request_parameters = request_parameters
        self._response_attributes_extractor = response_attributes_extractor
        self._is_null = True
        self._content: List[str] = []
        self._raw_data: List[Dict[str, Any]] = []
        self._cached_result: Optional[Dict[str, Any]] = None
        self._values = _ValuesAccumulator(
            choices=_IndexedAccumulator(
                lambda: _ValuesAccumulator(
                    message=_ValuesAccumulator(
                        content=_StringAccumulator(),
                        function_call=_ValuesAccumulator(
                            name=_StringAccumulator(),
                            arguments=_StringAccumulator(),
                        ),
                        tool_calls=_IndexedAccumulator(
                            lambda: _ValuesAccumulator(
                                function=_ValuesAccumulator(
                                    name=_StringAccumulator(),
                                    arguments=_StringAccumulator(),
                                ),
                            )
                        ),
                    ),
                ),
            ),
        )

    def process_chunk(self, chunk: Any) -> None:
        raw_chunk = _to_dict(chunk)
        self._raw_data.append(raw_chunk)
        self._is_null = False
        self._cached_result = None

        # Streaming choices carry message deltas. Feed the existing indexed
        # accumulators so fragmented tool arguments and multiple choices survive
        # serialization, just as non-streamed assistant messages do. Keep the
        # requested-model convention for streams; only aggregate message fields.
        self._values += {
            "choices": [
                {"index": choice["index"], "message": choice.get("delta") or {},
                 "finish_reason": choice.get("finish_reason")}
                for choice in raw_chunk.get("choices", [])
                if choice.get("index") is not None
            ]
        }

        if "choices" in raw_chunk:
            choices = raw_chunk["choices"]
            if choices and isinstance(choices, list):
                delta = choices[0].get("delta", {})
                content = delta.get("content")
                if content is not None:
                    self._content.append(content)

    def _result(self) -> Optional[Dict[str, Any]]:
        if self._is_null:
            return None
        if not self._cached_result:
            self._cached_result = dict(self._values)
        return self._cached_result

    def get_attributes(self) -> Iterator[Tuple[str, AttributeValue]]:
        # Combine the accumulated content
        output_value = "".join(self._content)

        if self._raw_data:
            last_chunk = self._raw_data[-1]
            if isinstance(last_chunk.get("usage"), Mapping):
                usage = last_chunk["usage"]
                total_tokens = usage.get("total_tokens")
                prompt_tokens = usage.get("prompt_tokens")
                completion_tokens = usage.get("completion_tokens")

                if total_tokens is not None:
                    yield SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS, total_tokens
                if prompt_tokens is not None:
                    yield SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, prompt_tokens
                if completion_tokens is not None:
                    yield SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, completion_tokens
                prompt_details = usage.get("prompt_tokens_details")
                if isinstance(prompt_details, Mapping):
                    if (cached_tokens := prompt_details.get("cached_tokens")) is not None:
                        yield SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS_CACHE_READ, cached_tokens
                completion_details = usage.get("completion_tokens_details")
                if isinstance(completion_details, Mapping):
                    if (reasoning_tokens := completion_details.get("reasoning_tokens")) is not None:
                        yield SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS_REASONING, reasoning_tokens

        # A tool-only completion has no text, but still has useful output.
        if not output_value and (result := self._result()):
            messages = [
                choice["message"]
                for choice in result.get("choices", [])
                if choice.get("message", {}).get("tool_calls")
                or choice.get("message", {}).get("function_call")
            ]
            if messages:
                yield SpanAttributes.OUTPUT_MIME_TYPE, FiMimeTypeValues.JSON.value
                output_value = safe_json_dumps(messages)
        yield SpanAttributes.OUTPUT_VALUE, output_value

    def get_status(self, *, exhausted: bool = False) -> Optional[trace_api.Status]:
        # Closing early is a normal caller action. Only judge a server-ended
        # stream when at least one choice proves the provider emits finish reasons.
        if not exhausted:
            return None
        choices = (self._result() or {}).get("choices", [])
        finished = {
            choice["index"] for choice in choices
            if choice.get("finish_reason") is not None
        }
        if not finished:
            return None
        expected = self._request_parameters.get("n", 1)
        if not isinstance(expected, int) or expected < 1:
            expected = 1
        # Check requested indices, not the count of returned choices. The indexed
        # accumulator retains reasons across null deltas and usage-only chunks.
        if any(index not in finished for index in range(expected)):
            return trace_api.Status(
                trace_api.StatusCode.ERROR,
                "Stream ended before all choices finished",
            )
        return None

    def get_extra_attributes(self) -> Iterator[Tuple[str, AttributeValue]]:
        if not (result := self._result()):
            return
        if self._response_attributes_extractor:
            yield from self._response_attributes_extractor.get_attributes_from_response(
                self._chat_completion_type.construct(**result),
                self._request_parameters,
            )


class _CompletionAccumulator:
    __slots__ = (
        "_is_null",
        "_values",
        "_cached_result",
        "_request_parameters",
        "_response_attributes_extractor",
        "_completion_type",
    )

    def __init__(
        self,
        request_parameters: Mapping[str, Any],
        completion_type: Type["Completion"],
        response_attributes_extractor: Optional[_CanGetAttributesFromResponse] = None,
    ) -> None:
        self._completion_type = completion_type
        self._request_parameters = request_parameters
        self._response_attributes_extractor = response_attributes_extractor
        self._is_null = True
        self._cached_result: Optional[Dict[str, Any]] = None
        self._values = _ValuesAccumulator(
            choices=_IndexedAccumulator(
                lambda: _ValuesAccumulator(text=_StringAccumulator())
            ),
        )

    def process_chunk(self, chunk: "Completion") -> None:
        self._is_null = False
        self._cached_result = None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            # `warnings=False` in `model_dump()` is only supported in Pydantic v2
            values = chunk.model_dump(exclude_unset=True)
        self._values += values

    def _result(self) -> Optional[Dict[str, Any]]:
        if self._is_null:
            return None
        if not self._cached_result:
            self._cached_result = dict(self._values)
        return self._cached_result

    def get_attributes(self) -> Iterator[Tuple[str, AttributeValue]]:
        if not (result := self._result()):
            return
        # Extract just the text content for legacy completions API
        output_content = None
        choices = result.get("choices", [])
        if choices:
            texts = [c.get("text", "") for c in choices if c.get("text")]
            if texts:
                output_content = "".join(texts)

        if output_content:
            yield SpanAttributes.OUTPUT_VALUE, output_content
        else:
            # Fallback to full response
            json_string = safe_json_dumps(result)
            yield from _as_output_attributes(
                _ValueAndType(json_string, FiMimeTypeValues.JSON),
            )

    def get_status(self) -> Optional[trace_api.Status]:
        return None

    def get_extra_attributes(self) -> Iterator[Tuple[str, AttributeValue]]:
        if not (result := self._result()):
            return
        if self._response_attributes_extractor:
            yield from self._response_attributes_extractor.get_attributes_from_response(
                self._completion_type.construct(**result),
                self._request_parameters,
            )


class _ValuesAccumulator:
    __slots__ = ("_values",)

    def __init__(self, **values: Any) -> None:
        self._values: Dict[str, Any] = values

    def __iter__(self) -> Iterator[Tuple[str, Any]]:
        for key, value in self._values.items():
            if value is None:
                continue
            if isinstance(value, _ValuesAccumulator):
                if dict_value := dict(value):
                    yield key, dict_value
            elif isinstance(value, _IndexedAccumulator):
                if list_value := list(value):
                    yield key, list_value
            elif isinstance(value, _StringAccumulator):
                if str_value := str(value):
                    yield key, str_value
            else:
                yield key, value

    def __iadd__(self, values: Optional[Mapping[str, Any]]) -> "_ValuesAccumulator":
        if not values:
            return self
        for key in self._values.keys():
            if (value := values.get(key)) is None:
                continue
            self_value = self._values[key]
            if isinstance(self_value, _ValuesAccumulator):
                if isinstance(value, Mapping):
                    self_value += value
            elif isinstance(self_value, _StringAccumulator):
                if isinstance(value, str):
                    self_value += value
            elif isinstance(self_value, _IndexedAccumulator):
                if isinstance(value, Iterable):
                    for v in value:
                        self_value += v
                else:
                    self_value += value
            elif isinstance(self_value, List) and isinstance(value, Iterable):
                self_value.extend(value)
            else:
                self._values[key] = value  # replacement
        for key in values.keys():
            if key in self._values or (value := values[key]) is None:
                continue
            value = deepcopy(value)
            if isinstance(value, Mapping):
                value = _ValuesAccumulator(**value)
            self._values[key] = value  # new entry
        return self


class _StringAccumulator:
    __slots__ = ("_fragments",)

    def __init__(self) -> None:
        self._fragments: List[str] = []

    def __str__(self) -> str:
        return "".join(self._fragments)

    def __iadd__(self, value: Optional[str]) -> "_StringAccumulator":
        if not value:
            return self
        self._fragments.append(value)
        return self


class _IndexedAccumulator:
    __slots__ = ("_indexed",)

    def __init__(self, factory: Callable[[], _ValuesAccumulator]) -> None:
        self._indexed: DefaultDict[int, _ValuesAccumulator] = defaultdict(factory)

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for _, values in sorted(self._indexed.items()):
            yield dict(values)

    def __iadd__(self, values: Optional[Mapping[str, Any]]) -> "_IndexedAccumulator":
        if (
            not values
            or not hasattr(values, "get")
            or (index := values.get("index")) is None
        ):
            return self
        self._indexed[index] += values
        return self
