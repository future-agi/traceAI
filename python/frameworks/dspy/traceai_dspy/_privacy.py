"""Capture policy shared by native callbacks and the pre-callback fallback."""

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from typing import Any

from fi_instrumentation import TraceConfig, safe_json_dumps
from fi_instrumentation.fi_types import SpanAttributes

REDACTED = "__REDACTED__"
GENERATION_PARAMETERS = frozenset(
    {
        "temperature",
        "max_tokens",
        "max_completion_tokens",
        "top_p",
        "top_k",
        "n",
        "seed",
        "stop",
        "frequency_penalty",
        "presence_penalty",
    }
)
_CREDENTIAL_KEYS = frozenset(
    {
        "apikey",
        "apitoken",
        "authorization",
        "proxyauthorization",
        "password",
        "secret",
        "secretkey",
        "clientsecret",
        "token",
        "accesstoken",
        "authtoken",
        "refreshtoken",
        "credentials",
        "headers",
        "extraheaders",
        "defaultheaders",
        "cookie",
        "cookies",
        "awsaccesskeyid",
        "awssecretaccesskey",
        "awssessiontoken",
    }
)
# Provider-qualified variants retain the same credential meaning. Match the
# normalized compound name (azure_api_key / azureApiKey), but require a word
# boundary for bare key/token/secret so monkey and token_count stay visible.
_CREDENTIAL_SUFFIXES = (
    "apikey",
    "apitoken",
    "accesstoken",
    "authtoken",
    "refreshtoken",
    "clientsecret",
    "secretkey",
    "authorization",
    "password",
    "credentials",
    "extraheaders",
    "defaultheaders",
    "headers",
    "cookie",
    "cookies",
)


def _is_credential_key(key: str) -> bool:
    normalized = "".join(char for char in key.lower() if char.isalnum())
    if normalized in _CREDENTIAL_KEYS or normalized.endswith(_CREDENTIAL_SUFFIXES):
        return True
    parts = key.replace("-", "_").replace(".", "_").split("_")
    if len(parts) > 1 and parts[-1].lower() in {"key", "token", "secret"}:
        return True
    return key.endswith(("Key", "Token", "Secret"))


def sanitize(value: Any) -> Any:
    """Copy payloads; credential configuration never depends on FI_HIDE_* flags."""
    if isinstance(value, Mapping):
        return {
            key: REDACTED if isinstance(key, str) and _is_credential_key(key)
            else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    return value


def payload(value: Any, encoder: type[json.JSONEncoder]) -> str:
    # The DSPy encoder exposes Example/Prediction and namedtuple fields, but
    # falls back to repr for other objects. Reprs can contain credentials with
    # no field boundaries left to sanitize. Preserve the structured cases,
    # expand dataclasses, and omit everything else instead of exporting repr.
    class CaptureEncoder(encoder):
        def default(self, record: Any) -> Any:
            if is_dataclass(record) and not isinstance(record, type):
                try:
                    return {field.name: getattr(record, field.name) for field in fields(record)}
                except Exception:
                    return "[object omitted]"
            from dspy.primitives.example import Example

            if isinstance(record, Example) or hasattr(record, "_asdict"):
                return super().default(record)
            return "[object omitted]"

    return safe_json_dumps(
        sanitize(json.loads(json.dumps(sanitize(value), cls=CaptureEncoder)))
    )


def generation_parameters(parameters: Mapping[str, Any]) -> dict[str, Any]:
    return sanitize(
        {
            key: value
            for key, value in parameters.items()
            if key in GENERATION_PARAMETERS
        }
    )


def lm_inputs(inputs: Mapping[str, Any]) -> dict[str, Any]:
    result = sanitize(inputs)
    if isinstance(result.get("kwargs"), Mapping):
        result["kwargs"] = generation_parameters(result["kwargs"])
    return result


def capture_attributes(
    attributes: dict[str, Any], config: TraceConfig
) -> dict[str, Any]:
    """Do not let untyped payload copies bypass typed semantic privacy rules.

    FITracer masks the semantic attributes. An arbitrary JSON payload has no
    trustworthy schema for distinguishing messages/text/images/parameters, so
    suppress that copy when any of its selective policies is enabled.
    """
    hide_input = (
        config.hide_input_messages
        or config.hide_input_text
        or config.hide_input_images
        or config.hide_llm_invocation_parameters
    )
    hide_output = config.hide_output_messages or config.hide_output_text
    for key in list(attributes):
        if hide_input and key in (
            SpanAttributes.INPUT_VALUE,
            SpanAttributes.GEN_AI_PROMPTS,
        ):
            attributes[key] = REDACTED
        elif hide_input and key == SpanAttributes.INPUT_MIME_TYPE:
            del attributes[key]
        elif hide_output and key == SpanAttributes.OUTPUT_VALUE:
            attributes[key] = REDACTED
        elif hide_output and key == SpanAttributes.OUTPUT_MIME_TYPE:
            del attributes[key]
        elif key.startswith("embedding.embeddings.") and key.endswith(
            ".embedding.text"
        ):
            if config.hide_inputs or hide_input:
                attributes[key] = REDACTED
        elif key.startswith("embedding.embeddings.") and key.endswith(
            ".embedding.vector"
        ):
            if config.hide_outputs or config.hide_embedding_vectors:
                del attributes[key]
        elif key == SpanAttributes.EMBEDDING_EMBEDDINGS and (
            config.hide_outputs or config.hide_embedding_vectors
        ):
            del attributes[key]
    return attributes
