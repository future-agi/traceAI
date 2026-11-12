"""Firecrawl returned screenshots and media, through the shared attachment manager.

A firecrawl-py v2 ``Document`` carries ``screenshot``, ``audio`` and ``video``
strings, and browser ``actions`` may carry ``screenshots``. Only values that
are inline ``data:<mime>;base64,`` URIs are uploaded, at the operation span,
as tool results. Firecrawl Cloud returns hosted URLs for these: they are never
fetched (the SDK core has no approved remote retrieval yet), so they are
``source_unavailable``. Nothing here starts a crawl, a browser or a request.
"""

from types import MappingProxyType
from typing import Any, Mapping

__all__ = ("capture_firecrawl_media", "firecrawl_attachment_capabilities")

#: Operations whose results carry Documents.
MEDIA_METHODS = frozenset((
    "scrape", "crawl", "batch_scrape", "wait_batch_scrape", "get_crawl_status", "get_batch_scrape_status",
))
MAX_DOCUMENTS = 16
MAX_RESULT_BYTES = 8 * 1024 * 1024
_IMAGE = ("image/png", "image/jpeg", "image/webp")
_AUDIO = ("audio/mpeg", "audio/wav", "audio/ogg", "audio/webm", "audio/mp4", "audio/flac")
_VIDEO = ("video/mp4", "video/webm")
_EMPTY: Mapping[str, Any] = MappingProxyType({})

firecrawl_attachment_capabilities = MappingProxyType({
    "schema_version": 1,
    "operations": tuple(sorted(MEDIA_METHODS)),
    "sources": ("inline_data_uri",),
    "fields": ("screenshot", "actions.screenshots", "audio", "video"),
    "unsupported": ("hosted_url", "remote_url", "images_links", "pdf_url"),
    "max_documents": MAX_DOCUMENTS,
    "max_decoded_bytes_per_result": MAX_RESULT_BYTES,
})


def _get(value: Any, key: str) -> Any:
    try:
        return value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)
    except Exception:
        return None


def _documents(method: str, result: Any) -> list:
    if method == "scrape":
        return [result]
    data = _get(result, "data")
    return list(data[:MAX_DOCUMENTS]) if isinstance(data, (list, tuple)) else []


def capture_firecrawl_media(manager, span, method: str, result: Any, *, trace_config=None,
                            capture=None) -> Mapping[str, Any]:
    """Capture inline media of one operation's returned Documents. Never raises."""
    if method not in MEDIA_METHODS:
        return _EMPTY
    try:
        from fi_instrumentation.attachments.media_capture import active
        from fi_instrumentation.attachments.tool_media import ToolMedia
    except ImportError:
        return _EMPTY
    try:
        if not active(manager):
            return _EMPTY
        media = ToolMedia(manager, span, "firecrawl", trace_config=trace_config, capture=capture,
                          budget=MAX_RESULT_BYTES)
        for index, document in enumerate(_documents(method, result)[:MAX_DOCUMENTS]):
            values = [("screenshot", _get(document, "screenshot"), _IMAGE),
                      ("audio", _get(document, "audio"), _AUDIO),
                      ("video", _get(document, "video"), _VIDEO)]
            shots = _get(_get(document, "actions"), "screenshots")
            if isinstance(shots, (list, tuple)):
                values.extend(("actions.screenshots.%d" % n, shot, _IMAGE) for n, shot in enumerate(shots[:16]))
            for field, value, mimes in values:
                if not media.usable:
                    break
                if not isinstance(value, str) or not value.startswith("data:"):
                    continue  # hosted URLs are never fetched
                media.data_uri("document/%d/%s" % (index, field), lambda v=value: v, mimes,
                               purpose="tool_result", role="tool", message_index=index)
        captured = media.captured
        return MappingProxyType(captured) if captured else _EMPTY
    except Exception:
        return _EMPTY
