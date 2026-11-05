"""Prepare telemetry-only message copies before any string/JSON serialization."""

from typing import Any, Mapping, Optional

from fi_instrumentation import REDACTED_VALUE, TraceConfig
from fi_instrumentation.fi_types import (
    ImageAttributes,
    MessageAttributes,
    MessageContentAttributes,
    SpanAttributes,
)


_IMAGE_KEY = (
    f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.0."
    f"{MessageAttributes.MESSAGE_CONTENTS}.0."
    f"{MessageContentAttributes.MESSAGE_CONTENT_IMAGE}.{ImageAttributes.IMAGE_URL}"
)


def _message_content_copy(content: Any, config: TraceConfig) -> Any:
    if isinstance(content, (list, tuple)):
        return [_message_content_copy(part, config) for part in content]
    if callable(getattr(content, "model_dump", None)):
        content = content.model_dump()
    if not isinstance(content, Mapping):
        return REDACTED_VALUE if config.hide_input_text and isinstance(content, str) else content
    part = dict(content)
    if config.hide_input_text:
        for key in ("text", "refusal"):
            if key in part:
                part[key] = REDACTED_VALUE
    if "image_url" in part:
        image = part["image_url"]
        if config.hide_input_images:
            part["image_url"] = REDACTED_VALUE
        elif isinstance(image, Mapping):
            image = dict(image)
            if "url" in image:
                masked = config.mask(_IMAGE_KEY, image["url"])
                image["url"] = REDACTED_VALUE if masked is None else masked
            part["image_url"] = image
        elif isinstance(image, str):
            masked = config.mask(_IMAGE_KEY, image)
            part["image_url"] = REDACTED_VALUE if masked is None else masked
    return part


def _messages_for_telemetry(messages: Any, config: TraceConfig) -> Optional[list]:
    # SDK message inputs accept arbitrary iterables. Telemetry must not iterate a
    # generator (or a custom one-shot iterable) before the SDK serializes it.
    # Omit that optional telemetry instead of consuming or replacing caller data.
    if not isinstance(messages, (list, tuple)):
        return None
    if config.hide_inputs or config.hide_input_messages:
        return []
    result = []
    for message in messages:
        if isinstance(message, Mapping):
            copy = dict(message)
        elif callable(getattr(message, "model_dump", None)):
            copy = message.model_dump()
        else:
            copy = {key: getattr(message, key) for key in ("role", "content") if hasattr(message, key)}
        if "content" in copy:
            copy["content"] = _message_content_copy(copy["content"], config)
        if config.hide_input_text:
            # Tool arguments, refusals and provider-specific message fields can
            # also carry text. Keep identifiers, not opaque duplicate content.
            for key in copy.keys() - {"role", "name", "tool_call_id", "content"}:
                copy[key] = REDACTED_VALUE
        result.append(copy)
    return result


def _hide_raw_output(config: TraceConfig) -> bool:
    # Raw provider JSON is an opaque duplicate of message content (including
    # tool arguments and provider extensions). Narrow message/text policies must
    # cover it too. Accounting and model attribution are extracted separately.
    return bool(config.hide_outputs or config.hide_output_messages or config.hide_output_text)
