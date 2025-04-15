from abc import ABC
import logging
from contextlib import contextmanager
from itertools import chain
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Tuple,
)

import opentelemetry.context as context_api
from anthropic.types import Message, MessageTokensCount
from anthropic.types import TextBlock, ToolUseBlock
from fi_instrumentation import get_attributes_from_context, safe_json_dumps
from fi_instrumentation.fi_types import (
    DocumentAttributes,
    EmbeddingAttributes,
    FiLLMProviderValues,
    FiLLMSystemValues,
    FiMimeTypeValues,
    FiSpanKindValues,
    MessageAttributes,
    MessageContentAttributes,
    SpanAttributes,
    ToolAttributes,
    ToolCallAttributes,
)
from opentelemetry import trace as trace_api
from opentelemetry.trace import INVALID_SPAN
from traceai_anthropic._stream import _MessagesStream, _Stream
from traceai_anthropic._with_span import _WithSpan

if TYPE_CHECKING:
    from anthropic.types import Usage
    from pydantic import BaseModel

logger = logging.getLogger(__name__)

class _WithTracer(ABC):
    """
    Base class for wrappers that need a tracer.
    """

    def __init__(self, tracer: trace_api.Tracer, *args: Any, **kwargs: Any) -> None:

        super().__init__(*args, **kwargs)
        self._tracer = tracer

    @contextmanager
    def _start_as_current_span(
        self, span_name: str, attributes: Optional[Mapping[str, Any]] = None
    ) -> Iterator[_WithSpan]:
        try:
            span = self._tracer.start_span(
                name=span_name,
                record_exception=False,
                set_status_on_exception=False,
                attributes=attributes,
            )
        except Exception as e:
            print(f"Error creating span: {e}")
            span = INVALID_SPAN
        with trace_api.use_span(
            span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            yield _WithSpan(span=span)


class _BaseWrapper(_WithTracer):
    """Base wrapper class with common functionality"""

    def _add_io_to_span_attributes(
        self,
        span: _WithSpan,
        input_data: Optional[Any] = None,
        output_data: Optional[Any] = None,
    ) -> None:
        """Add input/output data to span attributes"""
        try:
            if input_data is not None:
                span.set_attributes({"fi.llm.input": str(input_data)})
            if output_data is not None:
                span.set_attributes({"fi.llm.output": str(output_data)})
        except Exception as e:
            print(f"Error adding I/O to span attributes: {e}")


class _CompletionsWrapper(_BaseWrapper):
    """
    Wrapper for the pipeline processing
    Captures all calls to the pipeline
    """

    __slots__ = "_response_accumulator"

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)

        arguments = kwargs
        llm_prompt = dict(arguments).pop("system", None)
        llm_input_messages = dict(arguments).pop("messages", None)
        llm_invocation_parameters = _get_invocation_parameters(arguments)

        extracted_data = _extract_image_data(llm_input_messages)
        llm_filtered_input_messages = extracted_data["filtered_messages"]
        input_images = extracted_data["input_images"]
        eval_input = extracted_data["eval_input"]
        query = extracted_data["query"]

        with self._start_as_current_span(
            span_name="Completions",
            attributes=dict(
                chain(
                    get_attributes_from_context(),
                    _get_llm_model_name_from_input(arguments),
                    _get_llm_provider(),
                    _get_llm_system(),
                    _get_llm_span_kind(),
                    _get_llm_prompts(llm_prompt),
                    _get_inputs(llm_filtered_input_messages),
                    _get_llm_invocation_parameters(llm_invocation_parameters),
                    _get_llm_tools(llm_invocation_parameters),
                    _get_image_inputs(input_images),
                    _get_eval_input(eval_input),
                    _get_raw_input(arguments),
                    _get_query(query),
                )
            ),
        ) as span:
            try:
                response = wrapped(*args, **kwargs)
                span.set_attributes(dict(_get_raw_output(response)))

                # Add I/O to span attributes instead of sending separately
                self._add_io_to_span_attributes(
                    span=span,
                    output_data=(
                        response.completion if hasattr(response, "completion") else None
                    ),
                )

            except Exception as exception:
                print(f"Error in completion: {exception}")
                span.set_status(
                    trace_api.Status(trace_api.StatusCode.ERROR, str(exception))
                )
                span.record_exception(exception)
                raise

            streaming = kwargs.get("stream", False)
            if streaming:
                return _Stream(response, span)
            else:
                span.set_status(trace_api.StatusCode.OK)
                span.set_attributes(dict(_get_outputs(response)))
                span.finish_tracing()
                return response


