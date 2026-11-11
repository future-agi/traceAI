"""LlamaIndex typed message media through the shared, provider-owned attachment manager.

Mirrors ``@traceai/framework-attachments`` (dialect ``llamaindex``). Only
``ChatMessage.blocks`` (llama-index-core 0.12.3+) of chat-model input and of the
non-streaming chat response are observed:

- ``ImageBlock.image``, ``AudioBlock.audio`` and ``DocumentBlock.data`` held as
  bytes (LlamaIndex stores them base64-encoded; raw bytes are accepted too);
- the magic bytes must match the admitted type.

An ``IOBase`` source is never read, a ``path`` is never opened, and a ``url`` goes
to the core's ``RemoteUrl`` policy without being fetched. Bytes identical to ones
a provider instrumentor already uploaded in the same trace are not uploaded again.

This module imports ``fi_instrumentation.attachments``; the handler imports it only
when an enabled attachment manager is passed.
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

__all__ = ("capture_llamaindex_messages", "llamaindex_attachment_capabilities")

llamaindex_attachment_capabilities = MappingProxyType({
    "schema_version": 1,
    "operations": ("llm.chat.input", "llm.chat.output"),
    "sources": ("block_bytes",),
    "unsupported": ("io_source", "local_path", "remote_url_fetch", "video"),
    "min_core": "0.12.3",
    "max_part_bytes": MAX_FRAMEWORK_BYTES,
    "max_parts_per_span": MAX_FRAMEWORK_PARTS,
})

_ROLES = ("user", "assistant", "tool", "system")
_AUDIO_FORMATS = {"wav": "audio/wav", "mp3": "audio/mpeg", "ogg": "audio/ogg", "flac": "audio/flac",
                  "webm": "audio/webm"}
# block_type -> (data field, MIME field, default family)
_BLOCKS = {"image": ("image", "image_mimetype", IMAGE_MIMES), "audio": ("audio", None, AUDIO_MIMES),
           "document": ("data", "document_mimetype", DOCUMENT_MIMES)}


def _field(block: Any, name: str) -> Any:
    """A declared model field only: never a property, resolver or ``__getattr__``."""
    try:
        values = vars(block)
    except TypeError:
        return None
    return values.get(name)


def _ordered(mime: Any, family: tuple) -> Optional[tuple]:
    if mime is None:
        return family
    if isinstance(mime, str) and mime in family:
        return (mime,) + tuple(m for m in family if m != mime)
    return None


def _role(message: Any) -> str:
    role = _field(message, "role")
    role = getattr(role, "value", role)
    return role if role in _ROLES else "unknown"


def _parts(messages: Sequence[Any], direction: str) -> Iterator[MediaPart]:
    for message_index, message in enumerate(list(messages)[:MAX_FRAMEWORK_PARTS]):
        role = _role(message)
        blocks = _field(message, "blocks")
        if not isinstance(blocks, list):
            continue
        for part_index, block in enumerate(blocks[:MAX_FRAMEWORK_PARTS]):
            spec = _BLOCKS.get(_field(block, "block_type"))
            if spec is None:
                continue
            data_field, mime_field, family = spec
            if mime_field is not None:
                mimes = _ordered(_field(block, mime_field), family)
            else:
                fmt = _field(block, "format")
                mimes = _ordered(_AUDIO_FORMATS.get(fmt), family) if fmt is not None else family
            if not mimes:
                continue
            purpose = ("model_output" if direction == "output" else "tool_result" if role == "tool"
                       else "document" if family is DOCUMENT_MIMES else "model_input")
            data = _field(block, data_field)
            url = _field(block, "url")
            if isinstance(data, (bytes, bytearray)):
                # Base64 ASCII when it parses strictly; otherwise complete raw bytes.
                kind = "inline" if looks_base64(data) else "bytes"
                yield MediaPart(kind, lambda d=data: d, mimes, purpose, message_index, part_index, role=role,
                                raw=True)
            elif data is None and url is not None and _field(block, "path") is None:
                yield MediaPart("url", lambda u=url: str(u), mimes, purpose, message_index, part_index,
                                role=role)
            # IOBase sources and paths: never read or opened.


def capture_llamaindex_messages(manager, span, messages: Any, *, direction: str, trace_config=None,
                                capture=None) -> Mapping[str, Occurrence]:
    """Capture typed block media of chat messages; returns ``"{dir}/{msg}/{part}"`` positions.

    Never raises and never changes ``messages``.
    """
    try:
        if not isinstance(messages, (list, tuple)):
            return EMPTY
        return capture_parts(manager, span, "llamaindex", _parts(messages, direction), direction=direction,
                             trace_configs=(trace_config,), capture=capture)
    except Exception:
        return EMPTY
