"""Apply TraceConfig's field policy to the SDK's serialized OTel messages."""

import json
import logging
from typing import Any, Callable, Optional, Union
from urllib.parse import urlsplit, urlunsplit

from fi_instrumentation import REDACTED_VALUE
from fi_instrumentation.fi_types import (
    EmbeddingAttributes,
    ImageAttributes,
    MessageAttributes,
    MessageContentAttributes,
    SpanAttributes,
)
from opentelemetry.util.types import AttributeValue
from wrapt import ObjectProxy

logger = logging.getLogger(__name__)


class _EmbeddingTraceConfig(ObjectProxy):
    """Use the input-text policy also on published cores predating its alias."""

    def mask(self, key, value):
        if key.startswith(SpanAttributes.EMBEDDING_EMBEDDINGS + ".") and key.endswith(
            "." + EmbeddingAttributes.EMBEDDING_TEXT
        ):
            # Core 1.2.2 supports selective message privacy but not the embedding
            # text alias yet. Preserve that policy without duplicating payloads.
            key = f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}"
        return self.__wrapped__.mask(key, value)


class _NativeTraceConfig(ObjectProxy):
    """Keep native JSON spans, using the same policy as flattened FI messages.

    Adapt the config rather than only Span.set_attribute so creation attributes,
    batched updates and SDK-created child spans all cross the same boundary.
    """

    def mask(
        self,
        key: str,
        value: Union[AttributeValue, Callable[[], AttributeValue]],
    ) -> Optional[AttributeValue]:
        # Native OTel splits invocation parameters into individual attributes.
        # Keep model identity, but apply the core's aggregate parameter policy
        # to every other request parameter, including future SDK additions.
        policy_key = (
            SpanAttributes.GEN_AI_REQUEST_PARAMETERS
            if key.startswith("gen_ai.request.") and key != "gen_ai.request.model"
            else key
        )
        value = self.__wrapped__.mask(policy_key, value)
        if key in ("http.url", "url.full") and isinstance(value, str):
            # SDK HTTP metadata is independent of message-hiding flags. Never
            # export proxy userinfo, including percent-encoded credentials.
            try:
                url = urlsplit(value)
                return urlunsplit(url._replace(netloc=url.netloc.rsplit("@", 1)[-1]))
            except ValueError:
                return None
        if (
            key
            not in (
                SpanAttributes.GEN_AI_INPUT_MESSAGES,
                SpanAttributes.GEN_AI_OUTPUT_MESSAGES,
            )
            or value is None
            or value == REDACTED_VALUE
        ):
            return value
        # Native messages are JSON arrays. Never forward an opaque value whose
        # contents cannot be checked against the configured privacy policy.
        if not isinstance(value, str):
            return None
        try:
            messages = json.loads(value)
        except (ValueError, RecursionError):
            # Parsing errors may contain the private payload: log no details.
            logger.debug("Discarding malformed Mistral message attributes")
            return None
        if not isinstance(messages, list):
            return None
        for message_index, message in enumerate(messages):
            if not isinstance(message, dict) or not isinstance(
                message.get("parts"), list
            ):
                return None
            parts = []
            for part_index, part in enumerate(message["parts"]):
                if not isinstance(part, dict):
                    return None
                prefix = f"{key}.{message_index}.{MessageAttributes.MESSAGE_CONTENTS}.{part_index}"
                if part.get("type") in ("text", "reasoning", "tool_call_response"):
                    # The SDK stores tool-result text/chunks in response rather
                    # than content. Mask the payload, never the tool-call ID.
                    field = (
                        "response"
                        if part["type"] == "tool_call_response"
                        else "content"
                    )
                    policy_key = (
                        f"{prefix}.{MessageContentAttributes.MESSAGE_CONTENT_TEXT}"
                    )
                elif (
                    key == SpanAttributes.GEN_AI_INPUT_MESSAGES
                    and part.get("type") == "uri"
                    and part.get("modality") == "image"
                ):
                    field = "uri"
                    policy_key = f"{prefix}.{MessageContentAttributes.MESSAGE_CONTENT_IMAGE}.{ImageAttributes.IMAGE_URL}"
                else:
                    parts.append(part)
                    continue
                masked = self.__wrapped__.mask(policy_key, part.get(field))
                if (
                    key == SpanAttributes.GEN_AI_INPUT_MESSAGES
                    and part.get("type") == "tool_call_response"
                    and isinstance(masked, list)
                ):
                    masked = self._mask_tool_images(masked, prefix)
                if masked is not None:
                    part[field] = masked
                    parts.append(part)
            message["parts"] = parts
        return json.dumps(messages)

    def _mask_tool_images(self, chunks: list[Any], prefix: str) -> list[Any]:
        # Unlike user messages, the SDK leaves tool responses as raw content
        # chunks, with image_url represented by either a string or a URL object.
        result = []
        for index, chunk in enumerate(chunks):
            if not isinstance(chunk, dict) or chunk.get("type") != "image_url":
                result.append(chunk)
                continue
            image = chunk.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if not isinstance(url, str):
                continue
            policy_key = f"{prefix}.{index}.{MessageContentAttributes.MESSAGE_CONTENT_IMAGE}.{ImageAttributes.IMAGE_URL}"
            masked = self.__wrapped__.mask(policy_key, url)
            if masked is not None:
                chunk["image_url"] = (
                    {**image, "url": masked} if isinstance(image, dict) else masked
                )
                result.append(chunk)
        return result