class _AsyncCompletionsWrapper(_WithTracer):
    """
    Wrapper for the pipeline processing
    Captures all calls to the pipeline
    """

    async def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return await wrapped(*args, **kwargs)

        arguments = kwargs
        llm_prompt = dict(arguments).pop("system", None)
        llm_input_messages = dict(arguments).pop("messages", None)
        invocation_parameters = _get_invocation_parameters(arguments)

        extracted_data = _extract_image_data(llm_input_messages)
        filtered_llm_input_messages = extracted_data["filtered_messages"]
        input_images = extracted_data["input_images"]
        eval_input = extracted_data["eval_input"]
        query = extracted_data["query"]

        with self._start_as_current_span(
            span_name="AsyncCompletions",
            attributes=dict(
                chain(
                    get_attributes_from_context(),
                    _get_llm_model_name_from_input(arguments),
                    _get_llm_provider(),
                    _get_llm_system(),
                    _get_llm_span_kind(),
                    _get_llm_prompts(llm_prompt),
                    _get_inputs(filtered_llm_input_messages),
                    _get_llm_invocation_parameters(invocation_parameters),
                    _get_llm_tools(invocation_parameters),
                    _get_image_inputs(input_images),
                    _get_eval_input(eval_input),
                    _get_raw_input(arguments),
                    _get_query(query),
                )
            ),
        ) as span:
            try:
                response = await wrapped(*args, **kwargs)
                span.set_attributes(dict(_get_raw_output(response)))
            except Exception as exception:
                span.set_status(
                    trace_api.Status(trace_api.StatusCode.ERROR, str(exception))
                )
                span.record_exception(exception)
                raise
            span.set_status(trace_api.StatusCode.OK)
            streaming = kwargs.get("stream", False)
            if streaming:
                return _Stream(response, span)
            else:
                span.set_status(trace_api.StatusCode.OK)
                span.set_attributes(dict(_get_async_outputs(response)))
                span.finish_tracing()
                return response


class _MessagesWrapper(_BaseWrapper):
    """
    Wrapper for the pipeline processing
    Captures all calls to the pipeline
    """

    def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)

        arguments = kwargs
        llm_input_messages = dict(arguments).pop("messages", None)
        llm_prompt = dict(arguments).pop("system", None)
        invocation_parameters = _get_invocation_parameters(arguments)

        extracted_data = _extract_image_data(llm_input_messages)
        llm_filtered_input_messages = extracted_data["filtered_messages"]
        input_images = extracted_data["input_images"]
        eval_input = extracted_data["eval_input"]
        query = extracted_data["query"]

        with self._start_as_current_span(
            span_name="Messages",
            attributes=dict(
                chain(
                    get_attributes_from_context(),
                    _get_llm_model_name_from_input(arguments),
                    _get_llm_provider(),
                    _get_llm_system(),
                    _get_llm_span_kind(),
                    _get_llm_prompts(llm_prompt),
                    _get_llm_input_messages(llm_input_messages),
                    _get_llm_invocation_parameters(invocation_parameters),
                    _get_llm_tools(invocation_parameters),
                    _get_inputs(llm_filtered_input_messages),
                    _get_image_inputs(input_images),
                    _get_eval_input(eval_input),
                    _get_raw_input(arguments),
                    _get_query(query),
                )
            ),
        ) as span:
            try:
                response = wrapped(*args, **kwargs)
                streaming = kwargs.get("stream", False)
                if not streaming:
                    span.set_attributes(dict(_get_raw_output(response)))

                # Get output content
                output_content = None
                if hasattr(response, "content"):
                    if isinstance(response.content, list):
                        for block in response.content:
                            if hasattr(block, "text"):
                                output_content = block.text
                                break
                            elif isinstance(block, dict) and "text" in block:
                                output_content = block["text"]
                                break

                # Add I/O to span attributes instead of sending separately
                self._add_io_to_span_attributes(span=span, output_data=output_content)

            except Exception as exception:
                span.set_status(
                    trace_api.Status(trace_api.StatusCode.ERROR, str(exception))
                )
                span.record_exception(exception)
                raise

            streaming = kwargs.get("stream", False)
            if streaming:
                return _MessagesStream(response, span)
            else:
                span.set_status(trace_api.StatusCode.OK)
                span.set_attributes(
                    dict(
                        chain(
                            _get_llm_model_name_from_response(response),
                            _get_output_messages(response),
                            _get_llm_token_counts(response.usage),
                            _get_outputs(response),
                        )
                    )
                )
                span.finish_tracing()
                return response


