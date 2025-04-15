import json
from enum import Enum
from functools import wraps
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Collection,
    Dict,
    Iterable,
    Iterator,
    Mapping,
    Tuple,
    TypeVar,
)

import litellm
from fi_instrumentation import (
    FITracer,
    TraceConfig,
    get_attributes_from_context,
    safe_json_dumps,
)
from fi_instrumentation.fi_types import (
    EmbeddingAttributes,
    FiSpanKindValues,
    ImageAttributes,
    MessageAttributes,
    MessageContentAttributes,
    SpanAttributes,
)
from litellm.types.utils import Choices, EmbeddingResponse, ImageResponse, ModelResponse
from openai.types.image import Image
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from opentelemetry.util.types import AttributeValue
from traceai_litellm.package import _instruments
from traceai_litellm.version import __version__


# Helper functions to set span attributes
def _set_span_attribute(span: trace_api.Span, name: str, value: AttributeValue) -> None:
    if value is not None and value != "":
        span.set_attribute(name, value)


T = TypeVar("T", bound=type)


def is_iterable_of(lst: Iterable[object], tp: T) -> bool:
    return isinstance(lst, Iterable) and all(isinstance(x, tp) for x in lst)


def _get_attributes_from_message_param(
    message: Mapping[str, Any],
) -> Iterator[Tuple[str, AttributeValue]]:
    if not hasattr(message, "get"):
        return
    if role := message.get("role"):
        yield (
            MessageAttributes.MESSAGE_ROLE,
            role.value if isinstance(role, Enum) else role,
        )

    if content := message.get("content"):
        if isinstance(content, str):
            yield MessageAttributes.MESSAGE_CONTENT, content
        elif is_iterable_of(content, dict):
            for index, c in list(enumerate(content)):
                for key, value in _get_attributes_from_message_content(c):
                    yield f"{MessageAttributes.MESSAGE_CONTENTS}.{index}.{key}", value


def _get_attributes_from_message_content(
    content: Mapping[str, Any],
) -> Iterator[Tuple[str, AttributeValue]]:
    content = dict(content)
    type_ = content.pop("type")
    if type_ == "text":
        if text := content.pop("text"):
            yield f"{MessageContentAttributes.MESSAGE_CONTENT_TYPE}", "text"
            yield f"{MessageContentAttributes.MESSAGE_CONTENT_TEXT}", text
    elif type_ == "image_url":
        if image := content.pop("image_url"):
            yield f"{MessageContentAttributes.MESSAGE_CONTENT_TYPE}", "image"
            yield f"{MessageContentAttributes.MESSAGE_CONTENT_IMAGE}", image.get(
                "url", ""
            )


def _get_attributes_from_image(
    image: Mapping[str, Any],
) -> Iterator[Tuple[str, AttributeValue]]:
    image = dict(image)
    if url := image.pop("url"):
        yield f"{ImageAttributes.IMAGE_URL}", url


def _instrument_func_type_completion(
    span: trace_api.Span, kwargs: Dict[str, Any]
) -> None:
    """
    Currently instruments the functions:
        litellm.completion()
        litellm.acompletion() (async version of completion)
        litellm.completion_with_retries()
        litellm.acompletion_with_retries() (async version of completion_with_retries)
    """
    _set_span_attribute(span, SpanAttributes.RAW_INPUT, safe_json_dumps(kwargs))
    _set_span_attribute(span, SpanAttributes.FI_SPAN_KIND, FiSpanKindValues.LLM.value)
    _set_span_attribute(
        span, SpanAttributes.LLM_MODEL_NAME, kwargs.get("model", "unknown_model")
    )

    if messages := kwargs.get("messages"):
        process_messages = _process_messages(messages)
        if filtered_messages := process_messages.get("filtered_messages"):
            _set_span_attribute(
                span, SpanAttributes.INPUT_VALUE, json.dumps(filtered_messages)
            )
        if input_images := process_messages.get("input_images"):
            _set_span_attribute(
                span, SpanAttributes.INPUT_IMAGES, json.dumps(input_images)
            )
        if eval_input := process_messages.get("eval_input"):
            _set_span_attribute(span, SpanAttributes.EVAL_INPUT, eval_input)
        if query := process_messages.get("query"):
            _set_span_attribute(span, SpanAttributes.QUERY, query)

        for index, input_message in list(enumerate(messages)):
            for key, value in _get_attributes_from_message_param(input_message):
                _set_span_attribute(
                    span, f"{SpanAttributes.LLM_INPUT_MESSAGES}.{index}.{key}", value
                )

    invocation_params = {
        k: v for k, v in kwargs.items() if k not in ["model", "messages"]
    }
    _set_span_attribute(
        span, SpanAttributes.LLM_INVOCATION_PARAMETERS, json.dumps(invocation_params)
    )


