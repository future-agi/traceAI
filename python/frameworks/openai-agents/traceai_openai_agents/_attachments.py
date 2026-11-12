"""Tool media of OpenAI Agents SDK function spans, through the shared attachment manager.

The SDK records what a tool returned on its ``FunctionSpanData.output`` (only
when ``RunConfig.trace_include_sensitive_data`` is on). These typed outputs
are observed, at the tool span, before ``OUTPUT_VALUE`` is written:

- the computer tool's screenshot: a ``data:image/png;base64,`` string on the
  ``computer`` function span (SDK 0.10.0 and later record it there);
- ``ToolOutputImage`` / ``{"type": "image", "image_url": <data URI>}``
  (SDK 0.4.0+; MCP image results become these from 0.8.0);
- ``ToolOutputFileContent`` / ``{"type": "file", "file_data": <data URI>}``;
- a list of those.

Remote URLs, ``file_id`` values and plain strings on other tools are never
fetched or scraped. Model-call input media (``ResponseSpanData.input``) is
left to the OpenAI provider adapter, which owns that span. Admitted values
become ``fi-attachment:<occurrence_id>`` in the span's ``OUTPUT_VALUE``; the
SDK's own objects are never changed.
"""

from types import MappingProxyType
from typing import Any, Mapping, Tuple

__all__ = ("capture_function_output", "openai_agents_attachment_capabilities")

COMPUTER_TOOL_NAMES = ("computer", "computer_use_preview")
_IMAGE_MIMES = ("image/png", "image/jpeg", "image/webp")
_FILE_MIMES = ("application/pdf", "application/octet-stream")
_EMPTY: Mapping[str, Any] = MappingProxyType({})

openai_agents_attachment_capabilities = MappingProxyType({
    "schema_version": 1,
    "operations": ("function.output", "computer.screenshot"),
    "sources": ("inline_data_uri",),
    "minimum_sdk": {"computer.screenshot": "0.10.0", "tool_output_image": "0.4.0", "mcp_image": "0.8.0"},
    "unsupported": ("remote_url", "file_id", "response_input", "trace_include_sensitive_data_off"),
})


def _field(value: Any, key: str) -> Any:
    try:
        if isinstance(value, Mapping):
            return value.get(key)
        return getattr(value, key, None)
    except Exception:
        return None


def _typed(value: Any) -> bool:
    return isinstance(value, Mapping) or type(value).__name__ in ("ToolOutputImage", "ToolOutputFileContent")


def capture_function_output(manager, span, name: Any, output: Any, *, trace_config=None,
                            capture=None) -> Tuple[Mapping[str, Any], Any]:
    """Capture one function span's typed media; returns (admitted positions, telemetry output).

    The telemetry output is ``output`` itself when nothing was admitted, else a
    copy where only the admitted values are ``fi-attachment:`` URIs. Never raises.
    """
    try:
        from fi_instrumentation.attachments.media_capture import active
        from fi_instrumentation.attachments.tool_media import MAX_TOOL_PARTS, ToolMedia, uri
    except ImportError:
        return _EMPTY, output
    try:
        if not active(manager) or output is None:
            return _EMPTY, output
        media = ToolMedia(manager, span, "openai_agents", trace_config=trace_config, capture=capture)
        if not media.usable:
            return _EMPTY, output

        def item(value, index):
            position = "function/%d" % index
            if isinstance(value, str):
                if name in COMPUTER_TOOL_NAMES and value.startswith("data:image/"):
                    found = media.data_uri(position, lambda: value, _IMAGE_MIMES, purpose="computer_screenshot",
                                           role="tool", part_index=index)
                    return uri(found) if found else value
                return value
            if not _typed(value):
                return value
            kind = _field(value, "type")
            if kind == "image":
                key, mimes = "image_url", _IMAGE_MIMES
            elif kind == "file":
                key, mimes = "file_data", _FILE_MIMES
            else:
                return value
            encoded = _field(value, key)
            if not isinstance(encoded, str) or not encoded.startswith("data:"):
                return value  # URLs and file ids are never fetched
            found = media.data_uri(position, lambda: encoded, mimes, purpose="tool_result", role="tool",
                                   part_index=index)
            if not found:
                return value
            plain = dict(value) if isinstance(value, Mapping) else value.model_dump(exclude_none=True)
            plain[key] = uri(found)
            return plain

        if isinstance(output, list):
            projected = [item(value, index) if index < MAX_TOOL_PARTS else value
                         for index, value in enumerate(output)]
        else:
            projected = item(output, 0)
        captured = media.captured
        if not captured:
            return _EMPTY, output
        return MappingProxyType(captured), projected
    except Exception:
        return _EMPTY, output
