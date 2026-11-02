"""Conservative content redaction, applied before native truncation and at export."""

from __future__ import annotations

import json
import re
from typing import Any

from ._artifacts import ArtifactError, parse
from ._transport import Settings

_SECRET_KEY = re.compile(
    r"password|secret|token|api.?key|authorization|credential", re.I
)
_SECRET_VALUE = re.compile(
    r"(?i)(?:bearer\s+\S+|(?:sk|fi|api)[_-][a-z0-9_-]{12,}|(?:password|secret|token|api[_-]?key)\s*[=:]\s*[^\s,;]+)"
)
# Embedded JSON often arrives as ordinary text or as JSON escaped inside tool arguments.
# Redact the entire string rather than trying to guess where a quoted value ends.
_QUOTED_SECRET = re.compile(
    r"""(?i)\\?["'](?:password|secret|token|api[_-]?key|authorization|credential)\\?["']\s*:"""
)
_LOCATION = re.compile(
    r"(?:https?://[^\s\"']+|file://[^\s\"']+|(?<![\w])(?:[A-Za-z]:[\\/]|/)[^\s\"']+)"
)


class Privacy:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def text(self, value: str, limit: int = 2048) -> str:
        if _QUOTED_SECRET.search(value):
            if value.lstrip().startswith(("{", "[")) and len(value) <= 16384:
                try:
                    clean = json.dumps(self._nested(parse(value.encode())))
                    return clean.encode("utf-8")[:limit].decode(
                        "utf-8", errors="ignore"
                    )
                except ArtifactError:
                    pass
            return "[redacted]"
        for secret in (self.settings.api_key, self.settings.secret_key):
            value = value.replace(secret, "[redacted]")
        value = _SECRET_VALUE.sub("[redacted]", value)
        value = _LOCATION.sub("[location]", value)
        return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")

    def tree(self, value: Any) -> Any:
        # Artifact complexity has already been bounded by the reader.
        if isinstance(value, dict):
            return {
                self.text(k, 256): "[redacted]"
                if _SECRET_KEY.search(k) and not k.endswith(("tokens", "token_count"))
                else self.tree(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.tree(v) for v in value]
        if isinstance(value, str):
            if value.lstrip().startswith(("{", "[")):
                if len(value) > 16384:
                    return "[structured content omitted]"
                try:
                    nested = parse(value.encode())
                except ArtifactError:
                    return "[structured content omitted]"
                return self.text(json.dumps(self._nested(nested)))
            return self.text(value, 16384)
        return value

    def _nested(self, value, depth=0):
        if depth > 16:
            return "[omitted]"
        if isinstance(value, dict):
            return {
                self.text(k, 256): "[redacted]"
                if _SECRET_KEY.search(k)
                else self._nested(v, depth + 1)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self._nested(v, depth + 1) for v in value]
        if isinstance(value, str):
            if value.lstrip().startswith(("{", "[")):
                if len(value) > 16384:
                    return "[structured content omitted]"
                try:
                    return self._nested(parse(value.encode()), depth + 1)
                except ArtifactError:
                    return "[structured content omitted]"
            return self.text(value)
        return value
