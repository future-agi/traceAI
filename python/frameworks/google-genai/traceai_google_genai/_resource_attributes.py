"""Attributes for the SDK's non-generation resources.

Only application payload fields are inspected. Never serialize a whole SDK
request/response: those also carry transport headers and credentials.
"""

import json
from collections.abc import Mapping
from typing import Any

from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import SpanAttributes as S

_MAX_CONTENT = 65_536
_USAGE = {
    "total_input_tokens": S.GEN_AI_USAGE_INPUT_TOKENS,
    "total_output_tokens": S.GEN_AI_USAGE_OUTPUT_TOKENS,
    "total_tokens": S.GEN_AI_USAGE_TOTAL_TOKENS,
    "total_cached_tokens": S.GEN_AI_USAGE_CACHE_READ_TOKENS,
    "total_thought_tokens": S.GEN_AI_USAGE_OUTPUT_TOKENS_REASONING,
}


def field(value: Any, name: str, default: Any = None) -> Any:
    return (
        value.get(name, default)
        if isinstance(value, Mapping)
        else getattr(value, name, default)
    )


def _json(value: Any) -> str:
    def encode(obj: Any) -> Any:
        if hasattr(obj, "model_dump"):
            return obj.model_dump(mode="json", exclude_none=True)
        # Do not consume arbitrary iterables or stringify opaque objects.
        return None

    return json.dumps(value, default=encode, ensure_ascii=False)


def _content(attrs: dict, key: str, value: Any) -> None:
    if value is None:
        return
    encoded = _json(value)
    if len(encoded.encode("utf-8")) <= _MAX_CONTENT:
        attrs[key] = encoded
    else:
        attrs["google.genai.content_truncated"] = True


def _hide_input(config: TraceConfig) -> bool:
    return bool(
        config.hide_inputs
        or config.hide_input_text
        or config.hide_input_messages
        or config.hide_input_images
    )


def _hide_output(config: TraceConfig) -> bool:
    return bool(
        config.hide_outputs or config.hide_output_text or config.hide_output_messages
    )


def request_attributes(
    resource: str, method: str, kwargs: Mapping, config: TraceConfig
) -> dict:
    embedding = resource == "models"
    attrs = {
        S.GEN_AI_PROVIDER_NAME: "google",
        S.GEN_AI_SPAN_KIND: "EMBEDDING"
        if embedding
        else "LLM"
        if resource == "interactions"
        else "CHAIN",
        S.GEN_AI_OPERATION_NAME: "embeddings" if embedding else f"{resource}.{method}",
    }
    # New Interactions accepts a typed request as well as keyword body fields.
    request = kwargs.get("request") or kwargs
    body = field(request, "body") or request
    model = field(body, "model")
    if isinstance(model, str):
        attrs[S.GEN_AI_REQUEST_MODEL] = model
        if embedding:
            attrs[S.EMBEDDING_MODEL_NAME] = model
    if not _hide_input(config):
        if embedding:
            _content(attrs, S.INPUT_VALUE, kwargs.get("contents"))
        elif resource == "caches":
            cache_config = kwargs.get("config")
            _content(
                attrs,
                S.INPUT_VALUE,
                {
                    key: field(cache_config, key)
                    for key in ("contents", "system_instruction", "tools")
                    if field(cache_config, key) is not None
                },
            )
        else:
            _content(
                attrs,
                S.INPUT_VALUE,
                {
                    key: field(body, key)
                    for key in ("input", "system_instruction", "tools")
                    if field(body, key) is not None
                },
            )
    if S.INPUT_VALUE in attrs:
        attrs[S.INPUT_MIME_TYPE] = "application/json"
    return attrs


