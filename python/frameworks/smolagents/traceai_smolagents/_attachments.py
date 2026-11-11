"""smolagents typed media observations, through the shared attachment manager.

Observed, and nothing else:

- images smolagents hands to a model: ``{"type": "image", "image": <PIL image>}``
  content blocks (task images and ``ActionStep.observations_images``, such as
  a browser screenshot a step callback stored), on the model span as model
  input, before the model converts them;
- a tool's returned image: an in-memory ``PIL.Image.Image`` or an
  ``AgentImage`` whose image is already in memory, on the tool span.

Images are PNG-encoded only after a pixel-bounded preflight admitted them.
A file-backed ``AgentImage`` or PIL image that is not loaded yet, image URLs,
``image_url`` strings and every ``AgentAudio`` (a path or a tensor) are not
read (``source_unavailable``). smolagents replays memory images on every
model call; the same image object is uploaded once per manager.
"""

import threading
import weakref
from collections import OrderedDict
from types import MappingProxyType
from typing import Any, Mapping

__all__ = ("capture_model_images", "capture_tool_image", "smolagents_attachment_capabilities")

smolagents_attachment_capabilities = MappingProxyType({
    "schema_version": 1,
    "operations": ("model.input.image", "tool.output.image"),
    "sources": ("in_memory_pil_image",),
    "unsupported": ("agent_audio", "file_backed_image", "image_url", "tensor"),
})

_EMPTY: Mapping[str, Any] = MappingProxyType({})
_MAX_SEEN = 1024
_MAX_MESSAGES = 256


class _Seen:
    """Image objects already uploaded, per manager (identity only; PIL images are unhashable)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._managers: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

    def __contains__(self, item) -> bool:
        manager, image = item
        with self._lock:
            entry = self._managers.get(manager, {}).get(id(image))
            return entry is not None and entry() is image

    def add(self, manager, image) -> None:
        with self._lock:
            seen = self._managers.get(manager)
            if seen is None:
                seen = self._managers[manager] = OrderedDict()
            seen[id(image)] = weakref.ref(image)
            while len(seen) > _MAX_SEEN:
                seen.popitem(last=False)


_SEEN = _Seen()


def _get(value: Any, key: str) -> Any:
    try:
        return value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)
    except Exception:
        return None


def _in_memory(value: Any) -> Any:
    """The PIL image an ``AgentImage`` already holds in memory, else the value itself."""
    if type(value).__name__ == "AgentImage":
        raw = getattr(value, "_raw", None)
        return raw  # None for path- or tensor-backed values: never loaded here
    return value


def capture_model_images(manager, span, messages: Any, *, trace_config=None, capture=None) -> int:
    """Capture the in-memory images of one model call's input messages. Returns the admitted count."""
    try:
        from fi_instrumentation.attachments.media_capture import active
        from fi_instrumentation.attachments.tool_media import MAX_TOOL_PARTS, ToolMedia
    except ImportError:
        return 0
    admitted = 0
    try:
        if not active(manager) or not isinstance(messages, (list, tuple)):
            return 0
        media = ToolMedia(manager, span, "smolagents", trace_config=trace_config, capture=capture,
                          budget=8 * 1024 * 1024, direction="input")
        for message_index, message in enumerate(messages[:_MAX_MESSAGES]):
            content = _get(message, "content")
            if not isinstance(content, list):
                continue
            for part_index, part in enumerate(content[:MAX_TOOL_PARTS]):
                if not media.usable:
                    return admitted
                if _get(part, "type") != "image":
                    continue
                image = _in_memory(_get(part, "image"))
                if image is None or (manager, image) in _SEEN:
                    continue
                found = media.image("model/%d/%d" % (message_index, part_index), image, purpose="model_input",
                                    role="user", message_index=message_index, part_index=part_index)
                if found is not None:
                    _SEEN.add(manager, image)
                    admitted += 1
    except Exception:
        return admitted
    return admitted


def capture_tool_image(manager, span, response: Any, *, trace_config=None, capture=None) -> int:
    """Capture a tool's returned in-memory image on its span. Returns 1 when admitted."""
    try:
        from fi_instrumentation.attachments.media_capture import active
        from fi_instrumentation.attachments.tool_media import ToolMedia
    except ImportError:
        return 0
    try:
        if not active(manager):
            return 0
        image = _in_memory(response)
        if image is None or (manager, image) in _SEEN:
            return 0
        media = ToolMedia(manager, span, "smolagents", trace_config=trace_config, capture=capture,
                          budget=8 * 1024 * 1024)
        found = media.image("tool/0", image, purpose="tool_result", role="tool", part_index=0)
        if found is None:
            return 0
        _SEEN.add(manager, image)
        return 1
    except Exception:
        return 0
