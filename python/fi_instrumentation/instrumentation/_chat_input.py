"""A narrow, core-masked representation of chat input, not a trusted JSON marker.

Only exact built-in containers/scalars are inspected. No serializers, properties,
iterators or Mapping protocols are invoked. Unknown fields are omitted; malformed
known fields fail closed. The caller's objects are never modified. Validation and
policy run on EVERY use (including objects forged without calling __init__).
"""

import json
import math
from typing import TYPE_CHECKING, Any, Optional

from .pii_redaction import redact_pii_in_string

if TYPE_CHECKING:
    from .config import TraceConfig

_REDACTED = "__REDACTED__"
_ROLES = frozenset({"system", "developer", "user", "assistant", "tool", "function"})
_NUMBERS = frozenset({
    "frequency_penalty", "presence_penalty", "max_completion_tokens", "max_tokens",
    "temperature", "top_p", "top_logprobs", "n", "seed",
})
_BOOLEANS = frozenset({"logprobs", "parallel_tool_calls", "store"})
_TEXT = frozenset({"stop", "user"})
# These fields have no modality boundary. Under a granular policy they are
# redacted, not recursively searched for strings that merely look like images.
_OPAQUE = frozenset({
    "audio", "function_call", "functions", "logit_bias", "modalities",
    "prediction", "reasoning_effort", "response_format", "service_tier",
    "stream_options", "tool_choice", "tools",
})


def _object(value: Any) -> bool:
    return type(value) is dict and all(type(key) is str for key in value)


def _passive(value: Any, budget: list, depth: int = 0) -> Any:
    """Bounded copy; reject active subclasses, cycles and non-JSON values."""
    budget[0] -= 1
    if budget[0] < 0 or depth > 32:
        raise ValueError("chat input exceeds copy limits")
    kind = type(value)
    if value is None or kind is str or kind is bool or kind is int:
        return value
    if kind is float and math.isfinite(value):
        return value
    if kind is list or kind is tuple:
        return [_passive(item, budget, depth + 1) for item in value]
    if _object(value):
        return {key: _passive(item, budget, depth + 1) for key, item in value.items()}
    raise ValueError("chat input must contain passive JSON values")


def _content(value: Any, config: "TraceConfig") -> Any:
    if value is None:
        return None
    if type(value) is str:
        return _REDACTED if config.hide_input_text else value
    if type(value) is not list:
        raise ValueError("unsupported chat content")
    result = []
    for part in value:
        if not _object(part) or type(part.get("type")) is not str:
            raise ValueError("untyped chat content")
        kind = part["type"]
        if kind in ("text", "refusal"):
            if type(part.get(kind)) is not str:
                raise ValueError("invalid text content")
            result.append({"type": kind, kind: _REDACTED if config.hide_input_text else part[kind]})
        elif kind == "image_url":
            image = part.get("image_url")
            if type(image) is str:
                url, detail = image, None
            elif _object(image):
                url, detail = image.get("url"), image.get("detail")
            else:
                raise ValueError("invalid image content")
            if type(url) is not str:
                raise ValueError("invalid image reference")
            prefix = url[:11].lower()
            if not prefix.startswith(("https://", "http://", "data:image/")):
                raise ValueError("invalid image reference")
            if detail is not None and (type(detail) is not str or detail not in ("auto", "low", "high")):
                raise ValueError("invalid image detail")
            # Data URL markers are ASCII case-insensitive (";BASE64," is valid).
            hidden = config.hide_input_images or (
                prefix == "data:image/" and "base64" in url.lower()
                and len(url) > config.base64_image_max_length
            )
            if hidden:
                result.append({"type": kind, "image_url": _REDACTED})
            elif type(image) is str:
                result.append({"type": kind, "image_url": url})
            else:
                reference = {"url": url}
                if detail is not None:
                    reference["detail"] = detail
                result.append({"type": kind, "image_url": reference})
        elif kind == "input_audio":
            audio = part.get("input_audio")
            if not _object(audio):
                raise ValueError("invalid audio content")
            data, fmt = audio.get("data"), audio.get("format")
            if type(data) is not str or type(fmt) is not str:
                raise ValueError("invalid audio content")
            # Speech carries the same semantic content as text; the binary blob
            # is masked when input text is private, not under the image flag.
            result.append(
                {
                    "type": kind,
                    "input_audio": {
                        "data": _REDACTED if config.hide_input_text else data,
                        "format": fmt,
                    },
                }
            )
        else:
            # Never trust a caller-supplied modality label we do not understand.
            result.append(_REDACTED)
    return result


