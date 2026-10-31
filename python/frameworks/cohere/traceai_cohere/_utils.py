import logging
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import MessageAttributes, ToolCallAttributes
from opentelemetry import trace as trace_api
from opentelemetry.util.types import AttributeValue

from traceai_cohere._with_span import _WithSpan

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


def _message_attributes(
    message: Any, prefix: str
) -> Iterator[Tuple[str, AttributeValue]]:
    """Use canonical keys so the core masks message content and tool arguments."""
    message = _to_dict(message)
    yield f"{prefix}.{MessageAttributes.MESSAGE_ROLE}", message.get("role") or ""
    content = message.get("content", message.get("message"))
    if content is not None:
        if not isinstance(content, str):
            content = safe_json_dumps(content)
        yield f"{prefix}.{MessageAttributes.MESSAGE_CONTENT}", content
    if message.get("tool_call_id"):
        yield (
            f"{prefix}.{MessageAttributes.MESSAGE_TOOL_CALL_ID}",
            message["tool_call_id"],
        )
    for index, item in enumerate(message.get("tool_calls") or []):
        tool = _to_dict(item)
        function = tool.get("function") or {}
        key = f"{prefix}.{MessageAttributes.MESSAGE_TOOL_CALLS}.{index}"
        if tool.get("id"):
            yield f"{key}.{ToolCallAttributes.TOOL_CALL_ID}", tool["id"]
        yield (
            f"{key}.{ToolCallAttributes.TOOL_CALL_FUNCTION_NAME}",
            function.get("name", tool.get("name", "")),
        )
        arguments = function.get("arguments", tool.get("parameters", {}))
        yield (
            f"{key}.{ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}",
            (arguments if isinstance(arguments, str) else safe_json_dumps(arguments)),
        )
