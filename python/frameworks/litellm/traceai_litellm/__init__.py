import json
import inspect
from contextlib import contextmanager
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
import logging

logger = logging.getLogger(__name__)
try:
    from fi.evals import Protect
except ImportError:
    logger.warning("ai-evaluation is not installed, please install it to trace protect")
    Protect = None
    pass
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
    ToolCallAttributes,
)
from fi_instrumentation.instrumentation._protect_wrapper import GuardrailProtectWrapper
from litellm.types.utils import Choices, EmbeddingResponse, ImageResponse, ModelResponse
from openai.types.image import Image
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from opentelemetry.util.types import AttributeValue
from traceai_litellm.package import _instruments
from traceai_litellm.version import __version__
from wrapt import wrap_function_wrapper

_REQUEST_FIELDS = frozenset(
    {
        "model",
        "messages",
        "input",
        "prompt",
        "temperature",
        "top_p",
        "n",
        "stream",
        "stream_options",
        "stop",
        "max_tokens",
        "max_completion_tokens",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
        "seed",
        "response_format",
        "tools",
        "tool_choice",
        "functions",
        "function_call",
        "parallel_tool_calls",
        "encoding_format",
        "dimensions",
        "size",
        "quality",
        "style",
    }
)


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
    if hasattr(message, "model_dump"):
        message = message.model_dump()
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

    for field, attribute in (
        ("name", MessageAttributes.MESSAGE_NAME),
        ("tool_call_id", MessageAttributes.MESSAGE_TOOL_CALL_ID),
    ):
        if value := message.get(field):
            yield attribute, value
    if function := message.get("function_call"):
        if name := function.get("name"):
            yield MessageAttributes.MESSAGE_FUNCTION_CALL_NAME, name
        if arguments := function.get("arguments"):
            yield MessageAttributes.MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON, arguments
    for index, tool in enumerate(message.get("tool_calls") or []):
        prefix = f"{MessageAttributes.MESSAGE_TOOL_CALLS}.{index}"
        if tool_id := tool.get("id"):
            yield f"{prefix}.{ToolCallAttributes.TOOL_CALL_ID}", tool_id
        function = tool.get("function") or {}
        if name := function.get("name"):
            yield f"{prefix}.{ToolCallAttributes.TOOL_CALL_FUNCTION_NAME}", name
        if arguments := function.get("arguments"):
            yield f"{prefix}.{ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}", arguments


