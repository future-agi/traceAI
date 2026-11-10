"""Bounded structural redaction for native span attributes.

This helper is not an exporter or a complete privacy boundary. It
recognises credential *field names* in structured data, not secrets in free
text or arbitrary encodings. Inputs are never mutated.
"""
from __future__ import annotations

import json
import re
from typing import Any

REDACTED = "[REDACTED]"
_MAX_DEPTH = 24
_MAX_NODES = 10000
_MAX_TEXT = 1_000_000
_MAX_KEY = 512
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_PARTS = re.compile(r"[^a-z0-9]+")
_CREDENTIAL_NAMES = frozenset({
    "apikey", "xapikey", "authorization", "proxyauthorization",
    "accesstoken", "refreshtoken", "authtoken", "bearertoken", "idtoken",
    "token", "clientsecret", "secretkey", "clientkey", "password", "passwd",
    "credential", "credentials", "privatekey",
    "accesskeyid", "secretaccesskey", "sessiontoken", "securitytoken",
})


def _credential_field(key: str) -> bool:
    # Treat dots and brackets as path boundaries, not as separators inside a
    # single name. A credential-bearing path can continue after the credential
    # segment (e.g. headers.authorization.value). Matching complete segments
    # avoids hiding useful metrics such as input_tokens or token_count.
    for segment in re.split(r"[.\[\]]+", key):
        parts = [p for p in _PARTS.split(_CAMEL.sub(" ", segment).lower()) if p]
        if "".join(parts) in _CREDENTIAL_NAMES:
            return True
        # A flattened underscore form such as client_config_api_key has no
        # path delimiter. Do not match bare "token" as a suffix: e.g.
        # input_token is a legitimate usage metric.
        if any("".join(parts[i:]) in (_CREDENTIAL_NAMES - {"token"})
               for i in range(1, len(parts))):
            return True
    return False


class _Limit(Exception):
    pass


def redact_structured(value: Any) -> Any:
    """Return an independent sanitized JSON-like value or REDACTED on limits.

    JSON-serialized objects and arrays nested inside other JSON values are also
    inspected. Ordinary strings remain unchanged; callers must separately apply
    the relevant content and error-detail policy before exporting them.
    """
    remaining = [_MAX_NODES]
    active: set[int] = set()

    def visit(obj: Any, depth: int) -> Any:
        remaining[0] -= 1
        if depth > _MAX_DEPTH or remaining[0] < 0:
            raise _Limit
        if isinstance(obj, str):
            if len(obj) > _MAX_TEXT:
                raise _Limit
            stripped = obj.strip()
            if stripped.startswith(("{", "[")):
                try:
                    parsed = json.loads(obj)
                except (ValueError, RecursionError):
                    # An invalid or truncated structured-looking value must
                    # not fall back to publishing its potentially secret text.
                    return REDACTED
                return json.dumps(visit(parsed, depth + 1), ensure_ascii=False, separators=(",", ":"))
            return obj
        if obj is None or isinstance(obj, (bool, int, float)):
            return obj
        if isinstance(obj, (dict, list, tuple)):
            ident = id(obj)
            if ident in active:
                raise _Limit
            active.add(ident)
            try:
                if isinstance(obj, dict):
                    result = {}
                    for key, item in obj.items():
                        remaining[0] -= 1
                        if remaining[0] < 0 or not isinstance(key, str) or len(key) > _MAX_KEY:
                            raise _Limit
                        result[key] = REDACTED if _credential_field(key) else visit(item, depth + 1)
                    return result
                return [visit(item, depth + 1) for item in obj]
            finally:
                active.remove(ident)
        # Do not call repr/str on runtime objects, which may contain credentials.
        raise _Limit

    try:
        return visit(value, 0)
    except (ValueError, TypeError, RecursionError, _Limit):
        return REDACTED
