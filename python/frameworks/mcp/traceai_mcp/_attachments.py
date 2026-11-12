"""MCP tool and resource media through the shared, provider-owned attachment manager.

Mirrors ``@traceai/mcp`` (``wrapMCPClient`` / ``captureMCPResult``). Only typed
inline bytes are observed: ``ImageContent.data``, ``AudioContent.data`` and an
``EmbeddedResource`` (or ``resources/read``) ``BlobResourceContents.blob``.
Resource URIs, ``resource_link`` blocks, URLs, local paths and
``structuredContent`` are never fetched, opened or scraped. Each part is
preflighted before its base64 is read or decoded; at most 64 parts and
256 KiB of decoded bytes per result.

This module imports ``fi_instrumentation.attachments`` lazily, so the
transport-only ``MCPInstrumentor`` keeps working on a core without it.
"""

import functools
import inspect
from types import MappingProxyType
from typing import Any, Mapping, Optional

from opentelemetry import context as otel_context
from opentelemetry import trace as trace_api
from opentelemetry.trace import SpanKind, Status, StatusCode
from wrapt import ObjectProxy

from traceai_mcp.version import __version__

__all__ = ("capture_mcp_result", "wrap_mcp_client", "mcp_attachment_capabilities")

#: Declarative handoff for auto and reconciliation.
mcp_attachment_capabilities = MappingProxyType({
    "schema_version": 1,
    "operations": ("tools/call.output", "resources/read.output"),
    "sources": ("inline_base64",),
    "content_types": ("image", "audio", "resource.blob"),
    "unsupported": ("resource_link", "resource_uri", "remote_url", "local_path", "structured_content",
                    "transport_only_instrumentor"),
    "max_parts": 64,
    "max_decoded_bytes_per_result": 256 * 1024,
})

_EMPTY: Mapping[str, Any] = MappingProxyType({})
_OPERATIONS = ("tools/call", "resources/read")


def _get(value: Any, *names: str) -> Any:
    """A field of an MCP model (1.x camelCase or 2.x snake_case) or a plain dict; never raises."""
    for name in names:
        try:
            found = value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)
        except Exception:
            found = None
        if found is not None:
            return found
    return None


def _active(manager) -> bool:
    try:
        from fi_instrumentation.attachments.media_capture import active
    except ImportError:
        return False
    return active(manager)


def capture_mcp_result(manager, span, result: Any, operation: str = "tools/call", capture=None,
                       trace_config=None) -> Mapping[str, Any]:
    """Capture typed inline media of one MCP ``tools/call`` or ``resources/read`` result.

    ``span`` is the span that represents this call. Tool media is tool output
    and potential model input, so input and output hide flags both apply.
    Returns the admitted positions (``"<operation>/<index>"`` -> occurrence);
    empty when no manager, disabled, unsampled or denied. Never raises and
    never changes ``result``.
    """
    if operation not in _OPERATIONS or not _active(manager):
        return _EMPTY
    try:
        from fi_instrumentation.attachments.tool_media import MAX_TOOL_PARTS, ToolMedia
    except ImportError:
        return _EMPTY
    try:
        media = ToolMedia(manager, span, "mcp", trace_config=trace_config, capture=capture)
        if not media.usable:
            return _EMPTY
        error = _get(result, "isError", "is_error") is True
        parts = _get(result, "content" if operation == "tools/call" else "contents")
        if not isinstance(parts, (list, tuple)):
            return _EMPTY
        for index, part in enumerate(parts[:MAX_TOOL_PARTS]):
            if not media.usable:
                break
            kind = _get(part, "type")
            if operation == "resources/read":
                holder, field, expect = part, "blob", None
            elif kind in ("image", "audio"):
                holder, field, expect = part, "data", kind
            elif kind == "resource":
                holder, field, expect = _get(part, "resource"), "blob", None
            else:
                continue  # text, resource_link and unknown blocks are never dereferenced
            if holder is None:
                continue
            mime = _get(holder, "mimeType", "mime_type")
            media.base64("%s/%d" % (operation, index), lambda h=holder, f=field: _get(h, f), mime,
                         purpose="tool_result", expect=expect, role="tool", part_index=index, error=error)
        captured = media.captured
        return MappingProxyType(captured) if captured else _EMPTY
    except Exception:
        return _EMPTY  # Malformed or untrusted results must not change the tool response.


