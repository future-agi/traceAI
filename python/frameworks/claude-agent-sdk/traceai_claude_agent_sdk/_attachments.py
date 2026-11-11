"""Tool-result media of a Claude Agent SDK conversation, through the shared attachment manager.

The SDK parses each CLI ``user`` message into a ``UserMessage`` whose
``ToolResultBlock.content`` keeps the original Anthropic content blocks. The
base64 ``image`` and PDF ``document`` blocks there are uploaded BEFORE any text
flattening or truncation, at the tool's span (with ``tool_call_id``):

- while the tool span is still open, on that span (and projected on it);
- after the PostToolUse hook ended it, on its exact span context (the
  occurrence keeps the causal parent; the ended span is never mutated);
- for a result with no traced tool, on the conversation span.

URL and file sources, paths and transcripts are never read. The SDK drops
top-level user image blocks during parsing, so prompt images are not
observable here (``source_unavailable``). Model calls run inside the Claude
Code process, so no in-process provider adapter sees the same bytes.
"""

from collections import OrderedDict
from types import MappingProxyType
from typing import Any, Mapping, Optional

__all__ = ("capture_tool_results", "claude_agent_attachment_capabilities", "remember_ended_tool",
           "media_safe")

claude_agent_attachment_capabilities = MappingProxyType({
    "schema_version": 1,
    "operations": ("tool_result.output",),
    "sources": ("inline_base64",),
    "content_types": ("image", "document"),
    "unsupported": ("url_source", "file_source", "prompt_images", "transcript", "local_path"),
})

_EMPTY: Mapping[str, Any] = MappingProxyType({})
_MEDIA_TYPES = ("image", "document", "audio")
_MAX_ENDED = 256
_ended: "OrderedDict[str, Any]" = OrderedDict()


def remember_ended_tool(tool_use_id: Any, span: Any) -> None:
    """Keep the exact span context of a tool span the PostToolUse hook ended (bounded)."""
    try:
        if not isinstance(tool_use_id, str):
            return
        _ended[tool_use_id] = span.get_span_context()
        _ended.move_to_end(tool_use_id)
        while len(_ended) > _MAX_ENDED:
            _ended.popitem(last=False)
    except Exception:
        return


def forget_ended_tools() -> None:
    _ended.clear()


def _get(value: Any, key: str) -> Any:
    try:
        return value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)
    except Exception:
        return None


def capture_tool_results(manager, message: Any, conversation_span: Any, active_spans: Mapping[str, Any], *,
                         message_index: int, trace_config=None, capture=None) -> int:
    """Capture the media blocks of every ``ToolResultBlock`` in one ``UserMessage``. Never raises.

    ``active_spans`` maps a tool_use_id to its still-open span (the hooks' table).
    Returns the number of admitted blocks.
    """
    try:
        from fi_instrumentation.attachments.media_capture import active
        from fi_instrumentation.attachments.tool_media import MAX_TOOL_PARTS, ToolMedia
    except ImportError:
        return 0
    admitted = 0
    try:
        if not active(manager):
            return 0
        content = _get(message, "content")
        if not isinstance(content, list):
            return 0
        for block in content[:MAX_TOOL_PARTS]:
            if type(block).__name__ != "ToolResultBlock":
                continue
            blocks = _get(block, "content")
            if not isinstance(blocks, list):
                continue
            tool_use_id = _get(block, "tool_use_id")
            open_entry = active_spans.get(tool_use_id) if isinstance(tool_use_id, str) else None
            if open_entry is not None:
                target = open_entry[0]
            elif isinstance(tool_use_id, str) and tool_use_id in _ended:
                target = _ended[tool_use_id]
            else:
                target = conversation_span
            error = _get(block, "is_error") is True
            media = ToolMedia(manager, target, "claude_agent_sdk", trace_config=trace_config, capture=capture)
            for part_index, part in enumerate(blocks[:MAX_TOOL_PARTS]):
                if not media.usable:
                    break
                kind = _get(part, "type")
                if kind not in ("image", "document"):
                    continue
                source = _get(part, "source")
                if _get(source, "type") != "base64":
                    continue  # url / file sources are never fetched
                mime = _get(source, "media_type")
                expect = "image" if kind == "image" else "document"
                found = media.base64("tool_result/%s/%d" % (tool_use_id, part_index),
                                     lambda s=source: _get(s, "data"), mime, purpose="tool_result",
                                     expect=expect, role="tool", tool_call_id=tool_use_id,
                                     message_index=min(message_index, 65535), part_index=part_index,
                                     error=error)
                admitted += found is not None
    except Exception:
        return admitted
    return admitted


def media_safe(value: Any, depth: int = 0) -> Any:
    """A telemetry copy without inline media bytes: base64 fields of media blocks become ``"[omitted]"``.

    Used before tool responses are JSON-serialized and truncated, so bytes never
    reach span attributes as text. Non-media values are returned unchanged.
    """
    if depth > 6:
        return value
    if isinstance(value, list):
        return [media_safe(item, depth + 1) for item in value]
    if not isinstance(value, Mapping):
        return value
    media = value.get("type") in _MEDIA_TYPES
    result = {}
    for key, item in value.items():
        if media and key in ("data", "base64", "blob") and isinstance(item, str):
            result[key] = "[omitted]"
        elif media and key in ("source", "file") and isinstance(item, Mapping):
            result[key] = {k: ("[omitted]" if k in ("data", "base64") and isinstance(v, str) else v)
                           for k, v in item.items()}
        else:
            result[key] = media_safe(item, depth + 1)
    return result
