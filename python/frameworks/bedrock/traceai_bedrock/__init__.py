import base64
import io
import json
import logging
from enum import Enum
from functools import wraps
from importlib import import_module
from inspect import signature
from typing import (
    IO,
    Any,
    Callable,
    Collection,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Tuple,
    TypeVar,
    cast,
)

from botocore.client import BaseClient
from botocore.response import StreamingBody
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from opentelemetry.trace import Status, StatusCode, Tracer
from opentelemetry.util.types import AttributeValue
from wrapt import wrap_function_wrapper

from fi_instrumentation import (
    FITracer,
    TraceConfig,
    get_attributes_from_context,
    safe_json_dumps,
)
from traceai_bedrock._rag_wrappers import (
    _retrieve_and_generate_wrapper,
    _retrieve_wrapper,
)
from traceai_bedrock._wrappers import (
    _InvokeAgentWithResponseStream,
    _InvokeModelWithResponseStream,
    _RetrieveAndGenerateStream,
)
from traceai_bedrock.package import _instruments
from traceai_bedrock.utils import _extract_invoke_model_attributes
from traceai_bedrock.utils.anthropic import (
    _attributes as anthropic_attributes,
)
from traceai_bedrock.version import __version__
from fi_instrumentation.fi_types import (
    ImageAttributes,
    MessageAttributes,
    MessageContentAttributes,
    FiSpanKindValues,
    SpanAttributes,
    FiLLMProviderValues,
)
from fi_instrumentation.instrumentation._protect_wrapper import GuardrailProtectWrapper
try:
    from fi.evals import Protect
except ImportError:
    logger.warning("ai-evaluation is not installed, please install it to trace protect")
    Protect = None
    pass

ClientCreator = TypeVar("ClientCreator", bound=Callable[..., BaseClient])

_MODULE = "botocore.client"
_BASE_MODULE = "botocore"
_MINIMUM_CONVERSE_BOTOCORE_VERSION = "1.34.116"

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class InstrumentedClient(BaseClient):  # type: ignore
    """
    Proxy class representing an instrumented boto client.
    """

    invoke_model: Callable[..., Any]
    _unwrapped_invoke_model: Callable[..., Any]

    converse: Callable[..., Any]
    _unwrapped_converse: Callable[..., Any]

    invoke_agent: Callable[..., Any]
    _unwrapped_invoke_agent: Callable[..., Any]

    retrieve: Callable[..., Any]
    _unwrapped_retrieve: Callable[..., Any]

    retrieve_and_generate: Callable[..., Any]
    _unwrapped_retrieve_and_generate: Callable[..., Any]

    retrieve_and_generate_stream: Callable[..., Any]
    _unwrapped_retrieve_and_generate_stream: Callable[..., Any]


class BufferedStreamingBody(StreamingBody):  # type: ignore
    _raw_stream: IO[bytes]

    def __init__(self, raw_stream: IO[bytes], content_length: int) -> None:
        super().__init__(raw_stream, content_length)
        self._buffer: Optional[io.IOBase] = None

    def read(self, amt: Optional[int] = None) -> bytes:
        if self._buffer is None:
            self._buffer = io.BytesIO(self._raw_stream.read())

        output: bytes = self._buffer.read(amt)
        return output

    def reset(self) -> None:
        # Reset the buffer to enable reading the stream again
        if self._buffer is not None:
            self._buffer.seek(0)


def _client_creation_wrapper(
    tracer: Tracer, module_version: str
) -> Callable[[ClientCreator], ClientCreator]:
    def _client_wrapper(
        wrapped: ClientCreator,
        instance: Optional[Any],
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
    ) -> BaseClient:
        """Instruments boto client creation."""
        client = wrapped(*args, **kwargs)
        if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return client

        call_signature = signature(wrapped)
        bound_arguments = call_signature.bind(*args, **kwargs)
        bound_arguments.apply_defaults()

        if bound_arguments.arguments.get("service_name") == "bedrock-agent-runtime":
            client = cast(InstrumentedClient, client)

            client._unwrapped_invoke_agent = client.invoke_agent
            client.invoke_agent = _InvokeAgentWithResponseStream(tracer)(client.invoke_agent)

            client._unwrapped_retrieve = client.retrieve
            client.retrieve = _retrieve_wrapper(tracer)(client)

            client._unwrapped_retrieve_and_generate = client.retrieve_and_generate
            client.retrieve_and_generate = _retrieve_and_generate_wrapper(tracer)(client)

            client._unwrapped_retrieve_and_generate_stream = client.retrieve_and_generate_stream
            client.retrieve_and_generate_stream = _RetrieveAndGenerateStream(tracer)(
                client.retrieve_and_generate_stream
            )

        if bound_arguments.arguments.get("service_name") == "bedrock-runtime":
            client = cast(InstrumentedClient, client)

            client._unwrapped_invoke_model = client.invoke_model
            client.invoke_model = _model_invocation_wrapper(tracer)(client)
            client.invoke_model_with_response_stream = _InvokeModelWithResponseStream(tracer)(
                client.invoke_model_with_response_stream
            )

            if module_version >= _MINIMUM_CONVERSE_BOTOCORE_VERSION:
                client._unwrapped_converse = client.converse
                client.converse = _model_converse_wrapper(tracer)(client)
        return client

    return _client_wrapper  # type: ignore


