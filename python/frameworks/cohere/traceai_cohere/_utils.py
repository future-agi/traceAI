import logging
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

from fi_instrumentation import safe_json_dumps
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