def _instrument_func_type_embedding(
    span: trace_api.Span, kwargs: Dict[str, Any]
) -> None:
    """
    Currently instruments the functions:
        litellm.embedding()
        litellm.aembedding() (async version of embedding)
    """
    _set_span_attribute(span, SpanAttributes.RAW_INPUT, safe_json_dumps(kwargs))
    _set_span_attribute(
        span,
        SpanAttributes.FI_SPAN_KIND,
        FiSpanKindValues.EMBEDDING.value,
    )
    _set_span_attribute(
        span, SpanAttributes.EMBEDDING_MODEL_NAME, kwargs.get("model", "unknown_model")
    )
    _set_span_attribute(
        span,
        f"{SpanAttributes.EMBEDDING_EMBEDDINGS}.{0}.{EmbeddingAttributes.EMBEDDING_TEXT}",
        str(kwargs.get("input")),
    )
    _set_span_attribute(span, SpanAttributes.INPUT_VALUE, str(kwargs.get("input")))


def _instrument_func_type_image_generation(
    span: trace_api.Span, kwargs: Dict[str, Any]
) -> None:
    """
    Currently instruments the functions:
        litellm.image_generation()
        litellm.aimage_generation() (async version of image_generation)
    """
    _set_span_attribute(span, SpanAttributes.RAW_INPUT, safe_json_dumps(kwargs))
    _set_span_attribute(span, SpanAttributes.FI_SPAN_KIND, FiSpanKindValues.LLM.value)
    if model := kwargs.get("model"):
        _set_span_attribute(span, SpanAttributes.LLM_MODEL_NAME, model)
    if prompt := kwargs.get("prompt"):
        _set_span_attribute(span, SpanAttributes.INPUT_VALUE, str(prompt))


def _finalize_span(span: trace_api.Span, result: Any) -> None:
    _set_raw_output(span, result)
    if isinstance(result, ModelResponse):
        if (choices := result.choices) and len(choices) > 0:
            choice = choices[0]
            if isinstance(choice, Choices):
                if output := choice.message.content:
                    _set_span_attribute(span, SpanAttributes.OUTPUT_VALUE, output)
                for key, value in _get_attributes_from_message_param(choice.message):
                    _set_span_attribute(
                        span, f"{SpanAttributes.LLM_OUTPUT_MESSAGES}.0.{key}", value
                    )
    elif isinstance(result, EmbeddingResponse):
        if result_data := result.data:
            first_embedding = result_data[0]
            _set_span_attribute(
                span,
                f"{SpanAttributes.EMBEDDING_EMBEDDINGS}.{0}.{EmbeddingAttributes.EMBEDDING_VECTOR}",
                json.dumps(first_embedding.get("embedding", [])),
            )
            _set_span_attribute(
                span, SpanAttributes.EMBEDDING_EMBEDDINGS, safe_json_dumps(result_data)
            )
    elif isinstance(result, ImageResponse):
        if len(result.data) > 0:
            if img_data := result.data[0]:
                if isinstance(img_data, Image) and (
                    url := (img_data.url or img_data.b64_json)
                ):
                    _set_span_attribute(span, ImageAttributes.IMAGE_URL, url)
                    _set_span_attribute(span, SpanAttributes.OUTPUT_VALUE, url)
                elif isinstance(img_data, dict) and (
                    url := (img_data.get("url") or img_data.get("b64_json"))
                ):
                    _set_span_attribute(span, ImageAttributes.IMAGE_URL, url)
                    _set_span_attribute(span, SpanAttributes.OUTPUT_VALUE, url)
    if hasattr(result, "usage"):
        _set_span_attribute(
            span, SpanAttributes.LLM_TOKEN_COUNT_PROMPT, result.usage["prompt_tokens"]
        )
        _set_span_attribute(
            span,
            SpanAttributes.LLM_TOKEN_COUNT_COMPLETION,
            result.usage["completion_tokens"],
        )
        _set_span_attribute(
            span, SpanAttributes.LLM_TOKEN_COUNT_TOTAL, result.usage["total_tokens"]
        )


