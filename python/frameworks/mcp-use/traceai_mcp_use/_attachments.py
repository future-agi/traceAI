"""Tool media of an mcp-use agent run, through the shared attachment manager.

mcp-use 1.7 converts an MCP ``CallToolResult`` into LangChain content blocks
(``agents/adapters/langchain_adapter.py``): ``ImageContent`` becomes
``{"type": "image", "source_type": "base64", "data", "mime_type"}``,
``AudioContent`` an ``"audio"`` block and an ``EmbeddedResource`` blob a
``"file"`` block. Only those typed inline blocks are observed. Text, URLs,
resource URIs and resource links are never fetched or scraped. Each block is
preflighted before its base64 is read; at most 64 blocks and 256 KiB of
decoded bytes per tool result.
"""

from types import MappingProxyType
from typing import Any, Mapping, Optional

__all__ = ("capture_tool_blocks", "mcp_use_attachment_capabilities")

mcp_use_attachment_capabilities = MappingProxyType({
    "schema_version": 1,
    "operations": ("tool.output",),
    "sources": ("inline_base64",),
    "content_types": ("image", "audio", "file"),
    # LangChain stringifies tool content it does not know as a block (audio), so no typed block reaches callbacks.
    "unsupported": ("url", "resource_uri", "resource_link", "local_path", "audio_stringified_by_langchain"),
})

_EMPTY: Mapping[str, Any] = MappingProxyType({})
_EXPECT = {"image": "image", "audio": "audio", "file": None}


def _get(value: Any, key: str) -> Any:
    try:
        return value.get(key) if isinstance(value, Mapping) else None
    except Exception:
        return None


def capture_tool_blocks(manager, span, content: Any, *, trace_config=None, capture=None,
                        tool_call_id: Optional[str] = None, error: bool = False) -> Mapping[str, Any]:
    """Capture the typed base64 blocks of one tool result. Never raises; returns admitted positions."""
    try:
        from fi_instrumentation.attachments.media_capture import active
        from fi_instrumentation.attachments.tool_media import MAX_TOOL_PARTS, ToolMedia
    except ImportError:
        return _EMPTY
    try:
        if not active(manager) or not isinstance(content, (list, tuple)):
            return _EMPTY
        media = ToolMedia(manager, span, "mcp_use", trace_config=trace_config, capture=capture)
        for index, block in enumerate(content[:MAX_TOOL_PARTS]):
            if not media.usable:
                break
            kind = _get(block, "type")
            if kind not in _EXPECT:
                continue
            # LangChain's v0 shape (source_type + data) and its v1 standard shape (base64).
            if _get(block, "source_type") == "base64":
                field = "data"
            elif _get(block, "base64") is not None:
                field = "base64"
            else:
                continue  # url / id sources are never fetched
            media.base64("tool/%d" % index, lambda b=block, f=field: _get(b, f), _get(block, "mime_type"),
                         purpose="tool_result", expect=_EXPECT[kind], role="tool", tool_call_id=tool_call_id,
                         part_index=index, error=error)
        captured = media.captured
        return MappingProxyType(captured) if captured else _EMPTY
    except Exception:
        return _EMPTY
