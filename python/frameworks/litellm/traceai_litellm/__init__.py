import json
import inspect
import weakref
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
try:
    from fi_instrumentation.instrumentation.config import ChatInput
except ImportError:
    # The declared published 1.2.2 floor predates the structured-input API and
    # the opaque-input guard. Retain its existing path, not a validation fallback.
    ChatInput = None
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
from traceai_litellm._responses import ResponseEvents, as_dict, response_messages
from traceai_litellm.package import _instruments
from traceai_litellm.version import __version__
from wrapt import wrap_function_wrapper

_REQUEST_FIELDS = frozenset(
    {
        "model",
        "custom_llm_provider",
        "instructions",
        "max_output_tokens",
        "reasoning",
        "text",
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


def _has_non_text_content(message):
    """Keep blocks without dedicated text/image attributes in input.value."""
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(item, Mapping) and item.get("type") not in ("text", "image_url")
        for item in content
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
            yield (
                f"{MessageContentAttributes.MESSAGE_CONTENT_IMAGE}.{ImageAttributes.IMAGE_URL}",
                image.get("url", ""),
            )


def _get_attributes_from_image(
    image: Mapping[str, Any],
) -> Iterator[Tuple[str, AttributeValue]]:
    image = dict(image)
    if url := image.pop("url"):
        yield f"{ImageAttributes.IMAGE_URL}", url


def _telemetry_tool(tool):
    """Project tool definitions, never connection configuration, into telemetry.

    MCP authorization/headers and server URLs (which can embed credentials) must
    reach the provider unchanged but must not reach an exporter. Unknown built-in
    tools are type-only until their configuration has a safe projection. Function
    parameter schemas are application data, not connection headers.
    """
    tool = as_dict(tool)
    kind = tool.get("type")
    fields = {"type"}
    if kind == "function":
        fields.update(("name", "description", "parameters", "strict"))
    elif kind == "mcp":
        fields.update(
            ("server_label", "server_description", "allowed_tools", "require_approval")
        )
    result = {key: value for key, value in tool.items() if key in fields}
    if kind == "function" and "function" in tool:
        result["function"] = {
            key: value for key, value in as_dict(tool["function"]).items()
            if key in ("name", "description", "parameters", "strict")
        }
    return result


def _telemetry_parameters(parameters):
    parameters = dict(parameters)
    if parameters.get("tools") is not None:
        tools = parameters["tools"]
        # Do not consume a caller-owned iterator while preparing telemetry.
        parameters["tools"] = (
            [_telemetry_tool(tool) for tool in tools]
            if isinstance(tools, (list, tuple)) else []
        )
    if isinstance(parameters.get("tool_choice"), Mapping):
        parameters["tool_choice"] = _telemetry_tool(parameters["tool_choice"])
    return parameters


def _instrument_func_type_completion(
    span: trace_api.Span,
    kwargs: Dict[str, Any],
    config: Optional[TraceConfig] = None,
) -> None:
    """
    Currently instruments the functions:
        litellm.completion()
        litellm.acompletion() (async version of completion)
        litellm.completion_with_retries()
        litellm.acompletion_with_retries() (async version of completion_with_retries)
    """
    _set_span_attribute(
        span, SpanAttributes.GEN_AI_SPAN_KIND, FiSpanKindValues.LLM.value
    )
    _set_span_attribute(
        span, SpanAttributes.GEN_AI_REQUEST_MODEL, kwargs.get("model", "unknown_model")
    )

    if messages := kwargs.get("messages"):
        process_messages = _process_messages(messages, config)
        # Core masks indexed message attributes, but input.value is an independent
        # aggregate. Omit it when either message-content privacy flag applies.
        hide_message_input = bool(config and (config.hide_input_text or config.hide_input_messages))
        if not hide_message_input and (filtered_messages := process_messages.get("filtered_messages")):
            # Emit the sanitized conversation as a typed ChatInput so core masks
            # it per item instead of fail-closing the whole opaque aggregate.
            _set_span_attribute(
                span,
                SpanAttributes.INPUT_VALUE,
                ChatInput({"model": kwargs.get("model"), "messages": filtered_messages})
                if ChatInput is not None
                else json.dumps(filtered_messages),
            )
        if input_images := process_messages.get("input_images"):
            _set_span_attribute(
                span, SpanAttributes.INPUT_IMAGES, json.dumps(input_images)
            )
        # Opaque aggregate fallbacks for cores without the structured ChatInput
        # API; the typed path above already carries the sanitized conversation.
        if not hide_message_input and ChatInput is None:
            if any(
                _has_tool_context(message) or _has_non_text_content(message)
                for message in messages
            ):
                _set_span_attribute(
                    span, SpanAttributes.INPUT_VALUE, safe_json_dumps(filtered_messages)
                )
            elif eval_input := process_messages.get("eval_input"):
                _set_span_attribute(span, SpanAttributes.INPUT_VALUE, eval_input)

        for index, input_message in list(enumerate(messages)):
            for key, value in _message_attributes(input_message, config, output=False):
                _set_span_attribute(
                    span, f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{index}.{key}", value
                )

    invocation_params = _telemetry_parameters({
        k: v for k, v in kwargs.items() if k not in ["model", "messages"]
    })
    _set_span_attribute(
        span,
        SpanAttributes.GEN_AI_REQUEST_PARAMETERS,
        safe_json_dumps(invocation_params),
    )


def _message_attributes(message, config=None, *, output=True):
    hide_text = config and (config.hide_output_text if output else config.hide_input_text)
    for key, value in _get_attributes_from_message_param(message):
        # Released core masks message text, but not tool/function argument strings.
        if hide_text and key.endswith((
            ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON,
            MessageAttributes.MESSAGE_FUNCTION_CALL_ARGUMENTS_JSON,
        )):
            continue
        yield key, value


def _instrument_func_type_text_completion(span, kwargs, config=None):
    # Prompt is content, not a generation parameter: never leave an unmasked
    # duplicate in request.parameters. Normalize strings/batches to messages.
    request = {key: value for key, value in kwargs.items() if key != "prompt"}
    prompt = kwargs.get("prompt")
    prompts = (
        prompt
        if isinstance(prompt, list) and all(isinstance(p, str) for p in prompt)
        else [prompt]
    )
    request["messages"] = [
        {
            "role": "user",
            "content": value if isinstance(value, str) else safe_json_dumps(value),
        }
        for value in prompts if value is not None
    ]
    _instrument_func_type_completion(span, request, config)


def _instrument_func_type_responses(span, kwargs, config=None):
    messages = response_messages(kwargs.get("input"))
    if kwargs.get("instructions"):
        messages.insert(0, {"role": "system", "content": kwargs["instructions"]})
    request = {k: v for k, v in kwargs.items() if k not in ("input", "instructions")}
    request["messages"] = messages
    _instrument_func_type_completion(span, request, config)


def _set_provider(span, request=None, result=None):
    # Read only normalized routing fields. Never serialize hidden metadata, which
    # can contain API keys/headers. Do not pass credentials to the resolver.
    hidden = as_dict(getattr(result, "_hidden_params", None))
    provider = hidden.get("custom_llm_provider") or getattr(result, "custom_llm_provider", None)
    if not provider and request:
        provider = request.get("custom_llm_provider")
        if not provider and request.get("model"):
            _, provider, _, _ = litellm.get_llm_provider(model=request["model"])
    if isinstance(provider, str):
        _set_span_attribute(span, SpanAttributes.GEN_AI_PROVIDER_NAME, provider)


def _set_result_metadata(span, result):
    data = as_dict(result)
    _set_span_attribute(span, SpanAttributes.GEN_AI_RESPONSE_MODEL, data.get("model"))
    _observe(_set_provider, span, None, result)
    _set_usage(span, data.get("usage"))
    # LiteLLM may supply a cost computed by its own accounting. Record what it
    # reports, but never invoke pricing/cost estimation from instrumentation.
    usage = as_dict(data.get("usage"))
    if usage.get("cost") is None:
        hidden = as_dict(getattr(result, "_hidden_params", None))
        _set_span_attribute(span, SpanAttributes.GEN_AI_COST_TOTAL, hidden.get("response_cost"))


def _private_response(data, config):
    """Do not leak echoed Responses inputs/metadata through raw output."""
    hide_error = config and getattr(config, "hide_error_details", False)
    kind = data.get("type", "")
    if kind == "error" and hide_error:
        return {"type": "error"}
    if data.get("object") == "response":
        # Responses snapshots echo instructions, tools and arbitrary metadata.
        # Inputs already have their own privacy-filtered attributes.
        fields = {"id", "object", "model", "status", "output", "usage", "error", "created_at"}
        data = {key: value for key, value in data.items() if key in fields}
        if hide_error:
            data.pop("error", None)
    elif kind.startswith("response."):
        data = dict(data)
        if hide_error:
            data.pop("error", None)
        if isinstance(data.get("response"), Mapping):
            response = {"object": "response", **data["response"]}
            data["response"] = _private_response(response, config)
    return data


def _set_response_messages(span, messages, config=None):
    for index, message in enumerate(messages):
        for key, value in _message_attributes(message, config):
            _set_span_attribute(span, f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}.{key}", value)


def _instrument_func_type_embedding(
    span: trace_api.Span,
    kwargs: Dict[str, Any],
    config: Optional[TraceConfig] = None,
) -> None:
    """
    Currently instruments the functions:
        litellm.embedding()
        litellm.aembedding() (async version of embedding)
    """
    config = config or TraceConfig()
    hide_message_input = config.hide_input_text or config.hide_input_messages
    if not hide_message_input:
        _set_span_attribute(span, SpanAttributes.INPUT_VALUE, safe_json_dumps(kwargs))
    _set_span_attribute(
        span,
        SpanAttributes.GEN_AI_SPAN_KIND,
        FiSpanKindValues.EMBEDDING.value,
    )
    _set_span_attribute(
        span, SpanAttributes.EMBEDDING_MODEL_NAME, kwargs.get("model", "unknown_model")
    )
    inputs = kwargs.get("input")
    # A string or a list of token IDs is one input; strings/token lists form a batch.
    batch = (
        inputs
        if isinstance(inputs, list) and (
            not inputs or not all(isinstance(item, int) for item in inputs)
        )
        else [inputs]
    )
    config = config or TraceConfig()
    for index, text in enumerate(batch):
        # Released core 1.2.2 does not yet mask embedding.text. All three
        # input-content flags cover this indexed copy in the source core.
        safe_text = (
            "__REDACTED__"
            if config.hide_inputs or config.hide_input_messages or config.hide_input_text
            else str(text)
        )
        _set_span_attribute(
            span,
            f"{SpanAttributes.EMBEDDING_EMBEDDINGS}.{index}.{EmbeddingAttributes.EMBEDDING_TEXT}",
            safe_text,
        )
    if not hide_message_input:
        _set_span_attribute(span, SpanAttributes.INPUT_VALUE, str(inputs))


def _instrument_func_type_image_generation(
    span: trace_api.Span,
    kwargs: Dict[str, Any],
    config: Optional[TraceConfig] = None,
) -> None:
    """
    Currently instruments the functions:
        litellm.image_generation()
        litellm.aimage_generation() (async version of image_generation)
    """
    if not (config and (config.hide_input_text or config.hide_input_messages)):
        _set_span_attribute(span, SpanAttributes.INPUT_VALUE, safe_json_dumps(kwargs))
    _set_span_attribute(
        span, SpanAttributes.GEN_AI_SPAN_KIND, FiSpanKindValues.LLM.value
    )
    if model := kwargs.get("model"):
        _set_span_attribute(span, SpanAttributes.GEN_AI_REQUEST_MODEL, model)
    if (prompt := kwargs.get("prompt")) and not (
        config and (config.hide_input_text or config.hide_input_messages)
    ):
        _set_span_attribute(span, SpanAttributes.INPUT_VALUE, str(prompt))


def _finish_reasons(choices):
    reasons = {}
    for position, choice in enumerate(choices or []):
        choice = as_dict(choice)
        index = choice.get("index")
        if index is None:
            index = position
        reason = choice.get("finish_reason")
        if isinstance(index, int) and isinstance(reason, str) and reason:
            reasons[index] = reason
    return reasons


def _set_finish_reasons(span, reasons):
    if not reasons:
        return
    _set_span_attribute(
        span, SpanAttributes.GEN_AI_RESPONSE_FINISH_REASONS,
        [reasons[index] for index in sorted(reasons)],
    )
    # The standard array is in choice-index order. Indexed attributes also retain
    # identity for sparse/interleaved choices, without inventing missing reasons.
    for index, reason in reasons.items():
        _set_span_attribute(
            span,
            f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}.message.finish_reason",
            reason,
        )


def _finalize_span(span: trace_api.Span, result: Any, config: Optional[TraceConfig] = None) -> None:
    # Embedding vectors and private message content must not reappear in the
    # independent raw response aggregate after core filters semantic attributes.
    hide_message_output = bool(config and (config.hide_output_text or config.hide_output_messages))
    if not isinstance(result, EmbeddingResponse) and not hide_message_output:
        _set_raw_output(span, _private_response(as_dict(result), config) or result)
    if isinstance(result, ModelResponse):
        choices = result.choices or []
        for position, choice in enumerate(choices):
            if isinstance(choice, Choices):
                if (
                    len(choices) == 1
                    and choice.message.content
                    and not hide_message_output
                    and not _has_tool_context(choice.message)
                ):
                    _set_span_attribute(
                        span, SpanAttributes.OUTPUT_VALUE, choice.message.content
                    )
                index = choice.index if choice.index is not None else position
                for key, value in _message_attributes(choice.message, config):
                    _set_span_attribute(
                        span, f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}.{key}", value
                    )
    elif isinstance(result, EmbeddingResponse):
        for position, embedding in enumerate(result.data or []):
            index = embedding.get("index", position)
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
                    if not hide_message_output:
                        _set_span_attribute(span, SpanAttributes.OUTPUT_VALUE, url)
                elif isinstance(img_data, dict) and (
                    url := (img_data.get("url") or img_data.get("b64_json"))
                ):
                    _set_span_attribute(
                        span,
                        f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{0}.{MessageAttributes.MESSAGE_CONTENT}.{idx}.{MessageContentAttributes.MESSAGE_CONTENT_IMAGE}",
                        url,
                    )
                    if not hide_message_output:
                        _set_span_attribute(span, SpanAttributes.OUTPUT_VALUE, url)
    elif as_dict(result).get("object") == "text_completion":
        choices = as_dict(result).get("choices") or []
        for position, choice in enumerate(choices):
            choice = as_dict(choice)
            index = choice.get("index")
            if index is None:
                index = position
            message = {"role": "assistant", "content": choice.get("text")}
            if len(choices) == 1 and not hide_message_output:
                _set_span_attribute(span, SpanAttributes.OUTPUT_VALUE, choice.get("text"))
            for key, value in _message_attributes(message, config):
                _set_span_attribute(span, f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}.{key}", value)
    elif as_dict(result).get("object") == "response":
        _set_response_messages(span, response_messages(as_dict(result).get("output")), config)
    _set_finish_reasons(span, _finish_reasons(as_dict(result).get("choices")))
    _set_result_metadata(span, result)


