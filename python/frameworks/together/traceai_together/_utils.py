import logging
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

from fi_instrumentation import safe_json_dumps
from opentelemetry import trace as trace_api
from opentelemetry.util.types import AttributeValue

from traceai_together._with_span import _WithSpan

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def _to_dict(obj: Any) -> Dict[str, Any]:
    """Convert an object to a dictionary."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return {"value": str(obj)}


# Response-side fields that may be recorded while input settings are on. Everything else, in particular the
# request echo (CompletionResponse.prompt), is left out rather than stripped by name.
_RESPONSE_FIELDS = ("id", "object", "created", "model", "usage", "data", "system_fingerprint")
_CHOICE_FIELDS = ("index", "finish_reason", "text")
_MESSAGE_FIELDS = ("role", "content", "tool_calls", "function_call", "refusal")


def _input_hidden(tracer: Any) -> bool:
    config = getattr(tracer, "_self_config", None)
    return bool(config is not None and (config.hide_inputs or config.hide_input_messages or config.hide_input_text))


def _response_side(response: Dict[str, Any]) -> Dict[str, Any]:
    """The response fields that carry generated content, usage and identity only."""
    out: Dict[str, Any] = {k: response[k] for k in _RESPONSE_FIELDS if k in response}
    choices = response.get("choices")
    if isinstance(choices, list):
        kept = []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            item = {k: choice[k] for k in _CHOICE_FIELDS if k in choice}
            message = choice.get("message")
            if isinstance(message, dict):
                item["message"] = {k: message[k] for k in _MESSAGE_FIELDS if k in message}
            kept.append(item)
        out["choices"] = kept
    return out


def _response_dict(response: Any, tracer: Any) -> Dict[str, Any]:
    """The response as a dict; under any input hide setting, only its response-side fields."""
    data = _to_dict(response)
    return _response_side(data) if _input_hidden(tracer) else data


def _flatten(
    mapping: Mapping[str, Any], prefix: str = ""
) -> Iterator[Tuple[str, AttributeValue]]:
    """Flatten a nested dictionary into dot-separated keys."""
    for key, value in mapping.items():
        if value is None:
            continue
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, Mapping):
            yield from _flatten(value, full_key)
        elif isinstance(value, (list, tuple)):
            if value and isinstance(value[0], Mapping):
                for i, item in enumerate(value):
                    yield from _flatten(item, f"{full_key}.{i}")
            else:
                yield full_key, safe_json_dumps(value)
        else:
            yield full_key, value


def _finish_tracing(
    status: trace_api.Status,
    with_span: _WithSpan,
    attributes: Optional[Iterator[Tuple[str, AttributeValue]]] = None,
    extra_attributes: Optional[Iterator[Tuple[str, AttributeValue]]] = None,
) -> None:
    """Finish tracing with status and attributes."""
    if attributes:
        for key, value in attributes:
            if value is not None:
                with_span._span.set_attribute(key, value)
    if extra_attributes:
        for key, value in extra_attributes:
            if value is not None:
                with_span._span.set_attribute(key, value)
    with_span.finish_tracing(status=status)
