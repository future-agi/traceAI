"""Agno typed media through the shared, provider-owned attachment manager.

Observed at the model boundary only (``Model.invoke`` and friends):

- input: ``Message.images`` / ``audio`` / ``files`` of every message (tool
  messages keep their ``tool_call_id``);
- output: ``ModelResponse.images`` / ``audio`` / ``audios`` / ``files`` of a
  non-streaming call.

Only ``content`` held as bytes is captured, and its magic bytes must match an
admitted type. ``filepath`` is never opened, ``url`` goes to the core's
``RemoteUrl`` policy without being fetched, ``external`` provider objects and
media references are ignored. Videos are not captured.

Privacy is the INTERSECTION of the FI TraceConfig and the OpenInference
TraceConfig the instrumentor runs with (either one hiding wins), the manager's
own policy, and the per-call capture policy. The Agno span and a provider
instrumentor's span each get their own occurrence (byte deduplication is
server-side).
"""

from types import MappingProxyType
from typing import Any, Iterator, Mapping, Optional, Sequence

from fi_instrumentation.attachments.media_capture import (
    AUDIO_MIMES,
    DOCUMENT_MIMES,
    EMPTY,
    IMAGE_MIMES,
    MAX_FRAMEWORK_BYTES,
    MAX_FRAMEWORK_PARTS,
    MediaPart,
    Occurrence,
    capture_parts,
    looks_base64,
)

__all__ = ("capture_agno_input", "capture_agno_output", "agno_attachment_capabilities")

agno_attachment_capabilities = MappingProxyType({
    "schema_version": 1,
    "operations": ("model.invoke.input", "model.invoke.output", "model.invoke_stream.input"),
    "sources": ("content_bytes",),
    "unsupported": ("filepath", "remote_url_fetch", "external_file", "media_reference", "video",
                    "streamed_output"),
    "max_part_bytes": MAX_FRAMEWORK_BYTES,
    "max_parts_per_span": MAX_FRAMEWORK_PARTS,
})

_ROLES = ("user", "assistant", "tool", "system")
_AUDIO_FORMATS = {"wav": "audio/wav", "mp3": "audio/mpeg", "mpeg": "audio/mpeg", "ogg": "audio/ogg",
                  "flac": "audio/flac", "webm": "audio/webm"}
_IMAGE_FORMATS = {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg", "webp": "image/webp"}
# Message/ModelResponse field -> MIME family. Order fixes part indexes.
_INPUT_FIELDS = (("images", IMAGE_MIMES), ("audio", AUDIO_MIMES), ("files", DOCUMENT_MIMES))
_OUTPUT_FIELDS = (("images", IMAGE_MIMES), ("audio", AUDIO_MIMES), ("audios", AUDIO_MIMES),
                  ("files", DOCUMENT_MIMES))


def _field(value: Any, name: str) -> Any:
    """A declared model field only: never a property, resolver or ``__getattr__``."""
    try:
        return vars(value).get(name)
    except TypeError:
        return None


def _ordered(mime: Any, family: tuple) -> Optional[tuple]:
    if mime is None:
        return family
    if isinstance(mime, str) and mime in family:
        return (mime,) + tuple(m for m in family if m != mime)
    return None


def _declared(media: Any, family: tuple) -> Optional[tuple]:
    mime = _field(media, "mime_type")
    if mime is None:
        fmt = _field(media, "format")
        if isinstance(fmt, str):
            mime = (_IMAGE_FORMATS if family is IMAGE_MIMES else _AUDIO_FORMATS if family is AUDIO_MIMES
                    else {"pdf": "application/pdf"}).get(fmt.lower(), "unsupported/format")
    return _ordered(mime, family)


def _media_parts(values: Any, family: tuple, purpose: str, message_index: int, start: int, role: str,
                 tool_call_id: Optional[str]) -> Iterator[MediaPart]:
    values = [values] if values is not None and not isinstance(values, (list, tuple)) else list(values or ())
    for offset, media in enumerate(values[:MAX_FRAMEWORK_PARTS]):
        mimes = _declared(media, family)
        if not mimes:
            continue
        content = _field(media, "content")
        url = _field(media, "url")
        index = start + offset
        if isinstance(content, (bytes, bytearray)):
            kind = "inline" if looks_base64(content) else "bytes"
            yield MediaPart(kind, lambda c=content: c, mimes, purpose, message_index, index, role=role,
                            tool_call_id=tool_call_id)
        elif content is None and isinstance(url, str) and _field(media, "filepath") is None:
            yield MediaPart("url", lambda u=url: u, mimes, purpose, message_index, index, role=role,
                            tool_call_id=tool_call_id)
        # filepath, external objects, media references, str content: never read or resolved.


def _input_parts(messages: Sequence[Any]) -> Iterator[MediaPart]:
    for message_index, message in enumerate(list(messages)[:MAX_FRAMEWORK_PARTS]):
        role = _field(message, "role")
        role = role if role in _ROLES else "unknown"
        tool_call_id = _field(message, "tool_call_id") if role == "tool" else None
        tool_call_id = tool_call_id if isinstance(tool_call_id, str) else None
        start = 0
        for name, family in _INPUT_FIELDS:
            values = _field(message, name)
            purpose = ("tool_result" if role == "tool" else "document" if family is DOCUMENT_MIMES
                       else "model_input")
            parts = list(_media_parts(values, family, purpose, message_index, start, role, tool_call_id))
            yield from parts
            start += len(values) if isinstance(values, (list, tuple)) else (1 if values is not None else 0)


def _output_parts(response: Any) -> Iterator[MediaPart]:
    start = 0
    for name, family in _OUTPUT_FIELDS:
        values = _field(response, name)
        yield from _media_parts(values, family, "model_output", 0, start, "assistant", None)
        start += len(values) if isinstance(values, (list, tuple)) else (1 if values is not None else 0)


def capture_agno_input(manager, span, messages: Any, *, trace_configs: Sequence[Any] = (),
                       capture=None) -> Mapping[str, Occurrence]:
    """Capture typed media of the model call's input messages. Never raises."""
    try:
        if not isinstance(messages, (list, tuple)):
            return EMPTY
        return capture_parts(manager, span, "agno", _input_parts(messages), direction="input",
                             trace_configs=trace_configs, capture=capture)
    except Exception:
        return EMPTY


def capture_agno_output(manager, span, response: Any, *, trace_configs: Sequence[Any] = (),
                        capture=None) -> Mapping[str, Occurrence]:
    """Capture generated media of a non-streaming ``ModelResponse``. Never raises."""
    try:
        if response is None:
            return EMPTY
        return capture_parts(manager, span, "agno", _output_parts(response), direction="output",
                             trace_configs=trace_configs, capture=capture)
    except Exception:
        return EMPTY