def _has_tool_context(message):
    return any(
        message.get(key)
        for key in ("tool_calls", "tool_call_id", "function_call", "name")
    )


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
    _set_span_attribute(span, SpanAttributes.INPUT_VALUE, safe_json_dumps(kwargs))
    _set_span_attribute(
        span, SpanAttributes.GEN_AI_SPAN_KIND, FiSpanKindValues.LLM.value
    )
    _set_span_attribute(
        span, SpanAttributes.GEN_AI_REQUEST_MODEL, kwargs.get("model", "unknown_model")
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
        if any(_has_tool_context(message) for message in messages):
            # Keep tool IDs, arguments and results connected in the full conversation.
            _set_span_attribute(
                span, SpanAttributes.INPUT_VALUE, safe_json_dumps(messages)
            )
        elif eval_input := process_messages.get("eval_input"):
            _set_span_attribute(span, SpanAttributes.INPUT_VALUE, eval_input)

        for index, input_message in list(enumerate(messages)):
            for key, value in _get_attributes_from_message_param(input_message):
                _set_span_attribute(
                    span, f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{index}.{key}", value
                )

    invocation_params = {
        k: v for k, v in kwargs.items() if k not in ["model", "messages"]
    }
    _set_span_attribute(
        span,
        SpanAttributes.GEN_AI_REQUEST_PARAMETERS,
        safe_json_dumps(invocation_params),
    )


def _instrument_func_type_embedding(
    span: trace_api.Span, kwargs: Dict[str, Any]
) -> None:
    """
    Currently instruments the functions:
        litellm.embedding()
        litellm.aembedding() (async version of embedding)
    """
    _set_span_attribute(span, SpanAttributes.INPUT_VALUE, safe_json_dumps(kwargs))
    _set_span_attribute(
        span,
        SpanAttributes.GEN_AI_SPAN_KIND,
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
    _set_span_attribute(span, SpanAttributes.INPUT_VALUE, safe_json_dumps(kwargs))
    _set_span_attribute(
        span, SpanAttributes.GEN_AI_SPAN_KIND, FiSpanKindValues.LLM.value
    )
    if model := kwargs.get("model"):
        _set_span_attribute(span, SpanAttributes.GEN_AI_REQUEST_MODEL, model)
    if prompt := kwargs.get("prompt"):
        _set_span_attribute(span, SpanAttributes.INPUT_VALUE, str(prompt))


def _finalize_span(span: trace_api.Span, result: Any) -> None:
    # Embedding vectors must only use the canonical, privacy-filtered attributes.
    # Serializing the response elsewhere bypasses FI_HIDE_EMBEDDING_VECTORS.
    if not isinstance(result, EmbeddingResponse):
        _set_raw_output(span, result)
    if isinstance(result, ModelResponse):
        if (choices := result.choices) and len(choices) > 0:
            choice = choices[0]
            if isinstance(choice, Choices):
                if choice.message.content and not _has_tool_context(choice.message):
                    _set_span_attribute(
                        span, SpanAttributes.OUTPUT_VALUE, choice.message.content
                    )
                for key, value in _get_attributes_from_message_param(choice.message):
                    _set_span_attribute(
                        span, f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{key}", value
                    )
    elif isinstance(result, EmbeddingResponse):
        for index, embedding in enumerate(result.data or []):
            _set_span_attribute(
                span,
                f"{SpanAttributes.EMBEDDING_EMBEDDINGS}.{index}.{EmbeddingAttributes.EMBEDDING_VECTOR}",
                json.dumps(embedding.get("embedding", [])),
            )
    elif isinstance(result, ImageResponse):
        _set_span_attribute(
            span,
            f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{0}.{MessageAttributes.MESSAGE_ROLE}",
            "assistant",
        )
        for idx, img_data in enumerate(result.data):
            _set_span_attribute(
                span,
                f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{0}.{MessageAttributes.MESSAGE_CONTENT}.{idx}.{MessageContentAttributes.MESSAGE_CONTENT_TYPE}",
                "image",
            )
            if img_data:
                if isinstance(img_data, Image) and (
                    url := (img_data.url or img_data.b64_json)
                ):
                    _set_span_attribute(
                        span,
                        f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{0}.{MessageAttributes.MESSAGE_CONTENT}.{idx}.{MessageContentAttributes.MESSAGE_CONTENT_IMAGE}",
                        url,
                    )
                    _set_span_attribute(span, SpanAttributes.OUTPUT_VALUE, url)
                elif isinstance(img_data, dict) and (
                    url := (img_data.get("url") or img_data.get("b64_json"))
                ):
                    _set_span_attribute(
                        span,
                        f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{0}.{MessageAttributes.MESSAGE_CONTENT}.{idx}.{MessageContentAttributes.MESSAGE_CONTENT_IMAGE}",
                        url,
                    )
                    _set_span_attribute(span, SpanAttributes.OUTPUT_VALUE, url)
    _set_usage(span, getattr(result, "usage", None))


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
        if Protect is not None:
            self._original_protect = inspect.getattr_static(Protect, "protect")
            wrap_function_wrapper(
                module="fi.evals",
                name="Protect.protect",
                wrapper=GuardrailProtectWrapper(tracer=self._tracer),
            )
            self._wrapped_protect = inspect.getattr_static(Protect, "protect")
        else:
            self._original_protect = None
            self._wrapped_protect = None

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
        if Protect is not None and inspect.getattr_static(
            Protect, "protect"
        ) is getattr(self, "_wrapped_protect", None):
            Protect.protect = self._original_protect
        self._original_protect = None
        self._wrapped_protect = None
        self._tracer = None

    def _request(self, name, args, kwargs):
        """Bind positional arguments without changing the provider call."""
        try:
            signature = inspect.signature(self.original_litellm_funcs[name])
            bound = signature.bind_partial(*args, **kwargs)
            request = dict(bound.arguments)
            for key, param in signature.parameters.items():
                if param.kind is inspect.Parameter.VAR_KEYWORD:
                    request.update(request.pop(key, {}))
            # Async endpoints and retry helpers may expose only *args/**kwargs.
            endpoint = {
                "acompletion": "completion",
                "completion_with_retries": "completion",
                "acompletion_with_retries": "completion",
                "aembedding": "embedding",
                "aimage_generation": "image_generation",
            }.get(name)
            for key, param in signature.parameters.items():
                if param.kind is inspect.Parameter.VAR_POSITIONAL and key in request:
                    positional = request.pop(key)
                    if endpoint:
                        request.update(
                            inspect.signature(self.original_litellm_funcs[endpoint])
                            .bind_partial(*positional)
                            .arguments
                        )
        except (TypeError, ValueError):
            request = dict(kwargs)
        # Capture generation inputs/settings, not arbitrary SDK control objects,
        # credentials, HTTP headers, callbacks, or provider-specific secrets.
        return {key: value for key, value in request.items() if key in _REQUEST_FIELDS}

    def _call(self, name, annotate, args, kwargs):
        original = self.original_litellm_funcs[name]
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return original(*args, **kwargs)
        request = self._request(name, args, kwargs)
        span = self._tracer.start_span(
            name, attributes=dict(get_attributes_from_context())
        )
        _observe(annotate, span, request)
        try:
            with _provider_context(span):
                result = original(*args, **kwargs)
            if request.get("stream") and hasattr(result, "__next__"):
                return StreamingIteratorWrapper(result, span)
            _observe(_finalize_span, span, result)
            span.set_status(trace_api.StatusCode.OK)
        except BaseException as error:
            _record_error(span, error)
            span.end()
            raise
        span.end()
        return result

    async def _acall(self, name, annotate, args, kwargs):
        original = self.original_litellm_funcs[name]
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return await original(*args, **kwargs)
        request = self._request(name, args, kwargs)
        span = self._tracer.start_span(
            name, attributes=dict(get_attributes_from_context())
        )
        _observe(annotate, span, request)
        try:
            with _provider_context(span):
                result = await original(*args, **kwargs)
            if request.get("stream") and hasattr(result, "__anext__"):
                return AsyncStreamingIteratorWrapper(result, span)
            _observe(_finalize_span, span, result)
            span.set_status(trace_api.StatusCode.OK)
        except BaseException as error:
            _record_error(span, error)
            span.end()
            raise
        span.end()
        return result

    @wraps(litellm.completion)
    def _completion_wrapper(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("completion", _instrument_func_type_completion, args, kwargs)

    @wraps(litellm.acompletion)
    async def _acompletion_wrapper(self, *args: Any, **kwargs: Any) -> Any:
        return await self._acall(
            "acompletion", _instrument_func_type_completion, args, kwargs
        )

    @wraps(litellm.completion_with_retries)
    def _completion_with_retries_wrapper(self, *args: Any, **kwargs: Any) -> Any:
        return self._call(
            "completion_with_retries", _instrument_func_type_completion, args, kwargs
        )

    @wraps(litellm.acompletion_with_retries)
    async def _acompletion_with_retries_wrapper(self, *args: Any, **kwargs: Any) -> Any:
        return await self._acall(
            "acompletion_with_retries", _instrument_func_type_completion, args, kwargs
        )

    @wraps(litellm.embedding)
    def _embedding_wrapper(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("embedding", _instrument_func_type_embedding, args, kwargs)

    @wraps(litellm.aembedding)
    async def _aembedding_wrapper(self, *args: Any, **kwargs: Any) -> Any:
        return await self._acall(
            "aembedding", _instrument_func_type_embedding, args, kwargs
        )

    @wraps(litellm.image_generation)
    def _image_generation_wrapper(self, *args: Any, **kwargs: Any) -> Any:
        return self._call(
            "image_generation", _instrument_func_type_image_generation, args, kwargs
        )

    @wraps(litellm.aimage_generation)
    async def _aimage_generation_wrapper(self, *args: Any, **kwargs: Any) -> Any:
        return await self._acall(
            "aimage_generation", _instrument_func_type_image_generation, args, kwargs
        )

    def _set_wrapper_attr(self, func_wrapper: Any) -> None:
        func_wrapper.__func__.is_wrapper = True


def _observe(function, *args):
    """Telemetry extraction must not change a successful SDK operation."""
    try:
        function(*args)
    except Exception:
        # Do not log request, response, or exception content (which can include secrets).
        logger.debug("Could not extract LiteLLM telemetry")


@contextmanager
def _provider_context(span):
    # Keep a span current only during provider work, never across a yielded chunk.
    # Suppress nested LiteLLM/OpenAI instrumentation for the same request.
    with trace_api.use_span(
        span, end_on_exit=False, record_exception=False, set_status_on_exception=False
    ):
        token = context_api.attach(
            context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True)
        )
        try:
            yield
        finally:
            context_api.detach(token)


def _record_error(span, error):
    span.set_status(trace_api.StatusCode.ERROR)
    span.record_exception(error)


def _set_usage(span, usage):
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    if not isinstance(usage, Mapping):
        return
    for key, attribute in (
        ("prompt_tokens", SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS),
        ("completion_tokens", SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS),
        ("total_tokens", SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS),
    ):
        _set_span_attribute(span, attribute, usage.get(key))


class _StreamState:
    def __init__(self, span, span_cm=None):
        self.span = span
        self.span_cm = span_cm
        self.processed_chunks = []
        self.content_string = ""
        self.has_tool_context = False
        self._ended = False
        self._closed = False

    def _process(self, chunk):
        if hasattr(chunk, "to_dict"):
            chunk = chunk.to_dict()
        elif hasattr(chunk, "__dict__"):
            chunk = chunk.__dict__
        if not isinstance(chunk, Mapping):
            return
        self.processed_chunks.append(chunk)
        choices = chunk.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            self.has_tool_context |= _has_tool_context(delta)
            content = delta.get("content")
            if isinstance(content, str):
                self.content_string += content
        _set_usage(self.span, chunk.get("usage"))
        _set_span_attribute(
            self.span, SpanAttributes.GEN_AI_RESPONSE_MODEL, chunk.get("model")
        )

    def _finish(self, error=None):
        if self._ended:
            return
        self._ended = True
        try:
            _set_span_attribute(
                self.span,
                SpanAttributes.OUTPUT_VALUE,
                (
                    self.content_string
                    if self.content_string and not self.has_tool_context
                    else safe_json_dumps(self.processed_chunks)
                ),
            )
            if error is None:
                self.span.set_status(trace_api.StatusCode.OK)
            else:
                _record_error(self.span, error)
        finally:
            if self.span_cm is not None:
                self.span_cm.__exit__(
                    type(error) if error else None,
                    error,
                    error.__traceback__ if error else None,
                )
            else:
                self.span.end()


def _stream_close_method(stream, *, asynchronous=False):
    """Use the SDK close API, or its provider stream on older LiteLLM versions."""
    seen = set()
    while stream is not None and id(stream) not in seen:
        seen.add(id(stream))
        if asynchronous and callable(close := getattr(stream, "aclose", None)):
            return close
        if callable(close := getattr(stream, "close", None)):
            return close
        if not isinstance(stream, litellm.CustomStreamWrapper):
            break
        stream = stream.completion_stream
    return None


class StreamingIteratorWrapper(_StreamState):
    def __init__(self, iterator: Iterator[Any], span: trace_api.Span, span_cm=None):
        super().__init__(span, span_cm)
        self.iterator = iterator

    def __iter__(self):
        return self

    def __next__(self):
        if self._ended:
            raise StopIteration
        try:
            with _provider_context(self.span):
                chunk = next(self.iterator)
        except StopIteration:
            self._finish()
            raise
        except BaseException as error:
            self._finish(error)
            raise
        _observe(self._process, chunk)
        return chunk

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            close = _stream_close_method(self.iterator)
            if close is not None:
                with _provider_context(self.span):
                    close()
        except BaseException as error:
            self._finish(error)
            raise
        finally:
            self._finish()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_value is not None:
            self._finish(exc_value)
        self.close()


class AsyncStreamingIteratorWrapper(_StreamState):
    def __init__(
        self, async_iterator: AsyncIterator[Any], span: trace_api.Span, span_cm=None
    ):
        super().__init__(span, span_cm)
        self.async_iterator = async_iterator

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._ended:
            raise StopAsyncIteration
        try:
            with _provider_context(self.span):
                chunk = await self.async_iterator.__anext__()
        except StopAsyncIteration:
            self._finish()
            raise
        except BaseException as error:
            self._finish(error)
            raise
        _observe(self._process, chunk)
        return chunk

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        try:
            close = _stream_close_method(self.async_iterator, asynchronous=True)
            if close is not None:
                with _provider_context(self.span):
                    result = close()
                    if inspect.isawaitable(result):
                        await result
        except BaseException as error:
            self._finish(error)
            raise
        finally:
            self._finish()

    async def close(self):
        # Compatibility with the previous wrapper's async close() method.
        await self.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        if exc_value is not None:
            self._finish(exc_value)
        await self.aclose()


def _set_raw_output(span: trace_api.Span, result: Any) -> None:
    if not result:
        return
    if hasattr(result, "to_dict"):
        _set_span_attribute(
            span, SpanAttributes.OUTPUT_VALUE, safe_json_dumps(result.to_dict())
        )
    elif hasattr(result, "__dict__"):
        _set_span_attribute(
            span, SpanAttributes.OUTPUT_VALUE, safe_json_dumps(result.__dict__)
        )
    else:
        _set_span_attribute(span, SpanAttributes.OUTPUT_VALUE, safe_json_dumps(result))


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
        }
    except Exception as e:
        print(f"Error in _process_messages: {e}")
        return {
            "input_images": None,
            "filtered_messages": messages,
            "eval_input": None,
        }
