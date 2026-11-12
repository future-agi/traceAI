import json
from enum import Enum
from contextlib import contextmanager
from functools import wraps
from inspect import Parameter, signature
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Collection,
    Dict,
    Iterable,
    Iterator,
    Mapping,
    Optional,
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
)
from fi_instrumentation.instrumentation._protect_wrapper import GuardrailProtectWrapper
from litellm.types.utils import Choices, EmbeddingResponse, ImageResponse, ModelResponse
from openai.types.image import Image
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from opentelemetry.util.types import AttributeValue
from pydantic import BaseModel
from traceai_litellm.package import _instruments
from traceai_litellm.version import __version__
from wrapt import wrap_function_wrapper


# Helper functions to set span attributes
def _set_span_attribute(span: trace_api.Span, name: str, value: AttributeValue) -> None:
    if value is not None and value != "":
        span.set_attribute(name, value)


def _set_streaming_usage_attributes(span: trace_api.Span, usage: Any) -> None:
    if usage is None:
        return
    # Stream usage is cumulative: overwrite reported counts rather than sum them.
    for field, attribute in (
        ("prompt_tokens", SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS),
        ("completion_tokens", SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS),
        ("total_tokens", SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS),
    ):
        value = (
            usage.get(field)
            if isinstance(usage, Mapping)
            else getattr(usage, field, None)
        )
        _set_span_attribute(span, attribute, value)


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


# Completion kwargs also contain provider credentials, headers and clients.
# Capture only known inference controls, never arbitrary provider-specific data.
_COMPLETION_PARAMETER_TYPES = {
    "temperature": (int, float),
    "top_p": (int, float),
    "top_k": (int,),
    "n": (int,),
    "max_tokens": (int,),
    "max_completion_tokens": (int,),
    "frequency_penalty": (int, float),
    "presence_penalty": (int, float),
    "seed": (int,),
    "logprobs": (bool, int),
    "top_logprobs": (int,),
    "stream": (bool,),
    "parallel_tool_calls": (bool,),
    "reasoning_effort": (str,),
    "stop": (str,),
}


