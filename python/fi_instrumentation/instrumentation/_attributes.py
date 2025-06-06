import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime
from json import JSONEncoder
from types import ModuleType
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple, Union

from fi_instrumentation.fi_types import (
    FiMimeTypeValues,
    FiSpanKindValues,
    SpanAttributes,
)
from opentelemetry.util.types import AttributeValue
from typing_extensions import TypeGuard

from ._types import FiMimeType, FiSpanKind

pydantic: Optional[ModuleType]
try:
    import pydantic
except ImportError:
    pydantic = None

if TYPE_CHECKING:
    from _typeshed import DataclassInstance


def get_span_kind_attributes(kind: "FiSpanKind", /) -> Dict[str, AttributeValue]:
    normalized_kind = _normalize_fi_span_kind(kind)
    return {
        FI_SPAN_KIND: normalized_kind.value,
    }


def get_input_attributes(
    value: Any,
    *,
    mime_type: Optional[FiMimeType] = None,
) -> Dict[str, AttributeValue]:
    normalized_mime_type: Optional[FiMimeTypeValues] = None
    if mime_type is not None:
        normalized_mime_type = _normalize_mime_type(mime_type)
    if normalized_mime_type is FiMimeTypeValues.TEXT:
        value = str(value)
    elif normalized_mime_type is FiMimeTypeValues.JSON:
        if not isinstance(value, str):
            value = _json_serialize(value)
    else:
        value, normalized_mime_type = _infer_serialized_io_value_and_mime_type(value)
    attributes = {
        INPUT_VALUE: value,
    }
    if normalized_mime_type is not None:
        attributes[INPUT_MIME_TYPE] = normalized_mime_type.value
    return attributes


def get_output_attributes(
    value: Any,
    *,
    mime_type: Optional[FiMimeType] = None,
) -> Dict[str, AttributeValue]:
    normalized_mime_type: Optional[FiMimeTypeValues] = None
    if mime_type is not None:
        normalized_mime_type = _normalize_mime_type(mime_type)
    if normalized_mime_type is FiMimeTypeValues.TEXT:
        value = str(value)
    elif normalized_mime_type is FiMimeTypeValues.JSON:
        if not isinstance(value, str):
            value = _json_serialize(value)
    else:
        value, normalized_mime_type = _infer_serialized_io_value_and_mime_type(value)
    attributes = {
        OUTPUT_VALUE: value,
    }
    if normalized_mime_type is not None:
        attributes[OUTPUT_MIME_TYPE] = normalized_mime_type.value
    return attributes


def _infer_serialized_io_value_and_mime_type(
    value: Any,
) -> Tuple[Any, Optional[FiMimeTypeValues]]:
    if isinstance(value, str):
        return value, FiMimeTypeValues.TEXT
    if isinstance(value, (bool, int, float)):
        return value, None
    if isinstance(value, Sequence):
        for element_type in (str, bool, int, float):
            if all(isinstance(element, element_type) for element in value):
                return value, None
        return _json_serialize(value), FiMimeTypeValues.JSON
    if isinstance(value, Mapping):
        return _json_serialize(value), FiMimeTypeValues.JSON
    if _is_dataclass_instance(value):
        return _json_serialize(value), FiMimeTypeValues.JSON
    if pydantic is not None and isinstance(value, pydantic.BaseModel):
        return _json_serialize(value), FiMimeTypeValues.JSON
    return str(value), FiMimeTypeValues.TEXT


class IOValueJSONEncoder(JSONEncoder):
    def default(self, obj: Any) -> Any:
        try:
            if _is_dataclass_instance(obj):
                return asdict(obj)
            if pydantic is not None and isinstance(obj, pydantic.BaseModel):
                return obj.model_dump()
            if isinstance(obj, datetime):
                return obj.isoformat()
            return super().default(obj)
        except Exception:
            return str(obj)


def _json_serialize(obj: Any, **kwargs: Any) -> str:
    """
    Safely JSON dumps input and handles special types such as dataclasses and
    pydantic models.
    """
    return json.dumps(
        obj,
        cls=IOValueJSONEncoder,
        ensure_ascii=False,
    )


def get_tool_attributes(
    *,
    name: str,
    description: Optional[str] = None,
    parameters: Union[str, Dict[str, Any]],
) -> Dict[str, AttributeValue]:
    if isinstance(parameters, str):
        parameters_json = parameters
    elif isinstance(parameters, Mapping):
        parameters_json = _json_serialize(parameters)
    else:
        raise ValueError(f"Invalid parameters type: {type(parameters)}")
    attributes: Dict[str, AttributeValue] = {
        TOOL_NAME: name,
        TOOL_PARAMETERS: parameters_json,
    }
    if description is not None:
        attributes[TOOL_DESCRIPTION] = description
    return attributes


def _normalize_mime_type(mime_type: FiMimeType) -> FiMimeTypeValues:
    if isinstance(mime_type, FiMimeTypeValues):
        return mime_type
    try:
        return FiMimeTypeValues(mime_type)
    except ValueError:
        raise ValueError(f"Invalid mime type: {mime_type}")


def _normalize_fi_span_kind(
    kind: "FiSpanKind",
) -> FiSpanKindValues:
    if isinstance(kind, FiSpanKindValues):
        return kind
    if not kind.islower():
        raise ValueError("kind must be lowercase if provided as a string")
    try:
        return FiSpanKindValues(kind.upper())
    except ValueError:
        raise ValueError(f"Invalid Fi span kind: {kind}")


def _is_dataclass_instance(obj: Any) -> TypeGuard["DataclassInstance"]:
    """
    dataclasses.is_dataclass return true for both dataclass types and instances.
    This function returns true only for instances.

    See https://github.com/python/cpython/blob/05d12eecbde1ace39826320cadf8e673d709b229/Lib/dataclasses.py#L1391
    """
    cls = type(obj)
    return hasattr(cls, "__dataclass_fields__")


# span attributes
INPUT_MIME_TYPE = SpanAttributes.INPUT_MIME_TYPE
INPUT_VALUE = SpanAttributes.INPUT_VALUE
FI_SPAN_KIND = SpanAttributes.FI_SPAN_KIND
OUTPUT_MIME_TYPE = SpanAttributes.OUTPUT_MIME_TYPE
OUTPUT_VALUE = SpanAttributes.OUTPUT_VALUE
TOOL_DESCRIPTION = SpanAttributes.TOOL_DESCRIPTION
TOOL_NAME = SpanAttributes.TOOL_NAME
TOOL_PARAMETERS = SpanAttributes.TOOL_PARAMETERS