def _response_status(result):
    return (
        trace_api.StatusCode.ERROR if as_dict(result).get("status") == "failed"
        else trace_api.StatusCode.OK
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
        self._config = config
        self._tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider),
            config=config,
        )

        functions_to_instrument = {
            "completion": self._completion_wrapper,
            "acompletion": self._acompletion_wrapper,
            "text_completion": self._text_completion_wrapper,
            "atext_completion": self._atext_completion_wrapper,
            "completion_with_retries": self._completion_with_retries_wrapper,
            # Bug report filed on GitHub for acompletion_with_retries: https://github.com/BerriAI/litellm/issues/4908
            # "acompletion_with_retries": self._acompletion_with_retries_wrapper,
            "embedding": self._embedding_wrapper,
            "aembedding": self._aembedding_wrapper,
            "image_generation": self._image_generation_wrapper,
            "aimage_generation": self._aimage_generation_wrapper,
            # Guarded below: Responses APIs do not exist at the 1.43.0 floor.
            "responses": self._responses_wrapper,
            "aresponses": self._aresponses_wrapper,
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
                "atext_completion": "text_completion",
                "completion_with_retries": "completion",
                "acompletion_with_retries": "completion",
                "aembedding": "embedding",
                "aimage_generation": "image_generation",
                "aresponses": "responses",
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
        return _telemetry_parameters({
            key: value for key, value in request.items() if key in _REQUEST_FIELDS
        })

    def _call(self, name, annotate, args, kwargs):
        original = self.original_litellm_funcs[name]
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return original(*args, **kwargs)
        request = self._request(name, args, kwargs)
        span = self._tracer.start_span(
            name, attributes=dict(get_attributes_from_context())
        )
        _observe(annotate, span, request, self._config)
        _observe(_set_provider, span, request)
        try:
            with _provider_context(span):
                result = original(*args, **kwargs)
            _observe(_set_provider, span, None, result)
            if request.get("stream") and hasattr(result, "__next__"):
                return StreamingIteratorWrapper(result, span, config=self._config)
            span.set_status(_observe(_response_status, result) or trace_api.StatusCode.OK)
            _observe(_finalize_span, span, result, self._config)
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
        _observe(annotate, span, request, self._config)
        _observe(_set_provider, span, request)
        try:
            with _provider_context(span):
                result = await original(*args, **kwargs)
            _observe(_set_provider, span, None, result)
            if request.get("stream") and hasattr(result, "__anext__"):
                return AsyncStreamingIteratorWrapper(result, span, config=self._config)
            span.set_status(_observe(_response_status, result) or trace_api.StatusCode.OK)
            _observe(_finalize_span, span, result, self._config)
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

    def _text_completion_wrapper(self, *args: Any, **kwargs: Any) -> Any:
        return self._call(
            "text_completion", _instrument_func_type_text_completion, args, kwargs
        )

    async def _atext_completion_wrapper(self, *args: Any, **kwargs: Any) -> Any:
        return await self._acall(
            "atext_completion", _instrument_func_type_text_completion, args, kwargs
        )

    def _responses_wrapper(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("responses", _instrument_func_type_responses, args, kwargs)

    async def _aresponses_wrapper(self, *args: Any, **kwargs: Any) -> Any:
        return await self._acall("aresponses", _instrument_func_type_responses, args, kwargs)

    def _set_wrapper_attr(self, func_wrapper: Any) -> None:
        func_wrapper.__func__.is_wrapper = True


def _observe(function, *args):
    """Telemetry extraction must not change a successful SDK operation."""
    try:
        return function(*args)
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


def _first_present(*values):
    return next((value for value in values if value is not None), None)


def _set_usage(span, usage):
    usage = as_dict(usage)
    inputs = as_dict(_first_present(
        usage.get("prompt_tokens_details"), usage.get("input_tokens_details")
    ))
    outputs = as_dict(_first_present(
        usage.get("completion_tokens_details"), usage.get("output_tokens_details")
    ))
    for attribute, value in (
        (SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, _first_present(
            usage.get("prompt_tokens"), usage.get("input_tokens")
        )),
        (SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, _first_present(
            usage.get("completion_tokens"), usage.get("output_tokens")
        )),
        (SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS, usage.get("total_tokens")),
        (SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS_CACHE_READ, _first_present(
            inputs.get("cached_tokens"), inputs.get("cache_read_tokens"),
            usage.get("cache_read_input_tokens")
        )),
        (SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS_CACHE_WRITE, _first_present(
            inputs.get("cache_creation_tokens"), inputs.get("cache_write_tokens"),
            usage.get("cache_creation_input_tokens")
        )),
        (SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS_REASONING, outputs.get("reasoning_tokens")),
        (SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS_AUDIO, inputs.get("audio_tokens")),
        (SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS_AUDIO, outputs.get("audio_tokens")),
        (SpanAttributes.GEN_AI_COST_TOTAL, usage.get("cost")),
    ):
        _set_span_attribute(span, attribute, value)


class _StreamState:
    def __init__(self, span, span_cm=None, config=None):
        self.span = span
        self.span_cm = span_cm
        self.config = config
        self.processed_chunks = []
        self.content_by_choice = {}
        self.finish_reasons_by_choice = {}
        self.has_tool_context = False
        self.tools_by_choice = {}
        self.functions_by_choice = {}
        self.responses = ResponseEvents()
        self._ended = False

    def _process(self, chunk):
        _set_result_metadata(self.span, chunk)
        chunk = as_dict(chunk)
        self.processed_chunks.append(_private_response(chunk, self.config))
        if chunk.get("type", "").startswith("response.") or chunk.get("type") == "error":
            response = self.responses.add(chunk)
            _set_result_metadata(self.span, response)
            return
        choices = chunk.get("choices") or []
        self.finish_reasons_by_choice.update(_finish_reasons(choices))
        for position, choice in enumerate(choices):
            index = choice.get("index", position)
            delta = choice.get("delta") or {}
            if isinstance(choice.get("text"), str):
                delta = {"role": "assistant", "content": choice["text"]}
            self.has_tool_context |= _has_tool_context(delta)
            tools = self.tools_by_choice.setdefault(index, {})
            for tool_position, tool in enumerate(delta.get("tool_calls") or []):
                tool_index = tool.get("index", tool_position)
                target = tools.setdefault(tool_index, {"function": {}})
                if tool.get("id"):
                    target["id"] = tool["id"]
                _join_function(target["function"], tool.get("function") or {})
            if delta.get("function_call"):
                _join_function(self.functions_by_choice.setdefault(index, {}), delta["function_call"])
            self.content_by_choice.setdefault(index, "")
            content = delta.get("content")
            if isinstance(content, str):
                self.content_by_choice[index] += content
            _set_span_attribute(
                self.span,
                f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}.{MessageAttributes.MESSAGE_ROLE}",
                delta.get("role"),
            )

    def _finish(self, error=None, abandoned=False):
        if self._ended:
            return
        self._ended = True
        try:
            content = next(iter(self.content_by_choice.values()), "")
            if not (self.config and (self.config.hide_output_text or self.config.hide_output_messages)):
                _set_span_attribute(
                    self.span,
                    SpanAttributes.OUTPUT_VALUE,
                    (
                        content
                        if len(self.content_by_choice) == 1
                        and content
                        and not self.has_tool_context
                        else safe_json_dumps(self.processed_chunks)
                    ),
                )
            for index, text in self.content_by_choice.items():
                _set_span_attribute(
                    self.span,
                    f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}.{MessageAttributes.MESSAGE_CONTENT}",
                    text,
                )
            for index, tools in self.tools_by_choice.items():
                # Preserve both choice and tool indices even when interleaved.
                for tool_index, tool in tools.items():
                    for key, value in _message_attributes({"tool_calls": [tool]}, self.config):
                        key = key.replace(f"{MessageAttributes.MESSAGE_TOOL_CALLS}.0.", f"{MessageAttributes.MESSAGE_TOOL_CALLS}.{tool_index}.", 1)
                        _set_span_attribute(self.span, f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}.{key}", value)
            for index, function in self.functions_by_choice.items():
                for key, value in _message_attributes({"function_call": function}, self.config):
                    _set_span_attribute(self.span, f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}.{key}", value)
            _set_response_messages(self.span, self.responses.messages(), self.config)
            _set_finish_reasons(self.span, self.finish_reasons_by_choice)
            if error is not None:
                _record_error(self.span, error)
            elif self.responses.failed:
                self.span.set_status(trace_api.StatusCode.ERROR)
            elif not abandoned:
                self.span.set_status(trace_api.StatusCode.OK)
        finally:
            if self.span_cm is not None and not abandoned:
                self.span_cm.__exit__(
                    type(error) if error else None,
                    error,
                    error.__traceback__ if error else None,
                )
            else:
                self.span.end()