class _AsyncMessagesWrapper(_WithTracer):
    """
    Wrapper for the pipeline processing
    Captures all calls to the pipeline
    """

    async def __call__(
        self,
        wrapped: Callable[..., Any],
        instance: Any,
        args: Tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return await wrapped(*args, **kwargs)

        arguments = kwargs
        llm_prompt = dict(arguments).pop("system", None)
        llm_input_messages = dict(arguments).pop("messages", None)
        invocation_parameters = _get_invocation_parameters(arguments)

        extracted_data = _extract_image_data(llm_input_messages)
        llm_filtered_input_messages = extracted_data["filtered_messages"]
        input_images = extracted_data["input_images"]
        eval_input = extracted_data["eval_input"]
        query = extracted_data["query"]

        with self._start_as_current_span(
            span_name="AsyncMessages",
            attributes=dict(
                chain(
                    get_attributes_from_context(),
                    _get_llm_provider(),
                    _get_llm_system(),
                    _get_llm_model_name_from_input(arguments),
                    _get_llm_span_kind(),
                    _get_llm_prompts(llm_prompt),
                    _get_llm_input_messages(llm_input_messages),
                    _get_llm_invocation_parameters(invocation_parameters),
                    _get_llm_tools(invocation_parameters),
                    _get_inputs(llm_filtered_input_messages),
                    _get_image_inputs(input_images),
                    _get_eval_input(eval_input),
                    _get_raw_input(arguments),
                    _get_query(query),
                )
            ),
        ) as span:
            try:
                response = await wrapped(*args, **kwargs)
                span.set_attributes(dict(_get_raw_output(response)))
            except Exception as exception:
                span.set_status(
                    trace_api.Status(trace_api.StatusCode.ERROR, str(exception))
                )
                span.record_exception(exception)
                raise
            streaming = kwargs.get("stream", False)
            if streaming:
                return _MessagesStream(response, span)
            else:
                span.set_status(trace_api.StatusCode.OK)
                span.set_attributes(
                    dict(
                        chain(
                            _get_llm_model_name_from_response(response),
                            _get_output_messages(response),
                            _get_llm_token_counts(response.usage),
                            _get_async_outputs(response),
                        )
                    )
                )
                span.finish_tracing()
                return response


class _MessagesCountTokensWrapper(_BaseWrapper):
    """
    Wrapper for the Messages.count_tokens method to create spans.
    """

    def __call__(
        self,
        wrapped,
        instance,
        args,
        kwargs,
    ):
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)

        arguments = kwargs
        llm_input_messages = arguments.get("messages", None)
        llm_prompt = dict(arguments).pop("system", None)

        extracted_data = _extract_image_data(llm_input_messages)
        llm_filtered_input_messages = extracted_data["filtered_messages"]
        input_images = extracted_data["input_images"]
        eval_input = extracted_data["eval_input"]
        query = extracted_data["query"]

        with self._start_as_current_span(
            span_name="MessagesCountTokens",
            attributes=dict(
                chain(
                    get_attributes_from_context(),
                    _get_llm_model_name_from_input(arguments),
                    _get_llm_provider(),
                    _get_llm_system(),
                    _get_llm_span_kind(),
                    _get_llm_prompts(llm_prompt),
                    _get_llm_input_messages(llm_input_messages),
                    _get_inputs(llm_filtered_input_messages),
                    _get_image_inputs(input_images),
                    _get_eval_input(eval_input),
                    _get_raw_input(arguments),
                    _get_query(query),
                )
            ),
        ) as span:
            try:
                response = wrapped(*args, **kwargs)
                span.set_attributes(dict(_get_raw_output(response)))
            except Exception as exception:
                span.set_status(
                    trace_api.Status(trace_api.StatusCode.ERROR, str(exception))
                )
                span.record_exception(exception)
                raise

            span.set_status(trace_api.StatusCode.OK)

            output_data = getattr(response, "input_tokens", None)

            self._add_io_to_span_attributes(span=span, output_data=output_data)

            if output_data is not None:
                span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_PROMPT, output_data)
            span.finish_tracing()
            return response


