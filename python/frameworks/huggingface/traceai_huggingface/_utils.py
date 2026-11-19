import logging
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

from fi_instrumentation import safe_json_dumps
from opentelemetry import trace as trace_api
from opentelemetry.util.types import AttributeValue

from traceai_huggingface._with_span import _WithSpan

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
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return {"value": str(obj)}


# Response-side fields that may be recorded while input settings are on. The request echo (the prefill tokens
# of a text generation's details) is left out rather than stripped by name.
_RESPONSE_FIELDS = ("id", "object", "created", "model", "usage", "generated_text", "system_fingerprint")
_DETAIL_FIELDS = ("finish_reason", "generated_tokens", "seed")
_CHOICE_FIELDS = ("index", "finish_reason")
_MESSAGE_FIELDS = ("role", "content", "tool_calls", "tool_call_id", "name")


def _input_hidden(tracer: Any) -> bool:
    config = getattr(tracer, "_self_config", None)
    return bool(config is not None and (config.hide_inputs or config.hide_input_messages or config.hide_input_text or config.hide_input_images))


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _pick(obj: Any, names: Tuple[str, ...]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name in names:
        value = _field(obj, name)
        if value is not None:
            out[name] = dict(value) if isinstance(value, Mapping) else value
    return out


def _response_view(response: Any, tracer: Any) -> Any:
    """The response for the extractors; under any input hide setting, only its response-side fields."""
    if response is None or isinstance(response, (str, bytes)) or not _input_hidden(tracer):
        return response
    if not isinstance(response, Mapping) and not hasattr(response, "__dict__"):
        return response
    out = _pick(response, _RESPONSE_FIELDS)
    details = _field(response, "details")
    if details is not None:
        out["details"] = _pick(details, _DETAIL_FIELDS)
    choices = _field(response, "choices")
    if choices:
        kept = []
        for choice in choices:
            item = _pick(choice, _CHOICE_FIELDS)
            message = _field(choice, "message")
            if message is not None:
                item["message"] = _pick(message, _MESSAGE_FIELDS)
            kept.append(item)
        out["choices"] = kept
    return out


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