def _join_function(target, delta):
    for field in ("name", "arguments"):
        if isinstance(delta.get(field), str):
            target[field] = target.get(field, "") + delta[field]


class _StreamWrapper:
    def __init__(self, span, span_cm=None, config=None):
        self.span = span
        self._state = _StreamState(span, span_cm, config)
        self._closed = False
        # The callback owns only the state, not this wrapper or the SDK iterator.
        # It cannot keep an unread/partly read stream alive. No async I/O or
        # application-context detach is attempted during garbage collection.
        self._finalizer = weakref.finalize(self, self._state._finish, None, True)

    @property
    def _ended(self):
        return self._state._ended

    def _process(self, chunk):
        self._state._process(chunk)

    def _finish(self, error=None):
        try:
            self._state._finish(error)
        finally:
            self._finalizer.detach()


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


class StreamingIteratorWrapper(_StreamWrapper):
    def __init__(self, iterator: Iterator[Any], span: trace_api.Span, span_cm=None, config=None):
        super().__init__(span, span_cm, config)
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


class AsyncStreamingIteratorWrapper(_StreamWrapper):
    def __init__(
        self, async_iterator: AsyncIterator[Any], span: trace_api.Span, span_cm=None, config=None
    ):
        super().__init__(span, span_cm, config)
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