class ResponseAttributes:
    """Bounded stream capture; completion usage is summed across continuation hops."""

    def __init__(self, resource: str, config: TraceConfig) -> None:
        self.resource = resource
        self.config = config
        self.attributes: dict = {}
        self.events: list = []
        self.event_size = 0
        self.usage: dict = {}
        self.failed = False

    def response(self, response: Any) -> None:
        attrs = self.attributes
        if self.resource == "models":
            embeddings = field(response, "embeddings") or []
            if not self.config.hide_embedding_vectors and not _hide_output(self.config):
                for i, embedding in enumerate(embeddings[:64]):
                    vector = field(embedding, "values")
                    if isinstance(vector, (list, tuple)) and all(
                        isinstance(v, (int, float)) for v in vector
                    ):
                        attrs[f"{S.EMBEDDING_EMBEDDINGS}.{i}.embedding.vector"] = vector
            if embeddings:
                vector = field(embeddings[0], "values")
                if isinstance(vector, (list, tuple)):
                    attrs[S.GEN_AI_EMBEDDINGS_DIMENSION_COUNT] = len(vector)
            tokens = field(field(response, "metadata"), "billable_character_count")
            if isinstance(tokens, int):
                attrs["google.genai.billable_character_count"] = tokens
        elif self.resource == "caches":
            # Cached content creation is not an LLM inference. Do not attribute
            # cached token counts to generation input/output usage.
            for name in ("name", "model"):
                value = field(response, name)
                if isinstance(value, str):
                    attrs[f"google.genai.cache.{name}"] = value
            tokens = field(field(response, "usage_metadata"), "total_token_count")
            if isinstance(tokens, int):
                attrs["google.genai.cache.token_count"] = tokens
        else:
            self._interaction(response)
            self._usage(field(response, "usage"), add=False)
            if not _hide_output(self.config):
                _content(
                    attrs,
                    S.OUTPUT_VALUE,
                    {
                        key: field(response, key)
                        for key in ("outputs", "steps", "output_text")
                        if field(response, key) is not None
                    },
                )
                if S.OUTPUT_VALUE in attrs:
                    attrs[S.OUTPUT_MIME_TYPE] = "application/json"

    def _interaction(self, interaction: Any) -> None:
        for key, attr in (
            ("model", S.GEN_AI_RESPONSE_MODEL),
            ("id", S.GEN_AI_RESPONSE_ID),
        ):
            value = field(interaction, key)
            if isinstance(value, str):
                self.attributes[attr] = value
        status = field(interaction, "status")
        if isinstance(status, str):
            self.attributes["google.genai.interaction.status"] = status
            self.failed |= status in ("failed", "cancelled")

    def _usage(self, usage: Any, *, add: bool) -> None:
        for name, attr in _USAGE.items():
            value = field(usage, name)
            if isinstance(value, int):
                self.usage[attr] = self.usage.get(attr, 0) + value if add else value
        self.attributes.update(self.usage)

    def event(self, event: Any) -> None:
        event_type = field(event, "event_type")
        if event_type == "error":
            # Error payloads may echo prompts. Only record the error status.
            self.failed = True
        if event_type in (
            "interaction.created",
            "interaction.completed",
            "interaction.status_update",
        ):
            interaction = field(event, "interaction")
            self._interaction(interaction)
            if event_type == "interaction.completed":
                self._usage(field(interaction, "usage"), add=True)
        # Keep distinct content/tool fragments rather than merging unrelated calls.
        # Old SDKs emit content.*, new SDKs emit step.*. Unknown events are ignored.
        if not _hide_output(self.config) and event_type in (
            "content.start",
            "content.delta",
            "content.stop",
            "step.start",
            "step.delta",
            "step.stop",
        ):
            payload = {
                key: field(event, key)
                for key in ("event_type", "index", "content", "step", "delta")
                if field(event, key) is not None
            }
            if self.event_size < _MAX_CONTENT:
                encoded = _json(payload)
                self.event_size += len(encoded.encode("utf-8")) + 2
                if self.event_size <= _MAX_CONTENT:
                    self.events.append(json.loads(encoded))
                else:
                    self.attributes["google.genai.content_truncated"] = True

    def finish(self) -> dict:
        if self.events:
            self.attributes[S.OUTPUT_VALUE] = _json(self.events)
            self.attributes[S.OUTPUT_MIME_TYPE] = "application/json"
        return self.attributes