class LiteLLMInstrumentor(BaseInstrumentor):  # type: ignore
    original_litellm_funcs: Dict[str, Callable[..., Any]] = (
        {}
    )  # Dictionary for original uninstrumented liteLLM functions

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        if not (tracer_provider := kwargs.get("tracer_provider")):
            tracer_provider = trace_api.get_tracer_provider()
        if not (config := kwargs.get("config")):
            config = TraceConfig()
        else:
            assert isinstance(config, TraceConfig)
        self._tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider),
            config=config,
        )

        functions_to_instrument = {
            "completion": self._completion_wrapper,
            "acompletion": self._acompletion_wrapper,
            "completion_with_retries": self._completion_with_retries_wrapper,
            # Bug report filed on GitHub for acompletion_with_retries: https://github.com/BerriAI/litellm/issues/4908
            # "acompletion_with_retries": self._acompletion_with_retries_wrapper,
            "embedding": self._embedding_wrapper,
            "aembedding": self._aembedding_wrapper,
            "image_generation": self._image_generation_wrapper,
            "aimage_generation": self._aimage_generation_wrapper,
        }

        for func_name, func_wrapper in functions_to_instrument.items():
            if hasattr(litellm, func_name):
                original_func = getattr(litellm, func_name)
                self.original_litellm_funcs[func_name] = (
                    original_func  # Add original liteLLM function to dictionary
                )
                setattr(
                    litellm, func_name, func_wrapper
                )  # Monkey patch each function with their respective wrapper
                self._set_wrapper_attr(func_wrapper)

    def _uninstrument(self, **kwargs: Any) -> None:
        for (
            func_name,
            original_func,
        ) in LiteLLMInstrumentor.original_litellm_funcs.items():
            setattr(litellm, func_name, original_func)
        self.original_litellm_funcs.clear()

    @wraps(litellm.completion)
    def _completion_wrapper(self, *args: Any, **kwargs: Any) -> Any:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return self.original_litellm_funcs["completion"](*args, **kwargs)
        streaming = kwargs.get("stream", False)
        if not streaming:
            with self._tracer.start_as_current_span(
                name="completion", attributes=dict(get_attributes_from_context())
            ) as span:
                _instrument_func_type_completion(span, kwargs)
                result = self.original_litellm_funcs["completion"](*args, **kwargs)
                _finalize_span(span, result)
                return result
        else:
            original_iterator = self.original_litellm_funcs["completion"](
                *args, **kwargs
            )
            span_cm = self._tracer.start_as_current_span(
                name="completion", attributes=dict(get_attributes_from_context())
            )
            span = span_cm.__enter__()
            _instrument_func_type_completion(span, kwargs)
            return StreamingIteratorWrapper(original_iterator, span, span_cm)

    @wraps(litellm.acompletion)
    async def _acompletion_wrapper(self, *args: Any, **kwargs: Any) -> Any:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return await self.original_litellm_funcs["acompletion"](*args, **kwargs)
        streaming = kwargs.get("stream", False)
        if not streaming:
            with self._tracer.start_as_current_span(
                name="acompletion", attributes=dict(get_attributes_from_context())
            ) as span:
                _instrument_func_type_completion(span, kwargs)
                result = await self.original_litellm_funcs["acompletion"](
                    *args, **kwargs
                )
                _finalize_span(span, result)
                return result
        else:
            original_async_iterator = await self.original_litellm_funcs["acompletion"](
                *args, **kwargs
            )
            span_cm = self._tracer.start_as_current_span(
                name="acompletion", attributes=dict(get_attributes_from_context())
            )
            span = span_cm.__enter__()
            _instrument_func_type_completion(span, kwargs)
            return AsyncStreamingIteratorWrapper(original_async_iterator, span, span_cm)

    @wraps(litellm.completion_with_retries)
    def _completion_with_retries_wrapper(
        self, *args: Any, **kwargs: Any
    ) -> ModelResponse:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return self.original_litellm_funcs["completion_with_retries"](
                *args, **kwargs
            )  # type:ignore
        with self._tracer.start_as_current_span(
            name="completion_with_retries",
            attributes=dict(get_attributes_from_context()),
        ) as span:
            _instrument_func_type_completion(span, kwargs)
            result = self.original_litellm_funcs["completion_with_retries"](
                *args, **kwargs
            )
            _finalize_span(span, result)
        return result  # type:ignore

    @wraps(litellm.acompletion_with_retries)
    async def _acompletion_with_retries_wrapper(
        self, *args: Any, **kwargs: Any
    ) -> ModelResponse:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return self.original_litellm_funcs["acompletion_with_retries"](
                *args, **kwargs
            )  # type:ignore
        with self._tracer.start_as_current_span(
            name="acompletion_with_retries",
            attributes=dict(get_attributes_from_context()),
        ) as span:
            _instrument_func_type_completion(span, kwargs)
            result = await self.original_litellm_funcs["acompletion_with_retries"](
                *args, **kwargs
            )
            _finalize_span(span, result)
        return result  # type:ignore

    @wraps(litellm.embedding)
    def _embedding_wrapper(self, *args: Any, **kwargs: Any) -> EmbeddingResponse:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return self.original_litellm_funcs["embedding"](
                *args, **kwargs
            )  # type:ignore
        with self._tracer.start_as_current_span(
            name="embedding", attributes=dict(get_attributes_from_context())
        ) as span:
            _instrument_func_type_embedding(span, kwargs)
            result = self.original_litellm_funcs["embedding"](*args, **kwargs)
            _finalize_span(span, result)
        return result  # type:ignore

    @wraps(litellm.aembedding)
    async def _aembedding_wrapper(self, *args: Any, **kwargs: Any) -> EmbeddingResponse:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return self.original_litellm_funcs["aembedding"](
                *args, **kwargs
            )  # type:ignore
        with self._tracer.start_as_current_span(
            name="aembedding", attributes=dict(get_attributes_from_context())
        ) as span:
            _instrument_func_type_embedding(span, kwargs)
            result = await self.original_litellm_funcs["aembedding"](*args, **kwargs)
            _finalize_span(span, result)
        return result  # type:ignore

    @wraps(litellm.image_generation)
    def _image_generation_wrapper(self, *args: Any, **kwargs: Any) -> ImageResponse:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return self.original_litellm_funcs["image_generation"](
                *args, **kwargs
            )  # type:ignore
        with self._tracer.start_as_current_span(
            name="image_generation", attributes=dict(get_attributes_from_context())
        ) as span:
            _instrument_func_type_image_generation(span, kwargs)
            result = self.original_litellm_funcs["image_generation"](*args, **kwargs)
            _finalize_span(span, result)
        return result  # type:ignore

    @wraps(litellm.aimage_generation)
    async def _aimage_generation_wrapper(
        self, *args: Any, **kwargs: Any
    ) -> ImageResponse:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return self.original_litellm_funcs["aimage_generation"](
                *args, **kwargs
            )  # type:ignore
        with self._tracer.start_as_current_span(
            name="aimage_generation", attributes=dict(get_attributes_from_context())
        ) as span:
            _instrument_func_type_image_generation(span, kwargs)
            result = await self.original_litellm_funcs["aimage_generation"](
                *args, **kwargs
            )
            _finalize_span(span, result)
        return result  # type:ignore

    def _set_wrapper_attr(self, func_wrapper: Any) -> None:
        func_wrapper.__func__.is_wrapper = True


