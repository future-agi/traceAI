"""What may land on a span. Audio bytes, API keys and resumption handles never do."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional
from urllib.parse import urlsplit

from fi_instrumentation import TraceConfig


@dataclass(frozen=True)
class ContentPolicy:
    """Which content directions are captured.

    ``capture_inputs`` covers the system instruction, user text and input transcription,
    tool arguments and the prompt shown on a model turn. ``capture_outputs`` covers model
    text and output transcription and tool results. The conversation transcript and raw
    error text need both. Metadata (model, voice name, timings, tool names, flags, token
    counts, close codes) is always kept.
    """

    capture_inputs: bool = True
    capture_outputs: bool = True

    @property
    def full(self) -> bool:
        return self.capture_inputs and self.capture_outputs


# The standard traceAI switches (TraceConfig fields, or the FI_HIDE_* environment variables).
# Everything a Live session carries in either direction is message text, so any one of them
# drops that whole direction here.
_INPUT_SWITCHES = ("hide_inputs", "hide_input_messages", "hide_input_text")
_OUTPUT_SWITCHES = ("hide_outputs", "hide_output_messages", "hide_output_text")


def policy_from(config: Optional[TraceConfig]) -> ContentPolicy:
    config = config or TraceConfig()
    return ContentPolicy(
        capture_inputs=not any(bool(getattr(config, n, False)) for n in _INPUT_SWITCHES),
        capture_outputs=not any(bool(getattr(config, n, False)) for n in _OUTPUT_SWITCHES),
    )


def url_path(url: Any) -> Optional[str]:
    """Path only. Scheme, host, port, user info and query string are dropped."""
    if not url:
        return None
    try:
        return urlsplit(str(url)).path or None
    except ValueError:
        return None


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        if name in obj:
            return obj[name]
        camel = "".join(p.capitalize() if i else p for i, p in enumerate(name.split("_")))
        return obj.get(camel, default)
    return getattr(obj, name, default)


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def text_of_parts(parts: Iterable[Any], *, skip_thoughts: bool = True) -> str:
    """Concatenated text parts. Inline data (audio, images) is never read."""
    out = []
    for part in parts or []:
        if isinstance(part, str):
            out.append(part)
            continue
        if skip_thoughts and _get(part, "thought"):
            continue
        text = _get(part, "text")
        if isinstance(text, str) and text:
            out.append(text)
    return "".join(out)


def text_of_turns(turns: Any) -> str:
    """Text of the Content objects (or dicts, or strings) passed to send_client_content."""
    chunks = []
    for item in as_list(turns):
        if isinstance(item, str):
            chunks.append(item)
            continue
        text = text_of_parts(as_list(_get(item, "parts")))
        if text:
            chunks.append(text)
    return "\n".join(chunks)


def roles_of_turns(turns: Any) -> List[str]:
    roles = []
    for item in as_list(turns):
        role = "user" if isinstance(item, str) else _get(item, "role")
        if role:
            roles.append(str(role))
    return roles


def inline_audio(parts: Iterable[Any]) -> List[tuple]:
    """(byte_count, mime_type) for each inline-data part. The bytes themselves are not kept."""
    found = []
    for part in parts or []:
        blob = _get(part, "inline_data")
        if blob is None:
            continue
        data = _get(blob, "data")
        size = len(data) if isinstance(data, (bytes, bytearray)) else 0
        found.append((size, _get(blob, "mime_type") or ""))
    return found


def pcm_rate(mime_type: str) -> Optional[int]:
    """Sample rate from ``audio/pcm;rate=24000``. None when the type is not raw PCM with a rate."""
    if not mime_type or not str(mime_type).lower().startswith("audio/pcm"):
        return None
    for piece in str(mime_type).split(";")[1:]:
        key, _, value = piece.strip().partition("=")
        if key.strip().lower() == "rate":
            try:
                rate = int(value.strip())
            except ValueError:
                return None
            return rate if rate > 0 else None
    return None


def to_json(value: Any) -> str:
    """JSON for tool args and results. Bytes are replaced by their length, never encoded."""

    def scrub(obj: Any) -> Any:
        if isinstance(obj, (bytes, bytearray)):
            return f"<{len(obj)} bytes>"
        if hasattr(obj, "model_dump"):
            try:
                obj = obj.model_dump(exclude_none=True)
            except Exception:
                return str(type(obj).__name__)
        if isinstance(obj, dict):
            return {str(k): scrub(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [scrub(v) for v in obj]
        return obj

    try:
        return json.dumps(scrub(value), ensure_ascii=False, default=str)
    except Exception:
        return "{}"


def seconds(offset: Any) -> Optional[float]:
    """Parse a protobuf Duration string such as ``4.720s``."""
    if offset is None:
        return None
    text = str(offset).strip()
    if text.endswith("s"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None
