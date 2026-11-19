"""Content-type aware payload capture and best-effort result extraction.

Everything here is pure and defensive: a payload shape we do not recognise yields ``None``,
never an exception that could reach the caller of ``invoke_endpoint``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

# Longest text recorded for one payload. JSON is only parsed below the parse limit.
TEXT_LIMIT = 32768
PARSE_LIMIT = 1 << 20

JSON = "json"
TEXT = "text"
BINARY = "binary"

_JSON_TYPES = {"application/json", "application/jsonlines", "application/jsonl", "application/x-ndjson", "application/x-jsonlines"}
_TEXT_TYPES = {"application/xml", "application/x-yaml", "application/yaml", "application/x-www-form-urlencoded"}

_REQUEST_PARAMETER_KEYS = (
    "max_tokens", "max_new_tokens", "max_completion_tokens", "temperature", "top_p", "top_k", "n",
    "stop", "seed", "frequency_penalty", "presence_penalty", "repetition_penalty",
)


def base_type(content_type: Any) -> str:
    if not isinstance(content_type, str):
        return ""
    return content_type.split(";", 1)[0].strip().lower()


def classify(content_type: Any) -> str:
    """JSON or text only when the ContentType says so; everything else, including none, is binary."""
    base = base_type(content_type)
    if not base:
        return BINARY
    if base in _JSON_TYPES or base.endswith("+json"):
        return JSON
    if base.startswith("text/") or base in _TEXT_TYPES or base.endswith("+xml"):
        return TEXT
    return BINARY


def binary_note(content_type: Any, size: Optional[int] = None) -> str:
    kind = base_type(content_type) or "unknown content type"
    if size is None:
        return f"[binary content: {kind}]"
    return f"[binary content: {kind}, {size} bytes]"


def decode(data: Any) -> Optional[str]:
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8", errors="replace")
    return None


def clip(text: str) -> str:
    return text[:TEXT_LIMIT]


@dataclass
class Captured:
    """What to record for one request or response body."""

    value: str
    mime_type: str
    parsed: Any = None


def capture_body(data: Any, content_type: Any) -> Optional[Captured]:
    """Describe a bytes/str body. ``None`` for a body we cannot describe (file-like, other)."""
    kind = classify(content_type)
    if isinstance(data, (bytes, bytearray, str)):
        size = len(data)
        if kind == BINARY:
            return Captured(binary_note(content_type, size), "text/plain")
        text = decode(data) or ""
        parsed = None
        if kind == JSON and size <= PARSE_LIMIT:
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
        return Captured(clip(text), "application/json" if parsed is not None else "text/plain", parsed)
    if kind == BINARY:
        return Captured(binary_note(content_type), "text/plain")
    return Captured("[streaming body not captured]", "text/plain")


def request_parameters(parsed: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(parsed, dict):
        return None
    nested = parsed.get("parameters")
    if isinstance(nested, dict) and nested:
        return nested
    found = {key: parsed[key] for key in _REQUEST_PARAMETER_KEYS if key in parsed and parsed[key] is not None}
    return found or None


def _count(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _first(mapping: Dict[str, Any], *names: str) -> Optional[int]:
    for name in names:
        number = _count(mapping.get(name))
        if number is not None:
            return number
    return None


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _head(value: Any) -> Dict[str, Any]:
    if isinstance(value, list) and value:
        return _dict(value[0])
    return {}


def _str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


@dataclass
class Usage:
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    model: Optional[str] = None

    def merge(self, other: "Usage") -> None:
        for name in ("input_tokens", "output_tokens", "total_tokens", "model"):
            value = getattr(other, name)
            if value is not None:
                setattr(self, name, value)

    def complete(self) -> None:
        if self.total_tokens is None and self.input_tokens is not None and self.output_tokens is not None:
            self.total_tokens = self.input_tokens + self.output_tokens


def usage_of(obj: Any) -> Usage:
    """Token counts from the common response shapes; unknown shapes give an empty Usage."""
    usage = Usage()
    if isinstance(obj, list):
        obj = _head(obj)
    if not isinstance(obj, dict):
        return usage
    model = _str(obj.get("model"))
    if model:
        usage.model = model
    counts = _dict(obj.get("usage"))
    # OpenAI-compatible, Anthropic-style and Bedrock Converse-style usage objects.
    usage.input_tokens = _first(counts, "prompt_tokens", "input_tokens", "inputTokens")
    usage.output_tokens = _first(counts, "completion_tokens", "output_tokens", "outputTokens")
    usage.total_tokens = _first(counts, "total_tokens", "totalTokens")
    # Llama/Mistral/Titan-on-Bedrock style top-level counts.
    if usage.input_tokens is None:
        usage.input_tokens = _first(obj, "prompt_token_count", "inputTextTokenCount")
    if usage.output_tokens is None:
        usage.output_tokens = _first(obj, "generation_token_count") or _first(_head(obj.get("results")), "tokenCount")
    # Hugging Face TGI details.
    details = _dict(obj.get("details"))
    if usage.output_tokens is None:
        usage.output_tokens = _first(details, "generated_tokens")
    prefill = details.get("prefill")
    if usage.input_tokens is None and isinstance(prefill, list) and prefill:
        usage.input_tokens = len(prefill)
    usage.complete()
    return usage


def _join_text_blocks(blocks: Any) -> Optional[str]:
    if not isinstance(blocks, list):
        return None
    parts = [b["text"] for b in blocks if isinstance(b, dict) and isinstance(b.get("text"), str)]
    return "".join(parts) or None


def _choice_text(choices: Any) -> Optional[str]:
    first = _head(choices)
    message = _dict(first.get("message"))
    for candidate in (message.get("content"), first.get("text"), _dict(first.get("delta")).get("content")):
        if isinstance(candidate, str) and candidate:
            return candidate
        joined = _join_text_blocks(candidate)
        if joined:
            return joined
    return None


def text_of(obj: Any) -> Optional[str]:
    """The generated text from the common response shapes, or ``None``."""
    if isinstance(obj, list):
        obj = _head(obj)
    if not isinstance(obj, dict):
        return None
    text = _str(obj.get("generated_text"))
    if text:
        return text
    text = _choice_text(obj.get("choices"))
    if text:
        return text
    for key in ("generation", "completion", "output_text", "text"):
        text = _str(obj.get(key))
        if text:
            return text
    outputs = obj.get("outputs")
    if isinstance(outputs, list) and outputs:
        first = outputs[0]
        text = _str(first) or _str(_dict(first).get("text"))
        if text:
            return text
    text = _join_text_blocks(obj.get("content")) or _str(obj.get("content"))
    if text:
        return text
    generated = obj.get("generated_texts")
    if isinstance(generated, list) and generated:
        text = _str(generated[0])
        if text:
            return text
    return _str(_head(obj.get("results")).get("outputText"))