def _get_inputs(arguments: Mapping[str, Any]) -> Iterator[Tuple[str, Any]]:
    yield INPUT_VALUE, safe_json_dumps(arguments)
    yield INPUT_MIME_TYPE, JSON


def _get_raw_input(arguments: Mapping[str, Any]) -> Iterator[Tuple[str, Any]]:
    yield RAW_INPUT, safe_json_dumps(arguments)


def _get_raw_output(response) -> Iterator[Tuple[str, Any]]:
    if isinstance(response, (Message, MessageTokensCount)):
        yield RAW_OUTPUT, safe_json_dumps(response.to_dict())
    elif hasattr(response, "to_dict"):
        yield RAW_OUTPUT, safe_json_dumps(response.to_dict())
    else:
        yield RAW_OUTPUT, safe_json_dumps(response)


def _get_query(query: str) -> Iterator[Tuple[str, Any]]:
    yield QUERY, query


def _get_response(response: Any) -> Iterator[Tuple[str, Any]]:
    yield RESPONSE, response


def class_to_dict(obj: Any) -> Dict[str, Any]:
    """
    Recursively convert a class instance to a dictionary.
    """
    if isinstance(obj, dict):
        return {k: class_to_dict(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [class_to_dict(v) for v in obj]
    elif hasattr(obj, "__dict__"):
        return {k: class_to_dict(v) for k, v in obj.__dict__.items()}
    else:
        return obj


def _extract_image_data(messages):
    try:
        input_images = []
        filtered_messages = []
        eval_input = []

        def process_string_content(content):
            filtered_messages.append(message)
            eval_input.append(content)

        def process_image_item(item):
            source = item.get("source", {})
            if source.get("type") == "base64":
                data = source.get("data")
                if data:
                    input_images.append(data)
                    image_index = len(input_images) - 1
                    eval_input.append(
                        "{{"
                        + f"{SpanAttributes.INPUT_IMAGES}.{image_index}"
                        + "}}"
                    )

        def process_non_image_item(item, filtered_content):
            filtered_content.append(item)
            if item.get("type") == "text" and item.get("text"):
                eval_input.append(str(item.get("text")))

        def process_message_content(content):
            filtered_content = []
            if isinstance(content, str):
                process_string_content(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "image":
                            process_image_item(item)
                        else:
                            process_non_image_item(item, filtered_content)
            return filtered_content

        if isinstance(messages, list):
            for message in messages:
                content = message.get("content", [])
                filtered_content = process_message_content(content)

                if filtered_content:
                    filtered_message = message.copy()
                    filtered_message["content"] = filtered_content
                    filtered_messages.append(filtered_message)

        return {
            "input_images": input_images if input_images else None,
            "filtered_messages": filtered_messages if filtered_messages else None,
            "eval_input": "\n".join(eval_input),
            "query": str(eval_input[0]) if eval_input else None,
        }
    except Exception as e:
        logger.exception(f"Error in _extract_image_data: {e}")
        return {
            "input_images": None,
            "filtered_messages": messages,
            "eval_input": None,
            "query": None,
        }


def _get_image_inputs(input_images: Optional[List[str]]) -> Iterator[Tuple[str, Any]]:
    if input_images:
        yield SpanAttributes.INPUT_IMAGES, safe_json_dumps(input_images)


def _get_eval_input(input: Any) -> Iterator[Tuple[str, Any]]:
    if input is not None:
        yield SpanAttributes.EVAL_INPUT, safe_json_dumps(input)


def _get_outputs(response: "BaseModel") -> Iterator[Tuple[str, Any]]:
    yield OUTPUT_VALUE, response.model_dump_json()
    yield OUTPUT_MIME_TYPE, JSON


def _get_async_outputs(response: "BaseModel") -> Iterator[Tuple[str, Any]]:
    output_value = response
    if hasattr(response, "content"):
        content = response.content
        if isinstance(content, list):
            for block in content:
                if hasattr(block, "text"):
                    output_value = block.text
                    break
                elif isinstance(block, dict) and "text" in block:
                    output_value = block["text"]
                    break

    yield OUTPUT_VALUE, output_value
    yield OUTPUT_MIME_TYPE, JSON


def _get_llm_tools(
    invocation_parameters: Mapping[str, Any]
) -> Iterator[Tuple[str, Any]]:
    if isinstance(tools := invocation_parameters.get("tools"), list):
        yield LLM_TOOLS, safe_json_dumps(tools)
        for tool_index, tool_schema in enumerate(tools):
            yield f"{LLM_TOOLS}.{tool_index}.{TOOL_JSON_SCHEMA}", safe_json_dumps(
                tool_schema
            )


def _get_llm_span_kind() -> Iterator[Tuple[str, Any]]:
    yield FI_SPAN_KIND, LLM


def _get_llm_provider() -> Iterator[Tuple[str, Any]]:
    yield LLM_PROVIDER, LLM_PROVIDER_ANTHROPIC


def _get_llm_system() -> Iterator[Tuple[str, Any]]:
    yield LLM_SYSTEM, LLM_SYSTEM_ANTHROPIC


def _get_llm_token_counts(usage: "Usage") -> Iterator[Tuple[str, Any]]:
    yield LLM_TOKEN_COUNT_PROMPT, usage.input_tokens
    yield LLM_TOKEN_COUNT_COMPLETION, usage.output_tokens
    yield LLM_TOKEN_COUNT_TOTAL, usage.input_tokens + usage.output_tokens


def _get_llm_model_name_from_input(
    arguments: Mapping[str, Any]
) -> Iterator[Tuple[str, Any]]:
    if model_name := arguments.get("model"):
        yield LLM_MODEL_NAME, model_name


def _get_llm_model_name_from_response(message: "Message") -> Iterator[Tuple[str, Any]]:
    if model_name := getattr(message, "model"):
        yield LLM_MODEL_NAME, model_name


def _get_llm_invocation_parameters(
    invocation_parameters: Mapping[str, Any],
) -> Iterator[Tuple[str, Any]]:
    yield LLM_INVOCATION_PARAMETERS, safe_json_dumps(invocation_parameters)


def _get_llm_prompts(prompt: str) -> Iterator[Tuple[str, Any]]:
    yield LLM_PROMPTS, safe_json_dumps(prompt)


def _get_llm_input_messages(messages: List[Dict[str, str]]) -> Any:
    for i, message in enumerate(messages):
        tool_index = 0
        msg_prefix = f"{LLM_INPUT_MESSAGES}.{i}"

        if content := message.get("content"):
            if isinstance(content, str):
                yield f"{msg_prefix}.{MESSAGE_CONTENT}", content
            elif isinstance(content, list):
                for block_index, block in enumerate(content):
                    if isinstance(block, ToolUseBlock):
                        yield from _get_tool_use_block(block, msg_prefix, tool_index)
                        tool_index += 1
                    elif isinstance(block, TextBlock):
                        yield from _get_text_block(block, msg_prefix, block_index)
                    elif isinstance(block, dict):
                        block_type = block.get("type")
                        if block_type == "image":
                            yield from _get_image_block(block, msg_prefix, block_index)
                        elif block_type == "tool_use":
                            yield from _get_tool_use_block(block, msg_prefix, tool_index)
                            tool_index += 1
                        elif block_type == "tool_result":
                            yield from _get_tool_result_block(block, msg_prefix)
                        elif block_type == "text":
                            yield from _get_text_block_dct(block, msg_prefix, block_index)

        if role := message.get("role"):
            yield f"{msg_prefix}.{MESSAGE_ROLE}", role


def _get_tool_use_block(block: ToolUseBlock, msg_prefix: str, tool_index: int) -> Iterator[Tuple[str, Any]]:
    if tool_call_id := block.id:
        yield (
            f"{msg_prefix}.{MESSAGE_TOOL_CALLS}.{tool_index}.{TOOL_CALL_ID}",
            tool_call_id,
        )
    if block_name := block.name:
        yield (
            f"{msg_prefix}.{MESSAGE_TOOL_CALLS}.{tool_index}.{TOOL_CALL_FUNCTION_NAME}",
            block_name,
        )
    if block_input := block.input:
        yield (
            f"{msg_prefix}.{MESSAGE_TOOL_CALLS}.{tool_index}.{TOOL_CALL_FUNCTION_ARGUMENTS_JSON}",
            safe_json_dumps(block_input),
        )


def _get_text_block(block: TextBlock, msg_prefix: str, block_index: int) -> Iterator[Tuple[str, Any]]:
    yield f"{msg_prefix}.{MESSAGE_CONTENT}.{block_index}.{MESSAGE_CONTENT_TYPE}", "text"
    yield f"{msg_prefix}.{MESSAGE_CONTENT}.{block_index}.{MESSAGE_CONTENT_TEXT}", block.text


def _get_text_block_dct(block: Dict[str, Any], msg_prefix: str, block_index: int) -> Iterator[Tuple[str, Any]]:
    yield f"{msg_prefix}.{MESSAGE_CONTENT}.{block_index}.{MESSAGE_CONTENT_TYPE}", "text"
    yield f"{msg_prefix}.{MESSAGE_CONTENT}.{block_index}.{MESSAGE_CONTENT_TEXT}", block.get("text")


def _get_image_block(block: Dict[str, Any], msg_prefix: str, block_index: int) -> Iterator[Tuple[str, Any]]:
    source = block.get("source", {})
    if data := source.get("data"):
        yield f"{msg_prefix}.{MESSAGE_CONTENT}.{block_index}.{MESSAGE_CONTENT_TYPE}", "image"
        yield f"{msg_prefix}.{MESSAGE_CONTENT}.{block_index}.{MESSAGE_CONTENT_IMAGE}", data


def _get_tool_result_block(block: Dict[str, Any], msg_prefix: str) -> Iterator[Tuple[str, Any]]:
    if tool_call_id := block.get("tool_use_id"):
        yield (
            f"{msg_prefix}.{MESSAGE_TOOL_CALL_ID}",
            str(tool_call_id),
        )
    if content := block.get("content"):
        yield (
            f"{msg_prefix}.{MESSAGE_CONTENT}",
            (
                content
                if isinstance(content, str)
                else safe_json_dumps(content)
            ),
        )


def _get_invocation_parameters(kwargs: Mapping[str, Any]) -> Any:
    invocation_parameters = {}
    for key, value in kwargs.items():
        if _validate_invocation_parameter(key):
            invocation_parameters[key] = value
    return invocation_parameters


def _get_output_messages(response: Any) -> Any:
    """
    Extracts the tool call information from the response
    """

    tool_index = 0
    for block in response.content:
        yield f"{LLM_OUTPUT_MESSAGES}.{0}.{MESSAGE_ROLE}", response.role
        if isinstance(block, ToolUseBlock):
            yield (
                f"{LLM_OUTPUT_MESSAGES}.{0}.{MESSAGE_TOOL_CALLS}.{tool_index}.{TOOL_CALL_FUNCTION_NAME}",
                block.name,
            )
            yield (
                f"{LLM_OUTPUT_MESSAGES}.{0}.{MESSAGE_TOOL_CALLS}.{tool_index}.{TOOL_CALL_FUNCTION_ARGUMENTS_JSON}",
                safe_json_dumps(block.input),
            )
            tool_index += 1
        if isinstance(block, TextBlock):
            yield f"{LLM_OUTPUT_MESSAGES}.{0}.{MESSAGE_CONTENT}", block.text


def _validate_invocation_parameter(parameter: Any) -> bool:
    """
    Validates the invocation parameters.
    """
    valid_params = (
        "max_tokens",
        "max_tokens_to_sample",
        "model",
        "metadata",
        "stop_sequences",
        "stream",
        "system",
        "temperature",
        "tool_choice",
        "tools",
        "top_k",
        "top_p",
    )

    return parameter in valid_params


CHAIN = FiSpanKindValues.CHAIN.value
EMBEDDING = FiSpanKindValues.EMBEDDING.value
LLM = FiSpanKindValues.LLM.value
RETRIEVER = FiSpanKindValues.RETRIEVER.value

JSON = FiMimeTypeValues.JSON.value
TEXT = FiMimeTypeValues.TEXT.value

DOCUMENT_CONTENT = DocumentAttributes.DOCUMENT_CONTENT
DOCUMENT_ID = DocumentAttributes.DOCUMENT_ID
DOCUMENT_SCORE = DocumentAttributes.DOCUMENT_SCORE
DOCUMENT_METADATA = DocumentAttributes.DOCUMENT_METADATA
EMBEDDING_EMBEDDINGS = SpanAttributes.EMBEDDING_EMBEDDINGS
EMBEDDING_MODEL_NAME = SpanAttributes.EMBEDDING_MODEL_NAME
EMBEDDING_TEXT = EmbeddingAttributes.EMBEDDING_TEXT
EMBEDDING_VECTOR = EmbeddingAttributes.EMBEDDING_VECTOR
INPUT_MIME_TYPE = SpanAttributes.INPUT_MIME_TYPE
INPUT_VALUE = SpanAttributes.INPUT_VALUE
RAW_INPUT = SpanAttributes.RAW_INPUT
RAW_OUTPUT = SpanAttributes.RAW_OUTPUT
QUERY = SpanAttributes.QUERY
RESPONSE = SpanAttributes.RESPONSE
LLM_INPUT_MESSAGES = SpanAttributes.LLM_INPUT_MESSAGES
LLM_INVOCATION_PARAMETERS = SpanAttributes.LLM_INVOCATION_PARAMETERS
LLM_MODEL_NAME = SpanAttributes.LLM_MODEL_NAME
LLM_OUTPUT_MESSAGES = SpanAttributes.LLM_OUTPUT_MESSAGES
LLM_PROMPTS = SpanAttributes.LLM_PROMPTS
LLM_PROMPT_TEMPLATE = SpanAttributes.LLM_PROMPT_TEMPLATE
LLM_PROMPT_TEMPLATE_VARIABLES = SpanAttributes.LLM_PROMPT_TEMPLATE_VARIABLES
LLM_PROMPT_TEMPLATE_VERSION = SpanAttributes.LLM_PROMPT_TEMPLATE_VERSION
LLM_TOKEN_COUNT_COMPLETION = SpanAttributes.LLM_TOKEN_COUNT_COMPLETION
LLM_TOKEN_COUNT_PROMPT = SpanAttributes.LLM_TOKEN_COUNT_PROMPT
LLM_TOKEN_COUNT_TOTAL = SpanAttributes.LLM_TOKEN_COUNT_TOTAL
LLM_TOOLS = SpanAttributes.LLM_TOOLS
MESSAGE_CONTENT = MessageAttributes.MESSAGE_CONTENT
MESSAGE_CONTENTS = MessageAttributes.MESSAGE_CONTENTS
MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON = (
    MessageAttributes.MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON
)
MESSAGE_FUNCTION_CALL_NAME = MessageAttributes.MESSAGE_FUNCTION_CALL_NAME
MESSAGE_ROLE = MessageAttributes.MESSAGE_ROLE
MESSAGE_TOOL_CALLS = MessageAttributes.MESSAGE_TOOL_CALLS
MESSAGE_TOOL_CALL_ID = MessageAttributes.MESSAGE_TOOL_CALL_ID
METADATA = SpanAttributes.METADATA
FI_SPAN_KIND = SpanAttributes.FI_SPAN_KIND
OUTPUT_MIME_TYPE = SpanAttributes.OUTPUT_MIME_TYPE
OUTPUT_VALUE = SpanAttributes.OUTPUT_VALUE
RETRIEVAL_DOCUMENTS = SpanAttributes.RETRIEVAL_DOCUMENTS
SESSION_ID = SpanAttributes.SESSION_ID
TAG_TAGS = SpanAttributes.TAG_TAGS
TOOL_CALL_ID = ToolCallAttributes.TOOL_CALL_ID
TOOL_CALL_FUNCTION_ARGUMENTS_JSON = ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON
TOOL_CALL_FUNCTION_NAME = ToolCallAttributes.TOOL_CALL_FUNCTION_NAME
TOOL_JSON_SCHEMA = ToolAttributes.TOOL_JSON_SCHEMA
USER_ID = SpanAttributes.USER_ID
LLM_PROVIDER = SpanAttributes.LLM_PROVIDER
LLM_SYSTEM = SpanAttributes.LLM_SYSTEM
LLM_PROVIDER_ANTHROPIC = FiLLMProviderValues.ANTHROPIC.value
LLM_SYSTEM_ANTHROPIC = FiLLMSystemValues.ANTHROPIC.value
MESSAGE_CONTENT_TEXT = MessageContentAttributes.MESSAGE_CONTENT_TEXT
MESSAGE_CONTENT_IMAGE = MessageContentAttributes.MESSAGE_CONTENT_IMAGE
MESSAGE_CONTENT_TYPE = MessageContentAttributes.MESSAGE_CONTENT_TYPE