def _get_completion_invocation_parameters(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Project telemetry without inspecting or mutating provider configuration.

    Omit opaque mappings/sequences entirely (including extra_body, metadata,
    headers and structured provider options), rather than guessing credential
    names at each nesting level. Exact builtin types also prevent serializing
    client objects or subclasses with custom string representations.
    """
    parameters = {}
    for name, allowed_types in _COMPLETION_PARAMETER_TYPES.items():
        if name in kwargs:
            value = kwargs[name]
            if value is None or type(value) in allowed_types:
                parameters[name] = value

    # Stop sequences are the only supported container, and cannot nest objects.
    stop = kwargs.get("stop")
    if type(stop) in (list, tuple) and all(type(item) is str for item in stop):
        parameters["stop"] = list(stop)
    return parameters


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
    try:
        _set_completion_attributes(span, kwargs)
    except Exception:
        # Request telemetry must never prevent the wrapped provider call. Even
        # safe_json_dumps can fail on circular values or a failing __str__.
        logger.warning("Failed to extract LiteLLM completion request attributes")


def _set_completion_attributes(span: trace_api.Span, kwargs: Dict[str, Any]) -> None:
    invocation_params = _get_completion_invocation_parameters(kwargs)
    # Never write raw kwargs, even temporarily before the message-derived input.
    _set_span_attribute(
        span, SpanAttributes.INPUT_VALUE, safe_json_dumps(invocation_params)
    )
    _set_span_attribute(span, SpanAttributes.GEN_AI_SPAN_KIND, FiSpanKindValues.LLM.value)
    _set_span_attribute(
        span, SpanAttributes.GEN_AI_REQUEST_MODEL, kwargs.get("model", "unknown_model")
    )

    if messages := kwargs.get("messages"):
        process_messages = _process_messages(messages)
        if filtered_messages := process_messages.get("filtered_messages"):
            # Keep the full conversation as canonical input, not a derived text/query.
            _set_span_attribute(
                span, SpanAttributes.INPUT_VALUE, safe_json_dumps(filtered_messages)
            )
        if input_images := process_messages.get("input_images"):
            _set_span_attribute(
                span, SpanAttributes.INPUT_IMAGES, safe_json_dumps(input_images)
            )

        for index, input_message in list(enumerate(messages)):
            for key, value in _get_attributes_from_message_param(input_message):
                _set_span_attribute(
                    span, f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{index}.{key}", value
                )

    response_format = invocation_params.get("response_format")
    if isinstance(response_format, type) and issubclass(response_format, BaseModel):
        # Normalize only the telemetry copy; LiteLLM needs the original class.
        invocation_params["response_format"] = response_format.model_json_schema()
    _set_span_attribute(
        span, SpanAttributes.GEN_AI_REQUEST_PARAMETERS, safe_json_dumps(invocation_params)
    )


def _instrument_func_type_embedding(
    span: trace_api.Span, kwargs: Dict[str, Any]
) -> None:
    """
    Currently instruments the functions:
        litellm.embedding()
        litellm.aembedding() (async version of embedding)
    """
    # Only the embedding input is telemetry; provider kwargs may contain secrets.
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
    span: trace_api.Span, kwargs: Dict[str, Any], args: Tuple[Any, ...] = ()
) -> None:
    """
    Currently instruments the functions:
        litellm.image_generation()
        litellm.aimage_generation() (async version of image_generation)
    """
    _set_span_attribute(span, SpanAttributes.GEN_AI_SPAN_KIND, FiSpanKindValues.LLM.value)
    if model := kwargs.get("model"):
        _set_span_attribute(span, SpanAttributes.GEN_AI_REQUEST_MODEL, model)
    # LiteLLM's prompt is positional-or-keyword, including through its async API.
    # Project only this input; never serialize the remaining args/provider kwargs.
    prompt = args[0] if args else kwargs.get("prompt")
    if prompt:
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
                        span, f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{key}", value
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
        _set_span_attribute(span, f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{0}.{MessageAttributes.MESSAGE_ROLE}", "assistant")
        for idx,img_data in enumerate(result.data):
            _set_span_attribute(span, f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{0}.{MessageAttributes.MESSAGE_CONTENT}.{idx}.{MessageContentAttributes.MESSAGE_CONTENT_TYPE}", "image")
            if img_data:
                if isinstance(img_data, Image) and (
                    url := (img_data.url or img_data.b64_json)
                ):
                    _set_span_attribute(span, f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{0}.{MessageAttributes.MESSAGE_CONTENT}.{idx}.{MessageContentAttributes.MESSAGE_CONTENT_IMAGE}", url)
                    _set_span_attribute(span, SpanAttributes.OUTPUT_VALUE, url)
                elif isinstance(img_data, dict) and (
                    url := (img_data.get("url") or img_data.get("b64_json"))
                ):
                    _set_span_attribute(span, f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{0}.{MessageAttributes.MESSAGE_CONTENT}.{idx}.{MessageContentAttributes.MESSAGE_CONTENT_IMAGE}", url)
                    _set_span_attribute(span, SpanAttributes.OUTPUT_VALUE, url)
    usage = getattr(result, "usage", None)
    if usage is None:
        return
    # Image usage has different field names; retain legacy chat-style fallbacks.
    # Missing or unrecognized usage is missing telemetry, not an application error.
    is_image = isinstance(result, ImageResponse)
    for attribute, keys in (
        (
            SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS,
            ("input_tokens", "prompt_tokens") if is_image else ("prompt_tokens",),
        ),
        (
            SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS,
            ("output_tokens", "completion_tokens") if is_image else ("completion_tokens",),
        ),
        (SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS, ("total_tokens",)),
    ):
        for key in keys:
            value = (
                usage.get(key)
                if isinstance(usage, Mapping)
                else getattr(usage, key, None)
            )
            if value is not None:
                _set_span_attribute(span, attribute, value)
                break


def _media_capture(instrumentor: Any, span: trace_api.Span, arguments: Optional[Mapping[str, Any]] = None) -> Any:
    """Capture typed chat media of this call; returns the capture (also for output), or None."""
    attachments = getattr(instrumentor, "_attachments", None)
    if attachments is None:
        return None
    try:
        from fi_instrumentation.attachments.provider_media import ProviderMedia

        media = ProviderMedia(attachments, span, "litellm", trace_config=getattr(instrumentor, "_media_config", None))
        if not media.usable:
            return None
        if arguments is not None:
            media.compatible_messages(arguments.get("messages"), "openai")
        return media
    except Exception:
        logger.debug("Attachment capture unavailable", exc_info=True)
        return None


@contextmanager
def _media_owned(media: Any) -> Iterator[None]:
    """Run LiteLLM's downstream SDK call as the owner of this call's media.

    A nested provider instrumentor (for example OpenAI under LiteLLM) then
    uploads nothing for the same call. Only set while this adapter captures.
    """
    if media is None:
        yield
        return
    from fi_instrumentation.attachments.provider_media import claim

    token = context_api.attach(claim())
    try:
        yield
    finally:
        context_api.detach(token)


def _media_generated(media: Any, result: Any) -> None:
    if media is None or not isinstance(result, ImageResponse):
        return
    try:
        media.generated_images(result.data)
    except Exception:
        logger.debug("Attachment capture unavailable", exc_info=True)


def _attachment_manager(kwargs: Any) -> Any:
    """The shared manager passed as ``attachments`` (or auto's ``attachment_manager``), if it can admit.

    A missing, disabled or pre-attachments core leaves tracing exactly as before.
    """
    if kwargs.get("attachments") is None and kwargs.get("attachment_manager") is None:
        return None
    try:
        from fi_instrumentation.attachments.provider_media import manager_from_kwargs
    except ImportError:
        return None
    return manager_from_kwargs(kwargs)


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
        # Provider-owned shared attachment manager, or None (no capture, no imports).
        self._attachments = _attachment_manager(kwargs)
        self._media_config = config

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
            self._original_protect = Protect.protect
            wrap_function_wrapper(
                module="fi.evals",
                name="Protect.protect",
                wrapper=GuardrailProtectWrapper(tracer=self._tracer),
            )
        else:
            self._original_protect = None

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
        self._tracer = None

    def _get_call_arguments(
        self, func_name: str, args: Tuple[Any, ...], kwargs: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Bind tracing inputs without changing the call or adding defaults."""
        if not args:
            return kwargs
        positional_kinds = (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
        try:
            call_signature = signature(self.original_litellm_funcs[func_name])
            if not any(
                parameter.kind in positional_kinds
                for parameter in call_signature.parameters.values()
            ):
                # Variadic async/retry entry points forward the same positional
                # arguments to the corresponding synchronous API.
                base_name = {
                    "acompletion": "completion",
                    "completion_with_retries": "completion",
                    "acompletion_with_retries": "completion",
                    "aembedding": "embedding",
                    "aimage_generation": "image_generation",
                }.get(func_name)
                if base_name is not None and base_name in self.original_litellm_funcs:
                    call_signature = signature(self.original_litellm_funcs[base_name])
            bound = call_signature.bind_partial(*args, **kwargs)
        except (TypeError, ValueError):
            # Unavailable signatures or invalid arguments must not prevent the
            # original callable from returning or raising its own exception.
            return kwargs
        arguments = dict(kwargs)
        arguments.update(
            (name, value)
            for name, value in bound.arguments.items()
            if call_signature.parameters[name].kind in positional_kinds
        )
        return arguments

    @wraps(litellm.completion)
    def _completion_wrapper(self, *args: Any, **kwargs: Any) -> Any:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return self.original_litellm_funcs["completion"](*args, **kwargs)
        arguments = self._get_call_arguments("completion", args, kwargs)
        streaming = arguments.get("stream", False)
        if not streaming:
            with self._tracer.start_as_current_span(
                name="completion", attributes=dict(get_attributes_from_context())
            ) as span:
                try:
                    _instrument_func_type_completion(span, arguments)
                    media = _media_capture(self, span, arguments)
                    with _media_owned(media):
                        result = self.original_litellm_funcs["completion"](*args, **kwargs)
                    _finalize_span(span, result)
                    span.set_status(trace_api.StatusCode.OK)
                    return result
                except Exception as e:
                    span.record_exception(e)
                    raise
        else:
            span_cm = self._tracer.start_as_current_span(
                name="completion", attributes=dict(get_attributes_from_context())
            )
            span = span_cm.__enter__()
            try:
                _instrument_func_type_completion(span, arguments)
                media = _media_capture(self, span, arguments)
                with _media_owned(media):
                    original_iterator = self.original_litellm_funcs["completion"](
                        *args, **kwargs
                    )
                return StreamingIteratorWrapper(original_iterator, span, span_cm)
            except BaseException as e:
                span_cm.__exit__(type(e), e, e.__traceback__)
                raise

    @wraps(litellm.acompletion)
    async def _acompletion_wrapper(self, *args: Any, **kwargs: Any) -> Any:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return await self.original_litellm_funcs["acompletion"](*args, **kwargs)
        arguments = self._get_call_arguments("acompletion", args, kwargs)
        streaming = arguments.get("stream", False)
        if not streaming:
            with self._tracer.start_as_current_span(
                name="acompletion", attributes=dict(get_attributes_from_context())
            ) as span:
                try:
                    _instrument_func_type_completion(span, arguments)
                    media = _media_capture(self, span, arguments)
                    with _media_owned(media):
                        result = await self.original_litellm_funcs["acompletion"](
                            *args, **kwargs
                        )
                    _finalize_span(span, result)
                    span.set_status(trace_api.StatusCode.OK)
                    return result
                except Exception as e:
                    span.record_exception(e)
                    raise
        else:
            span_cm = self._tracer.start_as_current_span(
                name="acompletion", attributes=dict(get_attributes_from_context())
            )
            span = span_cm.__enter__()
            try:
                _instrument_func_type_completion(span, arguments)
                media = _media_capture(self, span, arguments)
                with _media_owned(media):
                    original_async_iterator = await self.original_litellm_funcs[
                        "acompletion"
                    ](*args, **kwargs)
                return AsyncStreamingIteratorWrapper(
                    original_async_iterator, span, span_cm
                )
            except BaseException as e:
                # Context exit takes the exception type before the instance.
                span_cm.__exit__(type(e), e, e.__traceback__)
                raise

    @wraps(litellm.completion_with_retries)
    def _completion_with_retries_wrapper(
        self, *args: Any, **kwargs: Any
    ) -> ModelResponse:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return self.original_litellm_funcs["completion_with_retries"](
                *args, **kwargs
            )  # type:ignore
        arguments = self._get_call_arguments("completion_with_retries", args, kwargs)
        with self._tracer.start_as_current_span(
            name="completion_with_retries",
            attributes=dict(get_attributes_from_context()),
        ) as span:
            try:
                _instrument_func_type_completion(span, arguments)
                media = _media_capture(self, span, arguments)
                with _media_owned(media):
                    result = self.original_litellm_funcs["completion_with_retries"](
                        *args, **kwargs
                    )
                _finalize_span(span, result)
                span.set_status(trace_api.StatusCode.OK)
                return result  # type:ignore
            except Exception as e:
                span.record_exception(e)
                raise

    @wraps(litellm.acompletion_with_retries)
    async def _acompletion_with_retries_wrapper(
        self, *args: Any, **kwargs: Any
    ) -> ModelResponse:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return await self.original_litellm_funcs["acompletion_with_retries"](
                *args, **kwargs
            )  # type:ignore
        arguments = self._get_call_arguments("acompletion_with_retries", args, kwargs)
        with self._tracer.start_as_current_span(
            name="acompletion_with_retries",
            attributes=dict(get_attributes_from_context()),
        ) as span:
            try:
                _instrument_func_type_completion(span, arguments)
                media = _media_capture(self, span, arguments)
                with _media_owned(media):
                    result = await self.original_litellm_funcs[
                        "acompletion_with_retries"
                    ](*args, **kwargs)
                _finalize_span(span, result)
                span.set_status(trace_api.StatusCode.OK)
                return result  # type:ignore
            except Exception as e:
                span.record_exception(e)
                raise

    @wraps(litellm.embedding)
    def _embedding_wrapper(self, *args: Any, **kwargs: Any) -> EmbeddingResponse:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return self.original_litellm_funcs["embedding"](
                *args, **kwargs
            )  # type:ignore
        arguments = self._get_call_arguments("embedding", args, kwargs)
        with self._tracer.start_as_current_span(
            name="embedding", attributes=dict(get_attributes_from_context())
        ) as span:
            try:
                _instrument_func_type_embedding(span, arguments)
                result = self.original_litellm_funcs["embedding"](*args, **kwargs)
                _finalize_span(span, result)
                span.set_status(trace_api.StatusCode.OK)
                return result  # type:ignore
            except Exception as e:
                span.record_exception(e)
                raise

    @wraps(litellm.aembedding)
    async def _aembedding_wrapper(self, *args: Any, **kwargs: Any) -> EmbeddingResponse:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return await self.original_litellm_funcs["aembedding"](
                *args, **kwargs
            )  # type:ignore
        arguments = self._get_call_arguments("aembedding", args, kwargs)
        with self._tracer.start_as_current_span(
            name="aembedding", attributes=dict(get_attributes_from_context())
        ) as span:
            try:
                _instrument_func_type_embedding(span, arguments)
                result = await self.original_litellm_funcs["aembedding"](
                    *args, **kwargs
                )
                _finalize_span(span, result)
                span.set_status(trace_api.StatusCode.OK)
                return result  # type:ignore
            except Exception as e:
                span.record_exception(e)
                raise

    @wraps(litellm.image_generation)
    def _image_generation_wrapper(self, *args: Any, **kwargs: Any) -> ImageResponse:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return self.original_litellm_funcs["image_generation"](
                *args, **kwargs
            )  # type:ignore
        arguments = self._get_call_arguments("image_generation", args, kwargs)
        with self._tracer.start_as_current_span(
            name="image_generation", attributes=dict(get_attributes_from_context())
        ) as span:
            try:
                _instrument_func_type_image_generation(span, arguments, args)
                media = _media_capture(self, span)
                with _media_owned(media):
                    result = self.original_litellm_funcs["image_generation"](
                        *args, **kwargs
                    )
                _media_generated(media, result)
                _finalize_span(span, result)
                span.set_status(trace_api.StatusCode.OK)
                return result  # type:ignore
            except Exception as e:
                span.record_exception(e)
                raise

    @wraps(litellm.aimage_generation)
    async def _aimage_generation_wrapper(
        self, *args: Any, **kwargs: Any
    ) -> ImageResponse:
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return await self.original_litellm_funcs["aimage_generation"](
                *args, **kwargs
            )  # type:ignore
        arguments = self._get_call_arguments("aimage_generation", args, kwargs)
        with self._tracer.start_as_current_span(
            name="aimage_generation", attributes=dict(get_attributes_from_context())
        ) as span:
            try:
                _instrument_func_type_image_generation(span, arguments, args)
                media = _media_capture(self, span)
                with _media_owned(media):
                    result = await self.original_litellm_funcs["aimage_generation"](
                        *args, **kwargs
                    )
                _media_generated(media, result)
                _finalize_span(span, result)
                span.set_status(trace_api.StatusCode.OK)
                return result  # type:ignore
            except Exception as e:
                span.record_exception(e)
                raise

    def _set_wrapper_attr(self, func_wrapper: Any) -> None:
        func_wrapper.__func__.is_wrapper = True


class StreamingIteratorWrapper:
    def __init__(self, iterator: Iterator[Any], span: trace_api.Span, span_cm):
        self.iterator = iterator
        self.span = span
        self.span_cm = span_cm
        self._finished = False
        self.processed_chunks = []
        self.content_string = ""

    def __iter__(self):
        return self

    def __next__(self):
        if self._finished:
            return next(self.iterator)
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
            _set_streaming_usage_attributes(self.span, chunk_dict.get("usage"))

            if (
                content := (chunk_dict.get("choices") or [{}])[0]
                .get("delta", {})
                .get("content")
            ):
                self.content_string += content

            return chunk
        except StopIteration:
            try:
                # After the iterator is exhausted, set the span attributes
                _set_span_attribute(
                    self.span,
                    SpanAttributes.OUTPUT_VALUE,
                    safe_json_dumps(self.processed_chunks),
                )
                _set_span_attribute(
                    self.span, SpanAttributes.OUTPUT_VALUE, self.content_string
                )
                self.span.set_status(trace_api.StatusCode.OK)
            finally:
                self._finish_span()
            raise
        except BaseException as e:
            self._finish_span(type(e), e, e.__traceback__)
            raise

    def _finish_span(self, exc_type=None, exc_value=None, traceback=None):
        # Exhaustion, failed iteration and context exit can all reach cleanup.
        if not self._finished:
            self._finished = True
            self.span_cm.__exit__(exc_type, exc_value, traceback)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        # If the iterator exits early, we need to close the span
        self._finish_span(exc_type, exc_value, traceback)


class AsyncStreamingIteratorWrapper:
    def __init__(
        self, async_iterator: AsyncIterator[Any], span: trace_api.Span, span_cm
    ):
        self.async_iterator = async_iterator
        self.span = span
        self.span_cm = span_cm
        self._finished = False
        self.processed_chunks = []
        self.content_string = ""

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._finished:
            return await self.async_iterator.__anext__()
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
            _set_streaming_usage_attributes(self.span, chunk_dict.get("usage"))

            if (
                content := (chunk_dict.get("choices") or [{}])[0]
                .get("delta", {})
                .get("content")
            ):
                self.content_string += content

            return chunk
        except StopAsyncIteration:
            try:
                # After the iterator is exhausted, set the span attributes
                _set_span_attribute(
                    self.span,
                    SpanAttributes.OUTPUT_VALUE,
                    safe_json_dumps(self.processed_chunks),
                )
                _set_span_attribute(
                    self.span, SpanAttributes.OUTPUT_VALUE, self.content_string
                )
                self.span.set_status(trace_api.StatusCode.OK)
            finally:
                self._finish_span()
            raise
        except BaseException as e:
            self._finish_span(type(e), e, e.__traceback__)
            raise

    def _finish_span(self, exc_type=None, exc_value=None, traceback=None):
        # A caller may close the stream after exhaustion, failure or cancellation.
        if not self._finished:
            self._finished = True
            self.span_cm.__exit__(exc_type, exc_value, traceback)

    async def close(self):
        # Explicitly close the span if needed
        self._finish_span()


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


def __getattr__(name: str) -> Any:
    # Lazy: the descriptor needs a fi-instrumentation-otel release with attachments.
    if name == "litellm_attachment_capabilities":
        from fi_instrumentation.attachments.provider_media import capabilities

        return capabilities("traceai_litellm")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
