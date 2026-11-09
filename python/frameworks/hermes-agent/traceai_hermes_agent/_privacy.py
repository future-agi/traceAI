"""Content-boundary privacy for the Node-free Hermes observer."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

_REDACTED = "[REDACTED]"
_MAX_CONTENT = 1024 * 1024
_CREDENTIAL = re.compile(r"\b(?:sk-|ghp_|github_pat_)[a-zA-Z0-9_-]{8,}")
_SENSITIVE = re.compile(r"secret|token|password|passwd|apikey|privatekey|accesskey|credential|authorization|cookie|signingkey|passphrase|^pwd$", re.I)
_SAFE_KEYS = {"inputtokens", "outputtokens", "totaltokens", "tokencount", "tokenizer", "publickey", "nonsecretkey"}


def redact(value: Any, secrets: tuple = (), depth: int = 0) -> Any:
    """Redact before display truncation, including JSON-encoded tool arguments.

    Known exporter credentials are replaced in both raw and decoded text; a
    recognizable credential must not require an assignment/key to be private.
    """
    if depth > 16:
        return "[REDACTED_DEPTH_LIMIT]"
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            sensitive = normalized not in _SAFE_KEYS and _SENSITIVE.search(normalized)
            result[key] = _REDACTED if sensitive else redact(item, secrets, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets, depth + 1) for item in value]
    if not isinstance(value, str):
        return value
    if len(value) > _MAX_CONTENT:
        return "[REDACTED_SIZE_LIMIT]"
    if value.lstrip().startswith(("{", "[", '"')):
        try:
            return json.dumps(redact(json.loads(value), secrets, depth + 1), ensure_ascii=False)
        except (ValueError, RecursionError):
            pass
    for secret in secrets:
        value = value.replace(secret, _REDACTED)
    return _CREDENTIAL.sub(_REDACTED, value)


def private_history(value: Any, *, hide_outputs: bool, hide_errors: bool, depth: int = 0) -> Any:
    """Apply output/error policy to history before it becomes generation input.

    Hermes can expose chat messages, Responses items or nested tool-result
    blocks. Never mutate the host's request. Error-bearing JSON tool results
    are inspected too, not just their outer message envelope.
    """
    if depth > 16:
        return None
    if isinstance(value, list):
        return [clean for item in value if (clean := private_history(
            item, hide_outputs=hide_outputs, hide_errors=hide_errors, depth=depth + 1)) is not None]
    if not isinstance(value, Mapping):
        return value
    if hide_outputs and (value.get("role") in {"assistant", "tool", "function"}
                         or value.get("type") in {"function_call", "function_call_output", "tool_result", "tool_use"}):
        return None
    if hide_errors and (value.get("isError") or value.get("is_error") or value.get("error")
                        or value.get("status") in {"error", "failed", "blocked", "timeout", "cancelled"}):
        return None
    result = dict(value)
    for key in ("content", "output"):
        content = result.get(key)
        if hide_errors and isinstance(content, str) and content.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(content)
                clean = private_history(decoded, hide_outputs=hide_outputs, hide_errors=hide_errors, depth=depth + 1)
                if clean is None:
                    return None
                if clean != decoded:
                    result[key] = json.dumps(clean, ensure_ascii=False)
            except (ValueError, RecursionError):
                pass
        elif isinstance(content, (list, Mapping)):
            result[key] = private_history(content, hide_outputs=hide_outputs, hide_errors=hide_errors, depth=depth + 1)
    return result