class StreamingIteratorWrapper:
    def __init__(self, iterator: Iterator[Any], span: trace_api.Span, span_cm):
        self.iterator = iterator
        self.span = span
        self.span_cm = span_cm
        self.processed_chunks = []
        self.content_string = ""

    def __iter__(self):
        return self

    def __next__(self):
        try:
            chunk = next(self.iterator)
            # Process chunk
            if hasattr(chunk, "to_dict"):
                chunk_dict = chunk.to_dict()
            elif hasattr(chunk, "__dict__"):
                chunk_dict = chunk.__dict__
            else:
                chunk_dict = chunk

            self.processed_chunks.append(chunk_dict)

            if (
                content := chunk_dict.get("choices", [{}])[0]
                .get("delta", {})
                .get("content")
            ):
                self.content_string += content

            return chunk
        except StopIteration:
            # After the iterator is exhausted, set the span attributes
            _set_span_attribute(
                self.span,
                SpanAttributes.RAW_OUTPUT,
                safe_json_dumps(self.processed_chunks),
            )
            _set_span_attribute(
                self.span, SpanAttributes.OUTPUT_VALUE, self.content_string
            )
            # End the span
            self.span_cm.__exit__(None, None, None)
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        # If the iterator exits early, we need to close the span
        self.span_cm.__exit__(exc_type, exc_value, traceback)


