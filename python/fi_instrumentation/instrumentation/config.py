import os
from contextvars import ContextVar
from dataclasses import dataclass, field, fields
from types import TracebackType
from typing import Any, Callable, Optional, Type, Union, get_args

from fi_instrumentation.fi_types import (
    EmbeddingAttributes,
    ImageAttributes,
    MessageAttributes,
    MessageContentAttributes,
    SpanAttributes,
)
from opentelemetry.context import (
    _SUPPRESS_INSTRUMENTATION_KEY,
    attach,
    detach,
    set_value,
)
from opentelemetry.util.types import AttributeValue

from ._errors import is_error_detail_key
from .logging import logger
from .pii_redaction import key_carries_free_text, redact_pii_in_value


class suppress_tracing:
    """
    Context manager to pause OpenTelemetry instrumentation.

    Examples:
        with suppress_tracing():
            # No tracing will occur within this block
            ...
    """

    def __init__(self) -> None:
        self._tokens: ContextVar[tuple] = ContextVar("fi_suppression_tokens", default=())

    def __enter__(self) -> "suppress_tracing":
        token = attach(set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
        self._tokens.set((*self._tokens.get(), token))
        return self

    async def __aenter__(self) -> "suppress_tracing":
        return self.__enter__()

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        tokens = self._tokens.get()
        self._tokens.set(tokens[:-1])
        detach(tokens[-1])

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self.__exit__(exc_type, exc_value, traceback)


FI_HIDE_LLM_INVOCATION_PARAMETERS = "FI_HIDE_LLM_INVOCATION_PARAMETERS"
FI_HIDE_INPUTS = "FI_HIDE_INPUTS"
# Hides input value & messages
FI_HIDE_OUTPUTS = "FI_HIDE_OUTPUTS"
# Hides output value & messages
FI_HIDE_INPUT_MESSAGES = "FI_HIDE_INPUT_MESSAGES"
# Hides all input messages
FI_HIDE_OUTPUT_MESSAGES = "FI_HIDE_OUTPUT_MESSAGES"
# Hides all output messages
FI_HIDE_INPUT_IMAGES = "FI_HIDE_INPUT_IMAGES"
# Hides images from input messages
FI_HIDE_INPUT_TEXT = "FI_HIDE_INPUT_TEXT"
# Hides text from input messages
FI_HIDE_OUTPUT_TEXT = "FI_HIDE_OUTPUT_TEXT"
# Hides text from output messages
FI_HIDE_EMBEDDING_VECTORS = "FI_HIDE_EMBEDDING_VECTORS"
# Hides embedding vectors
FI_BASE64_IMAGE_MAX_LENGTH = "FI_BASE64_IMAGE_MAX_LENGTH"
# Limits characters of a base64 encoding of an image
FI_PII_REDACTION = "FI_PII_REDACTION"
# Enables regex-based PII redaction on attribute values
FI_HIDE_ERROR_DETAILS = "FI_HIDE_ERROR_DETAILS"
# Hides error messages, stacktraces and the ERROR status description.
# Unset: on when any input/output hide flag is on.
REDACTED_VALUE = "__REDACTED__"

# Besides input.value: the raw request body and the prompt template's values.
# ``gen_ai.prompt.template.variables`` is what the context helper writes.
_HIDDEN_WITH_INPUTS = frozenset(
    {
        SpanAttributes.RAW_INPUT,
        SpanAttributes.LLM_PROMPT_TEMPLATE_VARIABLES,
        SpanAttributes.GEN_AI_PROMPT_TEMPLATE_VARIABLES,
    }
)
# Known serialized and flattened content fields, including their descendants.
# Keep identifiers such as model names and token counts available.
_INPUT_CONTENT_PREFIXES = (
    SpanAttributes.GEN_AI_SYSTEM_INSTRUCTIONS,
    SpanAttributes.GEN_AI_REQUEST_PARAMETERS,
    SpanAttributes.GEN_AI_REQUEST_STOP_SEQUENCES,
    SpanAttributes.GEN_AI_AGENT_DESCRIPTION,
    SpanAttributes.GEN_AI_IMAGE_PROMPT,
    SpanAttributes.GEN_AI_IMAGE_NEGATIVE_PROMPT,
    SpanAttributes.INPUT_IMAGES,
    SpanAttributes.GEN_AI_PROMPTS,
    SpanAttributes.GEN_AI_TOOL_DEFINITIONS,
    SpanAttributes.GEN_AI_TOOL_DESCRIPTION,
    SpanAttributes.GEN_AI_TOOL_CALL_ARGUMENTS,
    SpanAttributes.TOOL_PARAMETERS,
    "tool_call.function.arguments",
    "message.function_call_arguments_json",
    SpanAttributes.GEN_AI_COMPUTER_USE_TEXT,
    SpanAttributes.GEN_AI_COMPUTER_USE_ELEMENT_SELECTOR,
    "tool.json_schema",
    SpanAttributes.GEN_AI_RETRIEVAL_QUERY,
    SpanAttributes.GEN_AI_RERANKER_QUERY,
    SpanAttributes.GEN_AI_RERANKER_INPUT_DOCUMENTS,
    "reranker.query",
    "reranker.input_documents",
)
_OUTPUT_CONTENT_PREFIXES = (
    SpanAttributes.GEN_AI_TOOL_CALL_RESULT,
    SpanAttributes.GEN_AI_IMAGE_REVISED_PROMPT,
    SpanAttributes.GEN_AI_IMAGE_OUTPUT_URLS,
    SpanAttributes.GEN_AI_GUARDRAIL_MODIFIED_OUTPUT,
    SpanAttributes.GEN_AI_EVALUATION_EXPLANATION,
    SpanAttributes.GEN_AI_COMPUTER_USE_RESULT,
    SpanAttributes.RETRIEVAL_DOCUMENTS,
    SpanAttributes.GEN_AI_RETRIEVAL_DOCUMENTS,
    SpanAttributes.GEN_AI_RERANKER_OUTPUT_DOCUMENTS,
    "reranker.output_documents",
)

# These public fields do not encode a direction and may contain either side.
_MIXED_CONTENT_PREFIXES = (
    SpanAttributes.GEN_AI_VOICE_TRANSCRIPT,
    SpanAttributes.GEN_AI_VOICE_RECORDING_URL,
    SpanAttributes.GEN_AI_VOICE_RECORDING_STEREO_URL,
    SpanAttributes.GEN_AI_VOICE_RECORDING_CUSTOMER_URL,
    SpanAttributes.GEN_AI_VOICE_RECORDING_ASSISTANT_URL,
    SpanAttributes.GEN_AI_AUDIO_URL,
    SpanAttributes.GEN_AI_AUDIO_TRANSCRIPT,
    SpanAttributes.GEN_AI_COMPUTER_USE_SCREENSHOT,
    SpanAttributes.GEN_AI_COMPUTER_USE_CURRENT_URL,
)


def _has_content_prefix(key: str, prefixes: tuple) -> bool:
    return any(key == prefix or key.startswith(prefix + ".") for prefix in prefixes)


DEFAULT_HIDE_LLM_INVOCATION_PARAMETERS = False
DEFAULT_HIDE_INPUTS = False
DEFAULT_HIDE_OUTPUTS = False

DEFAULT_HIDE_INPUT_MESSAGES = False
DEFAULT_HIDE_OUTPUT_MESSAGES = False

DEFAULT_HIDE_INPUT_IMAGES = False
DEFAULT_HIDE_INPUT_TEXT = False
DEFAULT_HIDE_OUTPUT_TEXT = False

DEFAULT_HIDE_EMBEDDING_VECTORS = False
DEFAULT_BASE64_IMAGE_MAX_LENGTH = 32_000
DEFAULT_PII_REDACTION = False
DEFAULT_HIDE_ERROR_DETAILS = None
# None means "derive from the content hide flags", see _CONTENT_HIDE_FLAGS.

# Hiding any of these also hides error details by default, because error text
# (provider error bodies, exception messages) can quote the same content.
_CONTENT_HIDE_FLAGS = (
    "hide_inputs",
    "hide_outputs",
    "hide_input_messages",
    "hide_output_messages",
    "hide_input_text",
    "hide_output_text",
)


@dataclass(frozen=True)
class TraceConfig:
    """
    TraceConfig helps you modify the observability level of your tracing.
    For instance, you may want to keep sensitive information from being logged
    for security reasons, or you may want to limit the size of the base64
    encoded images to reduce payloads.

    For those attributes not passed, this object tries to read from designated
    environment variables and, if not found, has default values that maximize
    observability.
    """

    hide_llm_invocation_parameters: Optional[bool] = field(
        default=None,
        metadata={
            "env_var": FI_HIDE_LLM_INVOCATION_PARAMETERS,
            "default_value": DEFAULT_HIDE_LLM_INVOCATION_PARAMETERS,
        },
    )
    hide_inputs: Optional[bool] = field(
        default=None,
        metadata={
            "env_var": FI_HIDE_INPUTS,
            "default_value": DEFAULT_HIDE_INPUTS,
        },
    )
    """Hides input value & messages"""
    hide_outputs: Optional[bool] = field(
        default=None,
        metadata={
            "env_var": FI_HIDE_OUTPUTS,
            "default_value": DEFAULT_HIDE_OUTPUTS,
        },
    )
    """Hides output value & messages"""
    hide_input_messages: Optional[bool] = field(
        default=None,
        metadata={
            "env_var": FI_HIDE_INPUT_MESSAGES,
            "default_value": DEFAULT_HIDE_INPUT_MESSAGES,
        },
    )
    """Hides all input messages"""
    hide_output_messages: Optional[bool] = field(
        default=None,
        metadata={
            "env_var": FI_HIDE_OUTPUT_MESSAGES,
            "default_value": DEFAULT_HIDE_OUTPUT_MESSAGES,
        },
    )
    """Hides all output messages"""
    hide_input_images: Optional[bool] = field(
        default=None,
        metadata={
            "env_var": FI_HIDE_INPUT_IMAGES,
            "default_value": DEFAULT_HIDE_INPUT_IMAGES,
        },
    )
    """Hides images from input messages"""
    hide_input_text: Optional[bool] = field(
        default=None,
        metadata={
            "env_var": FI_HIDE_INPUT_TEXT,
            "default_value": DEFAULT_HIDE_INPUT_TEXT,
        },
    )
    """Hides text from input messages"""
    hide_output_text: Optional[bool] = field(
        default=None,
        metadata={
            "env_var": FI_HIDE_OUTPUT_TEXT,
            "default_value": DEFAULT_HIDE_OUTPUT_TEXT,
        },
    )
    """Hides text from output messages"""
    hide_embedding_vectors: Optional[bool] = field(
        default=None,
        metadata={
            "env_var": FI_HIDE_EMBEDDING_VECTORS,
            "default_value": DEFAULT_HIDE_EMBEDDING_VECTORS,
        },
    )
    """Hides embedding vectors"""
    base64_image_max_length: Optional[int] = field(
        default=None,
        metadata={
            "env_var": FI_BASE64_IMAGE_MAX_LENGTH,
            "default_value": DEFAULT_BASE64_IMAGE_MAX_LENGTH,
        },
    )
    """Limits characters of a base64 encoding of an image"""
    pii_redaction: Optional[bool] = field(
        default=None,
        metadata={
            "env_var": FI_PII_REDACTION,
            "default_value": DEFAULT_PII_REDACTION,
        },
    )
    """Enables regex-based PII redaction on attribute values"""
    _hide_error_details: Optional[bool] = field(
        default=None,
        metadata={
            "env_var": FI_HIDE_ERROR_DETAILS,
            "default_value": DEFAULT_HIDE_ERROR_DETAILS,
        },
    )
    """The ``hide_error_details`` setting as given: the argument, else
    FI_HIDE_ERROR_DETAILS, else None ("derive from the content hide flags"). It
    stays unresolved so that ``dataclasses.replace()`` derives it again from
    the new flags. Read the ``hide_error_details`` property instead."""

    def __init__(
        self,
        hide_llm_invocation_parameters: Optional[bool] = None,
        hide_inputs: Optional[bool] = None,
        hide_outputs: Optional[bool] = None,
        hide_input_messages: Optional[bool] = None,
        hide_output_messages: Optional[bool] = None,
        hide_input_images: Optional[bool] = None,
        hide_input_text: Optional[bool] = None,
        hide_output_text: Optional[bool] = None,
        hide_embedding_vectors: Optional[bool] = None,
        base64_image_max_length: Optional[int] = None,
        pii_redaction: Optional[bool] = None,
        hide_error_details: Optional[bool] = None,
        *,
        _hide_error_details: Optional[bool] = None,
    ) -> None:
        # Written by hand because the public argument ``hide_error_details``
        # fills the private field ``_hide_error_details``.
        # ``dataclasses.replace()`` passes that field by its own name; an
        # explicit ``hide_error_details`` wins over it.
        values = dict(locals())
        if hide_error_details is not None:
            values["_hide_error_details"] = hide_error_details
        for f in fields(self):
            object.__setattr__(self, f.name, values[f.name])
        self.__post_init__()

    @property
    def hide_error_details(self) -> bool:
        """Hides error details: the ERROR status description, exception.message,
        exception.stacktrace and error.message. The status stays ERROR and
        error.type / exception.type are kept. When neither the argument nor
        FI_HIDE_ERROR_DETAILS is set, it is on if any of hide_inputs,
        hide_outputs, hide_input_messages, hide_output_messages, hide_input_text
        or hide_output_text is on."""
        if self._hide_error_details is not None:
            return self._hide_error_details
        return any(getattr(self, name) for name in _CONTENT_HIDE_FLAGS)

    def __post_init__(self) -> None:
        for f in fields(self):
            expected_type = get_args(f.type)[0]
            # Optional is Union[T,NoneType]. get_args()returns (T, NoneType).
            # We collect the first type
            self._parse_value(
                f.name,
                expected_type,
                f.metadata["env_var"],
                f.metadata["default_value"],
            )

    def mask(
        self,
        key: str,
        value: Union[AttributeValue, Callable[[], AttributeValue]],
    ) -> Optional[AttributeValue]:
        if self.hide_error_details and is_error_detail_key(key):
            return None
        elif (
            self.hide_llm_invocation_parameters
            and key == SpanAttributes.GEN_AI_REQUEST_PARAMETERS
        ):
            return None
        elif self.hide_inputs and _has_content_prefix(key, (SpanAttributes.INPUT_VALUE,)):
            value = REDACTED_VALUE
        elif self.hide_inputs and key == SpanAttributes.INPUT_MIME_TYPE:
            return None
        elif self.hide_inputs and _has_content_prefix(key, tuple(_HIDDEN_WITH_INPUTS)):
            # The raw request body (messages included) and the values filled
            # into a prompt template are inputs too.
            value = REDACTED_VALUE
        elif self.hide_outputs and _has_content_prefix(key, (SpanAttributes.OUTPUT_VALUE,)):
            value = REDACTED_VALUE
        elif self.hide_outputs and key == SpanAttributes.OUTPUT_MIME_TYPE:
            return None
        elif self.hide_outputs and _has_content_prefix(key, (SpanAttributes.RAW_OUTPUT,)):
            value = REDACTED_VALUE
        # The aggregate tool call can contain both arguments and results; hide
        # it if either direction is private. Specific child fields remain
        # directional, and identifiers like gen_ai.tool.name stay visible.
        elif key == SpanAttributes.GEN_AI_TOOL_CALL and (self.hide_inputs or self.hide_outputs):
            value = REDACTED_VALUE
        elif (self.hide_inputs or self.hide_outputs) and _has_content_prefix(key, _MIXED_CONTENT_PREFIXES):
            value = REDACTED_VALUE
        elif self.hide_inputs and _has_content_prefix(key, _INPUT_CONTENT_PREFIXES):
            value = REDACTED_VALUE
        elif self.hide_outputs and _has_content_prefix(key, _OUTPUT_CONTENT_PREFIXES):
            value = REDACTED_VALUE
        elif (
            self.hide_inputs or self.hide_input_messages
        ) and SpanAttributes.GEN_AI_INPUT_MESSAGES in key:
            return None
        elif (
            self.hide_outputs or self.hide_output_messages
        ) and SpanAttributes.GEN_AI_OUTPUT_MESSAGES in key:
            return None
        elif (
            self.hide_input_text
            and SpanAttributes.GEN_AI_INPUT_MESSAGES in key
            and MessageAttributes.MESSAGE_CONTENT in key
            and MessageAttributes.MESSAGE_CONTENTS not in key
        ):
            value = REDACTED_VALUE
        elif (
            self.hide_output_text
            and SpanAttributes.GEN_AI_OUTPUT_MESSAGES in key
            and MessageAttributes.MESSAGE_CONTENT in key
            and MessageAttributes.MESSAGE_CONTENTS not in key
        ):
            value = REDACTED_VALUE
        elif (
            self.hide_input_text
            and SpanAttributes.GEN_AI_INPUT_MESSAGES in key
            and MessageContentAttributes.MESSAGE_CONTENT_TEXT in key
        ):
            value = REDACTED_VALUE
        elif (
            self.hide_output_text
            and SpanAttributes.GEN_AI_OUTPUT_MESSAGES in key
            and MessageContentAttributes.MESSAGE_CONTENT_TEXT in key
        ):
            value = REDACTED_VALUE
        elif self.hide_input_images and _has_content_prefix(key, (SpanAttributes.INPUT_IMAGES,)):
            return None
        elif (
            self.hide_input_images
            and SpanAttributes.GEN_AI_INPUT_MESSAGES in key
            and MessageContentAttributes.MESSAGE_CONTENT_IMAGE in key
        ):
            return None
        elif (
            is_base64_url(value)  # type:ignore
            and len(value) > self.base64_image_max_length  # type:ignore
            and SpanAttributes.GEN_AI_INPUT_MESSAGES in key
            and MessageContentAttributes.MESSAGE_CONTENT_IMAGE in key
            and key.endswith(ImageAttributes.IMAGE_URL)
        ):
            value = REDACTED_VALUE
        elif self.hide_embedding_vectors and (
            _has_content_prefix(key, (SpanAttributes.GEN_AI_EMBEDDINGS_VECTORS,))
            or (
                SpanAttributes.EMBEDDING_EMBEDDINGS in key
                and EmbeddingAttributes.EMBEDDING_VECTOR in key
            )
        ):
            return None
        resolved = value() if callable(value) else value
        if (
            self.pii_redaction
            and resolved is not None
            # Only free-text attributes get scanned — identifiers like
            # session.id/user.id/metadata are structured values where a
            # regex hit corrupts correlation, not a secret (#195).
            and key_carries_free_text(key)
        ):
            resolved = redact_pii_in_value(resolved)
        return resolved

    def _parse_value(
        self,
        field_name: str,
        expected_type: Any,
        env_var: str,
        default_value: Any,
    ) -> None:
        type_name = expected_type.__name__
        init_value = getattr(self, field_name, None)
        if init_value is None:
            env_value = os.getenv(env_var)
            if env_value is None:
                object.__setattr__(self, field_name, default_value)
            else:
                try:
                    env_value = self._cast_value(env_value, expected_type)
                    object.__setattr__(self, field_name, env_value)
                except Exception:
                    fallback = (
                        default_value
                        if default_value is not None
                        else "derived from the other hide flags"
                    )
                    logger.warning(
                        f"Could not parse '{env_value}' to {type_name} "
                        f"for the environment variable '{env_var}'. "
                        f"Using default value instead: {fallback}."
                    )
                    object.__setattr__(self, field_name, default_value)
        else:
            if not isinstance(init_value, expected_type):
                raise TypeError(
                    f"The field {field_name.lstrip('_')} must be of type '{type_name}' "
                    f"but '{type(init_value).__name__}' was found."
                )

    def _cast_value(
        self,
        value: Any,
        cast_to: Any,
    ) -> Any:
        if cast_to is bool:
            if isinstance(value, str) and value.lower() == "true":
                return True
            if isinstance(value, str) and value.lower() == "false":
                return False
            raise
        else:
            return cast_to(value)


def is_base64_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    return url.startswith("data:image/") and "base64" in url
