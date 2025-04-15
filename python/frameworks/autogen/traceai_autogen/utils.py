from typing import Any


def _to_dict(value: Any) -> Any:
    """
    Recursively converts objects to dictionaries, focusing on essential data.
    """
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_to_dict(item) for item in value]
    if isinstance(value, dict):
        return {k: _to_dict(v) for k, v in value.items()}
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _to_dict(value.to_dict())
    if hasattr(value, "__dict__"):
        return _to_dict(vars(value))
    return str(value)