def _model_invocation_wrapper(tracer: Tracer) -> Callable[[InstrumentedClient], Callable[..., Any]]:
    def _invocation_wrapper(wrapped_client: InstrumentedClient) -> Callable[..., Any]:
        """Instruments a bedrock client's `invoke_model` or `converse` method."""

        @wraps(wrapped_client.invoke_model)
        def instrumented_response(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
                return wrapped_client._unwrapped_invoke_model(*args, **kwargs)  # type: ignore

            with tracer.start_as_current_span("bedrock.invoke_model") as span:
                request_body = json.loads(kwargs["body"])
                model_id = str(kwargs.get("modelId"))
                # Determine if this is a Claude Messages API model
                is_claude_message_api = _extract_invoke_model_attributes.is_claude_message_api(
                    model_id
                )

                # Set input attributes based on model type
                if is_claude_message_api:
                    anthropic_attributes.set_input_attributes(span, request_body, model_id)
                else:
                    _extract_invoke_model_attributes.set_input_attributes(span, request_body)

                # Execute the model invocation with proper error handling
                try:
                    response = wrapped_client._unwrapped_invoke_model(*args, **kwargs)
                except Exception as e:
                    span.set_status(Status(StatusCode.ERROR))
                    span.end()
                    raise e

                # Process the streaming response body
                response["body"] = BufferedStreamingBody(
                    response["body"]._raw_stream, response["body"]._content_length
                )
                response_body = json.loads(response.get("body").read())
                response["body"].reset()

                # Set response attributes based on model type
                if is_claude_message_api:
                    anthropic_attributes.set_response_attributes(span, response_body)
                else:
                    _extract_invoke_model_attributes.set_response_attributes(
                        span, kwargs, response_body, response
                    )
                span.set_attributes(dict(get_attributes_from_context()))
                span.set_status(Status(StatusCode.OK))
                return response  # type: ignore

        return instrumented_response

    return _invocation_wrapper


def _model_converse_wrapper(tracer: Tracer) -> Callable[[InstrumentedClient], Callable[..., Any]]:
    def _converse_wrapper(wrapped_client: InstrumentedClient) -> Callable[..., Any]:
        """Instruments a bedrock client's `converse` method."""

        @wraps(wrapped_client.converse)
        def instrumented_response(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
                return wrapped_client._unwrapped_converse(*args, **kwargs)  # type: ignore

            with tracer.start_as_current_span("bedrock.converse") as span:
                span.set_attribute(
                    SpanAttributes.FI_SPAN_KIND,
                    FiSpanKindValues.LLM.value,
                )
                
                _set_span_attribute(span, SpanAttributes.LLM_PROVIDER, FiLLMProviderValues.AWS.value)

                if model_id := kwargs.get("modelId"):
                    _set_span_attribute(span, SpanAttributes.LLM_MODEL_NAME, model_id)

                if inference_config := kwargs.get("inferenceConfig"):
                    invocation_parameters = safe_json_dumps(inference_config)
                    _set_span_attribute(
                        span, SpanAttributes.LLM_INVOCATION_PARAMETERS, invocation_parameters
                    )

                aggregated_messages = []
                if system_prompts := kwargs.get("system"):
                    aggregated_messages.append(
                        {
                            "role": "system",
                            "content": [
                                {
                                    "text": " ".join(
                                        prompt.get("text", "") for prompt in system_prompts
                                    )
                                }
                            ],
                        }
                    )

                aggregated_messages.extend(kwargs.get("messages", []))
                for idx, msg in enumerate(aggregated_messages):
                    if not isinstance(msg, dict):
                        # Only dictionaries supported for now
                        continue
                    for key, value in _get_attributes_from_message_param(msg):
                        _set_span_attribute(
                            span,
                            f"{SpanAttributes.LLM_INPUT_MESSAGES}.{idx}.{key}",
                            value,
                        )
                last_message = aggregated_messages[-1]
                if isinstance(last_message, dict) and (
                    request_msg_content := last_message.get("content")
                ):
                    request_msg_prompt = "\n".join(
                        content_input.get("text", "")  # type: ignore
                        for content_input in request_msg_content
                    ).strip("\n")
                    _set_span_attribute(span, SpanAttributes.INPUT_VALUE, request_msg_prompt)

                try:
                    response = wrapped_client._unwrapped_converse(*args, **kwargs)
                except Exception as e:
                    span.record_exception(e)
                    raise e

                if (
                    (response_message := response.get("output", {}).get("message"))
                    and (response_role := response_message.get("role"))
                    and (response_content := response_message.get("content", []))
                ):
                    # Currently only supports text-based data
                    response_text = "\n".join(
                        content_input.get("text", "") for content_input in response_content
                    )
                    _set_span_attribute(span, SpanAttributes.OUTPUT_VALUE, response_text)

                    span_prefix = f"{SpanAttributes.LLM_OUTPUT_MESSAGES}.0"
                    _set_span_attribute(span, f"{span_prefix}.message.role", response_role)
                    _set_span_attribute(span, f"{span_prefix}.message.content", response_text)

                if usage := response.get("usage"):
                    if input_token_count := usage.get("inputTokens"):
                        _set_span_attribute(
                            span, SpanAttributes.LLM_TOKEN_COUNT_PROMPT, input_token_count
                        )
                    if response_token_count := usage.get("outputTokens"):
                        _set_span_attribute(
                            span,
                            SpanAttributes.LLM_TOKEN_COUNT_COMPLETION,
                            response_token_count,
                        )
                    if total_token_count := usage.get("totalTokens"):
                        _set_span_attribute(
                            span, SpanAttributes.LLM_TOKEN_COUNT_TOTAL, total_token_count
                        )

                span.set_attributes(dict(get_attributes_from_context()))
                span.set_status(Status(StatusCode.OK))
                return response  # type: ignore

        return instrumented_response

    return _converse_wrapper


class BedrockInstrumentor(BaseInstrumentor):  # type: ignore
    __slots__ = (
        "_tracer",
        "_original_client_creator",
    )

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

        boto = import_module(_MODULE)
        botocore = import_module(_BASE_MODULE)
        self._original_client_creator = boto.ClientCreator.create_client

        wrap_function_wrapper(
            module=_MODULE,
            name="ClientCreator.create_client",
            wrapper=_client_creation_wrapper(
                tracer=self._tracer, module_version=botocore.__version__
            ),
        )

        self._original_protect = Protect.protect
        wrap_function_wrapper(
            module="fi.evals",
            name="Protect.protect",
            wrapper=GuardrailProtectWrapper(tracer=self._tracer),
        )

    def _uninstrument(self, **kwargs: Any) -> None:
        boto = import_module(_MODULE)
        boto.ClientCreator.create_client = self._original_client_creator
        self._original_client_creator = None


def _set_span_attribute(span: trace_api.Span, name: str, value: AttributeValue) -> None:
    if value is not None and value != "":
        span.set_attribute(name, value)


def _get_attributes_from_message_param(
    message: Dict[str, Any],
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
        elif isinstance(content, List):
            # See https://github.com/openai/openai-python/blob/f1c7d714914e3321ca2e72839fe2d132a8646e7f/src/openai/types/chat/chat_completion_user_message_param.py#L14  # noqa: E501
            try:
                value = safe_json_dumps(content)
            except Exception:
                logger.exception("Failed to serialize message content")
            yield MessageAttributes.MESSAGE_CONTENT, value


def _get_attributes_from_message_content(
    content: Dict[str, Any],
) -> Iterator[Tuple[str, AttributeValue]]:
    content = dict(content)
    if text := content.get("text"):
        yield f"{MessageContentAttributes.MESSAGE_CONTENT_TYPE}", "text"
        yield f"{MessageContentAttributes.MESSAGE_CONTENT_TEXT}", text
    if image := content.get("image"):
        yield f"{MessageContentAttributes.MESSAGE_CONTENT_TYPE}", "image"
        for key, value in _get_attributes_from_image(image):
            yield f"{key}", value


def _get_attributes_from_image(
    image: Dict[str, Any],
) -> Iterator[Tuple[str, AttributeValue]]:
    if (source := image.get("source")) and (img_bytes := source.get("bytes")):
        base64_img = base64.b64encode(img_bytes).decode("utf-8")
        yield (
            f"{MessageContentAttributes.MESSAGE_CONTENT_IMAGE}",
            f"data:image/jpeg;base64,{base64_img}",
        )


T = TypeVar("T", bound=type)


def is_iterable_of(lst: Iterable[object], tp: T) -> bool:
    return isinstance(lst, Iterable) and all(isinstance(x, tp) for x in lst)
