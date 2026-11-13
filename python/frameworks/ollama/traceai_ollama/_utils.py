import logging
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.instrumentation.config import REDACTED_VALUE
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry import trace as trace_api
from opentelemetry.util.types import AttributeValue

from traceai_ollama._with_span import _WithSpan

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


def _redact_tool_calls(tool_calls: Any) -> Any:
    """Mask argument payloads in a detached telemetry copy, preserving tool names."""
    if not isinstance(tool_calls, (list, tuple)):
        return tool_calls
    redacted = []
    for tool in tool_calls:
        # SDK models are iterable but are not Mappings. Normalize both levels
        # before redaction, including a mapping containing a typed function.
        item = dict(tool) if isinstance(tool, Mapping) else dict(_to_dict(tool))
        if item.get("function") is not None:
            value = item["function"]
            function = dict(value) if isinstance(value, Mapping) else dict(_to_dict(value))
            if "arguments" in function:
                function["arguments"] = REDACTED_VALUE
            item["function"] = function
        redacted.append(item)
    return redacted


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


def _get_output_value(response: Dict[str, Any]) -> str:
    """Extract output value from response."""
    if "message" in response:
        return response["message"].get("content", "")
    if "response" in response:
        return response.get("response", "")
    return ""
