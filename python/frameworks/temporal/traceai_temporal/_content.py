"""Sandbox-safe, bounded snapshots of already-decoded Temporal arguments/results.

Never run a payload converter twice: converters and object serialization hooks are
application code. Only exact builtins are inspected; everything else is a type name.
There is no environment access, I/O, clock or randomness in this module.
"""

import json
import math
from itertools import islice
from typing import Any

MAX_BYTES = 4 * 1024 * 1024
MAX_NODES = 4096
MAX_DEPTH = 32
_OMITTED = "<omitted: content limit>"


def serialize(value: Any) -> str:
    """Return deterministic JSON, never a raw/truncated prefix of a value.

    The shared node/text budgets bound branching and cycles before json.dumps.
    Account for worst-case JSON escaping (six ASCII bytes per character), as
    well as container punctuation, so even the intermediate snapshot is bounded.
    """
    budget = [MAX_NODES, MAX_BYTES - MAX_NODES * 64]

    def text(value: str) -> str:
        cost = len(value) * 6 + 2
        if cost > budget[1]:
            return _OMITTED
        budget[1] -= cost
        return value

    def visit(value: Any, depth: int) -> Any:
        budget[0] -= 1
        if budget[0] < 0 or depth > MAX_DEPTH:
            return _OMITTED
        typ = type(value)
        if typ is str:
            return text(value)
        if typ is bool or typ is type(None):
            return value
        if typ is int:
            # Avoid unbounded big-int conversion and Python's digit limit.
            return value if -(1 << 63) <= value < (1 << 63) else text("int")
        if typ is float:
            return value if math.isfinite(value) else (
                "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"
            )
        if typ is dict:
            out = {}
            for key, item in islice(dict.items(value), max(0, budget[0] // 2)):
                name = key if type(key) is str else type.__dict__["__name__"].__get__(type(key))
                # Never detach a value from its structural privacy marker. If a
                # key cannot be retained, its value must not survive under an
                # omission/type-name key (e.g. authorization -> omitted key).
                if len(name) * 6 + 2 > budget[1]:
                    out[_OMITTED] = _OMITTED
                    break
                budget[0] -= 1
                out[text(name)] = visit(item, depth + 1) if type(key) is str else "<omitted: unsupported key>"
                if budget[0] <= 0:
                    break
            if len(value) > len(out):
                out[_OMITTED] = _OMITTED
            return out
        if typ is list or typ is tuple:
            out = []
            for item in islice(value, max(0, budget[0])):
                out.append(visit(item, depth + 1))
                if budget[0] <= 0:
                    break
            if len(value) > len(out):
                out.append(_OMITTED)
            return out
        # Bypass even a custom metaclass's __getattribute__/__name__ property.
        return text(type.__dict__["__name__"].__get__(typ))

    try:
        result = json.dumps(visit(value, 0), sort_keys=True, ensure_ascii=True,
                            separators=(",", ":"), allow_nan=False)
        return result if len(result) <= MAX_BYTES else json.dumps(_OMITTED)
    except Exception:
        # No exception text: it could itself contain application content.
        return json.dumps(_OMITTED)


def content_attributes(side: str, value: Any) -> dict:
    return {side + ".value": serialize(value), side + ".mime_type": "application/json"}