def _process_messages(messages, config=None):
    """Keep conversation structure, with images referenced rather than copied."""
    if not isinstance(messages, list):
        return {"input_images": None, "filtered_messages": messages, "eval_input": None}

    config = config or TraceConfig()
    input_images = []
    filtered_messages = []
    eval_input = []
    for message_index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            continue
        filtered_message = dict(message)
        content = message.get("content")
        if isinstance(content, str):
            eval_input.append(content)
        elif isinstance(content, list):
            filtered_content = []
            for content_index, item in enumerate(content):
                if not isinstance(item, Mapping):
                    continue
                if item.get("type") == "image_url":
                    image = item.get("image_url") or {}
                    if image_url := image.get("url"):
                        # The aggregate must use the same privacy/size policy as
                        # the canonical image.url attribute, not bypass it via JSON.
                        key = (
                            f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{message_index}."
                            f"{MessageAttributes.MESSAGE_CONTENTS}.{content_index}."
                            f"{MessageContentAttributes.MESSAGE_CONTENT_IMAGE}.{ImageAttributes.IMAGE_URL}"
                        )
                        image_index = len(input_images)
                        input_images.append(config.mask(key, image_url))
                        reference = (
                            "{{" + f"{SpanAttributes.INPUT_IMAGES}.{image_index}" + "}}"
                        )
                        filtered_content.append({"type": "text", "text": reference})
                        eval_input.append(reference)
                else:
                    filtered_content.append(dict(item))
                    if item.get("type") == "text" and (text := item.get("text")):
                        eval_input.append(str(text))
            filtered_message["content"] = filtered_content
        # Preserve content=None assistant tool calls and tool results as well.
        filtered_messages.append(filtered_message)

    return {
        "input_images": input_images or None,
        "filtered_messages": filtered_messages,
        "eval_input": "\n".join(eval_input),
    }