def _parameters(payload: dict, config: "TraceConfig", budget: list) -> dict:
    result = {}
    if config.hide_inputs or config.hide_llm_invocation_parameters:
        return result
    for key in (_NUMBERS | _BOOLEANS | _TEXT | _OPAQUE) & payload.keys():
        value = payload[key]
        if value is None:
            continue
        if key in _NUMBERS:
            if (type(value) is not int and type(value) is not float) or (type(value) is float and not math.isfinite(value)):
                raise ValueError("invalid numeric parameter")
            result[key] = value
        elif key in _BOOLEANS:
            if type(value) is not bool:
                raise ValueError("invalid boolean parameter")
            result[key] = value
        elif key in _TEXT:
            if config.hide_input_text:
                result[key] = _REDACTED
            elif type(value) is str:
                result[key] = value
            elif type(value) is list:
                # Check the shared allowance before ANY scan, iteration or copy.
                # The container and every string leaf each consume one node.
                if len(value) + 1 > budget[0]:
                    raise ValueError("chat input exceeds copy limits")
                copied = _passive(value, budget)
                if not all(type(item) is str for item in copied):
                    raise ValueError("invalid text parameter")
                result[key] = copied
            else:
                raise ValueError("invalid text parameter")
        else:
            result[key] = (_REDACTED if config.hide_input_messages or config.hide_input_text or config.hide_input_images
                           else _passive(value, budget))
    return result


def _redact_validated_leaves(value: Any) -> Any:
    """Scan only the validated, detached built-ins, before JSON/str escaping."""
    if type(value) is str:
        return redact_pii_in_string(value)
    if type(value) is list:
        return [_redact_validated_leaves(item) for item in value]
    if type(value) is dict:
        # Keys (e.g. JSON-schema property names) are escaped by json.dumps too.
        return {redact_pii_in_string(key): _redact_validated_leaves(item) for key, item in value.items()}
    return value


class ChatInput:
    """Unserialized chat request for FiSpan.set_attribute's input keys only.

    Import ChatInput from fi_instrumentation.instrumentation.config.
    Use ``span.set_attribute("input.value", ChatInput(request))`` (or
    ``raw.input``). Creation-time sampler results remain opaque and fail closed;
    this type does not let a sampler mark returned strings as trusted.

    Core validates model/messages and allowlisted chat request parameters before
    serializing. This is NOT an assertion that input was sanitized by a caller.
    Opaque strings and lazy values passed to TraceConfig.mask retain their old
    fail-closed behavior. Outputs and arbitrary attribute aliases are unsupported.
    """

    __slots__ = ("_payload",)

    def __init__(self, payload: Any) -> None:
        # Do not inspect even passive data before the whole-input policy runs.
        self._payload = payload

    def masked(self, config: "TraceConfig") -> Optional[dict]:
        """Return a fresh, policy-filtered built-in copy, or None (fail closed)."""
        if config.hide_inputs or config.hide_input_messages:
            return None
        try:
            # Validate again even when the slot was forged or changed by caller.
            payload = object.__getattribute__(self, "_payload")
            if not _object(payload) or type(payload.get("model")) is not str:
                return None
            result = {"model": payload["model"]}
            budget = [10000]
            if "messages" in payload:
                messages = payload["messages"]
                if (type(messages) is not list and type(messages) is not tuple) or len(messages) > 10000:
                    return None
                result["messages"] = []
                for message in messages:
                    if not _object(message):
                        return None
                    role = message.get("role")
                    if type(role) is not str or role not in _ROLES:
                        return None
                    copy = {"role": role}
                    if "content" in message:
                        # Copy first so no custom leaf can reach masking or JSON.
                        copy["content"] = _content(_passive(message["content"], budget), config)
                    for key in ("name", "tool_call_id", "refusal", "tool_calls", "function_call", "audio"):
                        if key in message:
                            copy[key] = (_REDACTED if config.hide_input_text or config.hide_input_images
                                         else _passive(message[key], budget))
                    result["messages"].append(copy)
            result.update(_parameters(payload, config, budget))
            return _redact_validated_leaves(result) if config.pii_redaction else result
        except (AttributeError, ValueError, RecursionError):
            return None

    def masked_invocation_parameters(self, config: "TraceConfig") -> Optional[dict]:
        """Independently allowed parameters, without inspecting hidden messages.

        Uses the same strict validator and actual policy as the input projection.
        Message hiding does not hide typed invocation metadata, but opaque fields
        that could duplicate message/tool content are still redacted.
        """
        if config.hide_inputs or config.hide_llm_invocation_parameters:
            return None
        try:
            payload = object.__getattribute__(self, "_payload")
            if not _object(payload):
                return None
            result = _parameters(payload, config, [10000])
            return _redact_validated_leaves(result) if config.pii_redaction else result
        except (AttributeError, ValueError, RecursionError):
            return None


def mask_chat_input(value: ChatInput, config: "TraceConfig") -> str:
    # Call the class implementation, never an instance-provided serializer.
    payload = ChatInput.masked(value, config)
    if payload is None:
        return _REDACTED
    try:
        return json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (ValueError, OverflowError):
        # Even built-in integers can exceed the interpreter's JSON digit limit.
        return _REDACTED