class AsyncStreamingIteratorWrapper:
    def __init__(
        self, async_iterator: AsyncIterator[Any], span: trace_api.Span, span_cm
    ):
        self.async_iterator = async_iterator
        self.span = span
        self.span_cm = span_cm
        self.processed_chunks = []
        self.content_string = ""

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            chunk = await self.async_iterator.__anext__()
            # Process chunk
            if hasattr(chunk, "to_dict"):
                chunk_dict = chunk.to_dict()
            elif hasattr(chunk, "__dict__"):
                chunk_dict = chunk.__dict__
            else:
                chunk_dict = chunk

            self.processed_chunks.append(chunk_dict)

            if (
                content := chunk_dict.get("choices", [{}])[0]
                .get("delta", {})
                .get("content")
            ):
                self.content_string += content

            return chunk
        except StopAsyncIteration:
            # After the iterator is exhausted, set the span attributes
            _set_span_attribute(
                self.span,
                SpanAttributes.RAW_OUTPUT,
                safe_json_dumps(self.processed_chunks),
            )
            _set_span_attribute(
                self.span, SpanAttributes.OUTPUT_VALUE, self.content_string
            )
            # End the span
            self.span_cm.__exit__(None, None, None)
            raise

    async def close(self):
        # Explicitly close the span if needed
        self.span_cm.__exit__(None, None, None)


def _set_raw_output(span: trace_api.Span, result: Any) -> None:
    if not result:
        return
    if hasattr(result, "to_dict"):
        _set_span_attribute(
            span, SpanAttributes.RAW_OUTPUT, safe_json_dumps(result.to_dict())
        )
    elif hasattr(result, "__dict__"):
        _set_span_attribute(
            span, SpanAttributes.RAW_OUTPUT, safe_json_dumps(result.__dict__)
        )
    else:
        _set_span_attribute(span, SpanAttributes.RAW_OUTPUT, safe_json_dumps(result))


def _process_messages(messages):
    try:
        input_images = []
        filtered_messages = []
        eval_input = []

        if isinstance(messages, list):
            for message in messages:
                filtered_content = []
                content = message.get("content", [])

                # Handle string content
                if isinstance(content, str):
                    filtered_messages.append(message)
                    eval_input.append(content)
                    continue

                # Handle list content
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict):
                            if item.get("type") == "image_url":
                                image_url = item.get("image_url", {}).get("url")
                                if image_url:
                                    input_images.append(image_url)
                                    image_index = len(input_images) - 1
                                    eval_input.append(
                                        "{{"
                                        + f"{SpanAttributes.INPUT_IMAGES}.{image_index}"
                                        + "}}"
                                    )
                            elif item.get("type") == "text":
                                filtered_content.append(item)
                                if text := item.get("text"):
                                    eval_input.append(str(text))

                    # Create new message with filtered content
                    if filtered_content:
                        filtered_message = message.copy()
                        filtered_message["content"] = filtered_content
                        filtered_messages.append(filtered_message)

        return {
            "input_images": input_images if input_images else None,
            "filtered_messages": filtered_messages if filtered_messages else messages,
            "eval_input": "\n".join(eval_input),
            "query": str(eval_input[0]) if eval_input else None,
        }
    except Exception as e:
        print(f"Error in _process_messages: {e}")
        return {
            "input_images": None,
            "filtered_messages": messages,
            "eval_input": None,
            "query": None,
        }