def _provider_manager(tracer_provider) -> Any:
    owner = tracer_provider if tracer_provider is not None else trace_api.get_tracer_provider()
    manager = getattr(owner, "attachment_manager", None)
    if manager is None:
        delegate = getattr(owner, "_real_tracer_provider", None) or getattr(owner, "_tracer_provider", None)
        manager = getattr(delegate, "attachment_manager", None)
    return manager


class _TracedSession(ObjectProxy):  # type: ignore[misc]
    """Proxies an MCP ``ClientSession``; only ``call_tool`` and ``read_resource`` gain a span."""

    def __init__(self, wrapped, config):
        super().__init__(wrapped)
        self._self_config = config

    def _self_traced(self, operation: str, method, args, kwargs):
        tracer_provider, attachments, trace_config, capture = self._self_config

        @functools.wraps(method)
        async def run():
            span = None
            manager = None
            try:
                manager = attachments if attachments is not None else _provider_manager(tracer_provider)
                provider = tracer_provider if tracer_provider is not None else trace_api.get_tracer_provider()
                tracer = provider.get_tracer("traceai_mcp", __version__)
                span = tracer.start_span("mcp.%s" % operation, kind=SpanKind.CLIENT,
                                         attributes={"gen_ai.span.kind": "TOOL", "mcp.method.name": operation})
            except Exception:
                span = None  # Tracing never prevents the SDK call.
            if span is None:
                return await method(*args, **kwargs)
            token = otel_context.attach(trace_api.set_span_in_context(span))
            try:
                result = await method(*args, **kwargs)
            except BaseException:
                otel_context.detach(token)
                try:
                    span.set_status(Status(StatusCode.ERROR))
                finally:
                    span.end()
                raise
            otel_context.detach(token)
            try:
                if manager is not None:
                    capture_mcp_result(manager, span, result, operation, capture, trace_config)
                failed = _get(result, "isError", "is_error") is True
                span.set_status(Status(StatusCode.ERROR if failed else StatusCode.OK))
            except Exception:
                pass  # Never log SDK results or exceptions.
            finally:
                span.end()
            return result

        return run()

    def call_tool(self, *args, **kwargs):
        method = self.__wrapped__.call_tool
        if not inspect.iscoroutinefunction(method):
            return method(*args, **kwargs)
        return self._self_traced("tools/call", method, args, kwargs)

    def read_resource(self, *args, **kwargs):
        method = self.__wrapped__.read_resource
        if not inspect.iscoroutinefunction(method):
            return method(*args, **kwargs)
        return self._self_traced("resources/read", method, args, kwargs)


def wrap_mcp_client(session, *, tracer_provider=None, attachments=None, trace_config=None,
                    attachment_capture=None):
    """Trace a ``mcp.ClientSession``'s ``call_tool`` and ``read_resource`` and capture their media.

    ``attachments`` defaults to the provider's shared manager
    (``tracer_provider.attachment_manager``, as ``fi_instrumentation.register``
    and ``traceai.init`` set it); no uploader is created here. ``trace_config``
    and ``attachment_capture`` can only add restrictions. The SDK result,
    exception and cancellation are returned unchanged. Wrapping a traced session
    again returns it unchanged, so spans never nest and media is never uploaded
    twice.
    """
    # A traced session is returned as is, so spans never nest and media is never captured twice.
    # (ObjectProxy delegates ==/hash to the session, so identity caches would see them as equal.)
    if isinstance(session, _TracedSession):
        return session
    proxy = _TracedSession(session, (tracer_provider, attachments, trace_config, attachment_capture))
    return proxy
