"""Capture policy shared by native callbacks and the pre-callback fallback."""

import json
from collections.abc import Mapping
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


def sanitize(value: Any) -> Any:
    """Copy payloads; credential configuration never depends on FI_HIDE_* flags."""
    if isinstance(value, Mapping):
        return {
            key: REDACTED
            if isinstance(key, str)
            and (
                "".join(char for char in key.lower() if char.isalnum())
                in _CREDENTIAL_KEYS
            )
            else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    return value


def payload(value: Any, encoder: type[json.JSONEncoder]) -> str:
    # Sanitize again after encoding: DSPy Predictions become mappings in the
    # encoder, and may themselves contain credential-named fields.
    return safe_json_dumps(
        sanitize(json.loads(safe_json_dumps(sanitize(value), cls=encoder)))
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
