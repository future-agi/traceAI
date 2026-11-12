"""Shared tool-media helpers for MCP and agent adapters, built on ``media_capture``.

MCP clients, ``mcp-use``, the OpenAI Agents SDK, the Claude Agent SDK,
smolagents and Firecrawl all observe media that a TOOL returned. These helpers
give them one set of rules (the TypeScript ``@traceai/mcp`` and
``@traceai/claude-agent-sdk`` semantics):

- Tool media is tool output AND potential model input, so input and output
  hide flags, ``hide_input_images`` for images and ``pii_redaction`` all deny
  (deny wins; the core cannot relax a local restriction).
- Only typed inline values are read: base64 fields, data URIs, and in-memory
  images. Nothing here fetches a URL, dereferences a resource URI or path,
  opens a file, or reads a stream. A URL or file-backed value is skipped.
- Every value is preflighted before it is read or decoded. Each observation
  has a bounded decode budget and part count; parts beyond them are skipped.
- A declared MIME is never trusted over the bytes: when the first bytes are a
  known signature (``_SIGNATURES``) of ANOTHER modality (a PNG labelled
  ``audio/wav``), the part is denied, so a mislabel cannot escape
  ``hide_input_images`` or a modality restriction. Never relabelled. The
  signatures checked are PNG, JPEG, GIF, WebP, BMP, TIFF, ISO-BMFF image
  brands (``ftyp`` avif/heic/heix/mif1/msf1), WAV, Ogg, FLAC, ID3-tagged MP3
  and PDF; other bytes (MP4/WebM containers, MP3 without an ID3 tag) are
  judged by their declared MIME only. ``application/octet-stream`` is the
  ``file`` modality, so an octet-stream blob whose bytes carry a recognised
  image, audio or PDF signature is skipped (fail-closed, never relabelled).
- A context marked by another adapter as the owner of this call's media
  (``OWNER_KEY``, shared by name with ``traceai``) captures nothing here.

Not part of ``fi_instrumentation.attachments.__all__``. Every function is
non-throwing and returns ``None``/empty on denial.
"""

import base64
import binascii
import dataclasses
import io
import sys
from typing import Any, Callable, Mapping, Optional

from opentelemetry import context as otel_context

from . import media_capture as mc
from .policy import CapturePolicy
from .types import MIME_MODALITIES, Bytes

KiB = 1024
MiB = 1024 * KiB
#: Decoded bytes one tool result may contribute (TypeScript MCP_MEDIA_DECODE_BUDGET).
TOOL_RESULT_BUDGET = 256 * KiB
#: Typed parts inspected per tool result.
MAX_TOOL_PARTS = 64
#: In-memory images are PNG-encoded only up to this many pixels (no huge synchronous encodes).
MAX_IMAGE_PIXELS = 2048 * 2048
#: The cross-adapter owner mark, by name (``media_capture.OWNER_KEY`` where the core defines it).
OWNER_KEY = getattr(mc, "OWNER_KEY", "traceai.auto.attachment_owner.v1")
URI_PREFIX = "fi-attachment:"
Occurrence = mc.Occurrence


def owned_elsewhere() -> bool:
    """True when another adapter marked this call's media as its own (by context key name)."""
    try:
        return otel_context.get_value(OWNER_KEY) is not None
    except Exception:
        return True


def with_ownership(context=None):
    """A context that tells downstream observers this call's media is captured already."""
    return otel_context.set_value(OWNER_KEY, True, context)


def uri(occurrence: Occurrence) -> str:
    return URI_PREFIX + occurrence.occurrence_id


def tool_capture_policy(trace_config=None, capture: Optional[CapturePolicy] = None, *, image: bool = False,
                        error: bool = False) -> CapturePolicy:
    """Deny-only policy for media a tool returned: input AND output restrictions both apply."""
    base = capture if isinstance(capture, CapturePolicy) else CapturePolicy()
    seen_input = mc.local_capture_policy(trace_config, direction="input", image=image, error_detail=error,
                                         base=base)
    seen_output = mc.local_capture_policy(trace_config, direction="output", image=image, error_detail=error,
                                          base=base)
    return dataclasses.replace(
        seen_input,
        capture_content=seen_input.capture_content and seen_output.capture_content,
        requested=seen_input.requested and seen_output.requested and not owned_elsewhere(),
        contains_output=True,
        model_input=True,
    )


def mime_kind(mime: Any) -> Optional[str]:
    """The F01 modality of an exact MIME; None when the MIME is not in the F01 enum."""
    return MIME_MODALITIES.get(mime) if isinstance(mime, str) else None


#: Leading-byte signatures with an unambiguous F01 modality. ISO-BMFF ``ftyp`` boxes name an
#: image only for the HEIF/AVIF still-image brands listed here; other brands (mp42, isom,
#: ``M4A ``, ``qt  ``) and WebM/Matroska are shared by audio and video, so those bytes are
#: judged by their declared MIME only. ``consistent`` decodes the first 12 bytes.
_SIGNATURES = (
    (0, b"\x89PNG\r\n\x1a\n", "image"), (0, b"\xff\xd8\xff", "image"), (0, b"GIF87a", "image"),
    (0, b"GIF89a", "image"), (8, b"WEBP", "image"), (0, b"BM", "image"), (0, b"II*\x00", "image"),
    (0, b"MM\x00*", "image"), (4, b"ftypavif", "image"), (4, b"ftypheic", "image"), (4, b"ftypheix", "image"),
    (4, b"ftypmif1", "image"), (4, b"ftypmsf1", "image"), (8, b"WAVE", "audio"),
    (0, b"OggS", "audio"), (0, b"fLaC", "audio"), (0, b"ID3", "audio"), (0, b"%PDF-", "document"),
)


def signature_kind(head: bytes) -> Optional[str]:
    """The modality the first bytes name, or None when they name none (or are ambiguous)."""
    for offset, magic, kind in _SIGNATURES:
        if head[offset:offset + len(magic)] == magic:
            if offset == 8 and head[:4] != b"RIFF":
                continue
            return kind
    return None


def _declared_kind(value: str, mimes, raw: bool) -> Optional[str]:
    if raw:
        return mime_kind(mimes[0])
    end = value.find(";", 5, 128)
    return mime_kind(value[5:end]) if value.startswith("data:") and end > 5 else None


def consistent(value: Any, mimes, raw: bool) -> bool:
    """False when the base64 value's first bytes are a signature of another modality than declared."""
    if not isinstance(value, str):
        return True  # Not inline base64: the structural parse rejects it.
    start = 0
    if not raw:
        comma = value.find(",", 0, 128)
        if comma < 0:
            return True
        start = comma + 1
    try:
        head = base64.b64decode(value[start:start + 16], validate=True)
    except (binascii.Error, ValueError):
        return True  # Malformed base64: strict decoding rejects it later.
    found = signature_kind(head)
    return found is None or found == _declared_kind(value, mimes, raw)


def _pil_image(value: Any) -> bool:
    module = sys.modules.get("PIL.Image")
    image_type = getattr(module, "Image", None) if module is not None else None
    return isinstance(image_type, type) and isinstance(value, image_type)


def model_input_policy(trace_config=None, capture: Optional[CapturePolicy] = None, *,
                       image: bool = False) -> CapturePolicy:
    """Deny-only policy for media an agent framework hands to a model as input."""
    policy = mc.local_capture_policy(trace_config, direction="input", image=image, base=capture)
    return dataclasses.replace(policy, requested=policy.requested and not owned_elsewhere())


class ToolMedia:
    """One adapter's observation of one span, with a decode budget and registry dedup.

    ``namespace`` keeps positions of different adapters apart in the shared
    per-manager registry; a repeated observation of the same span and
    position returns the first occurrence and uploads nothing.
    ``direction`` is ``"output"`` for media a tool returned (tool policy) or
    ``"input"`` for media a framework sends to a model (model-input policy).
    """

    def __init__(self, manager, span, namespace: str, *, trace_config=None,
                 capture: Optional[CapturePolicy] = None, budget: int = TOOL_RESULT_BUDGET,
                 max_parts: int = MAX_TOOL_PARTS, direction: str = "output"):
        self.manager, self.span = manager, span
        self.direction = "input" if direction == "input" else "output"
        self._trace_config, self._capture = trace_config, capture
        self.remaining = budget
        self._parts = 0
        self._max_parts = max_parts
        self._observation = mc.Observation(manager, span, namespace)
        if owned_elsewhere():
            self._observation.key = None

    def _policy(self, image: bool, error: bool) -> CapturePolicy:
        if self.direction == "input":
            return model_input_policy(self._trace_config, self._capture, image=image)
        return tool_capture_policy(self._trace_config, self._capture, image=image, error=error)

    @property
    def usable(self) -> bool:
        return self._observation.usable and self.remaining > 0

    @property
    def captured(self) -> Mapping[str, Occurrence]:
        return dict(self._observation.captured)

    def _visit(self, position: str, capture: Callable[[], Optional[Occurrence]]) -> Optional[Occurrence]:
        if not self.usable or self._parts >= self._max_parts:
            return None
        self._parts += 1
        self._observation.visit(position, capture)
        return self._observation.captured.get(position)

    def base64(self, position: str, read: Callable[[], Any], mime: Any, *, purpose: str,
               expect: Optional[str] = None, role: str = "tool", tool_call_id: Optional[str] = None,
               message_index: Optional[int] = None, part_index: Optional[int] = None,
               error: bool = False) -> Optional[Occurrence]:
        """A raw base64 value with a declared MIME (MCP ImageContent/AudioContent/blob, Anthropic source).

        ``expect`` ("image", "audio", ...) rejects a MIME of another modality, so a
        misleading MIME cannot relabel an image to escape ``hide_input_images``.
        ``read`` is called at most once, and only after a preflight admitted the part.
        """
        kind = mime_kind(mime)
        if kind is None or (expect is not None and kind != expect):
            return None
        return self._inline(position, read, (mime,), True, purpose, kind == "image", role, tool_call_id,
                            message_index, part_index, error)

    def data_uri(self, position: str, read: Callable[[], Any], mimes, *, purpose: str, role: str = "tool",
                 tool_call_id: Optional[str] = None, message_index: Optional[int] = None,
                 part_index: Optional[int] = None, error: bool = False) -> Optional[Occurrence]:
        """A ``data:<mime>;base64,`` string whose MIME must be one of ``mimes`` (first is preflighted)."""
        mimes = tuple(mime for mime in mimes if mime_kind(mime))
        if not mimes:
            return None
        image = any(mime_kind(mime) == "image" for mime in mimes)
        return self._inline(position, read, mimes, False, purpose, image, role, tool_call_id, message_index,
                            part_index, error)

    def _inline(self, position, read, mimes, raw, purpose, image, role, tool_call_id, message_index, part_index,
                error) -> Optional[Occurrence]:
        seen = {}

        def once():
            if "value" not in seen:
                value = read()
                seen["value"] = value if consistent(value, mimes, raw) else None
            return seen["value"]

        def capture():
            policy = self._policy(image, error)
            return mc.attach_inline(self.manager, self.span, once, mimes, raw=raw, direction=self.direction,
                                    purpose=purpose, capture=policy, message_index=message_index,
                                    part_index=part_index, role=role, tool_call_id=tool_call_id,
                                    max_bytes=self.remaining)

        before = self._observation.captured.get(position)
        occurrence = self._visit(position, capture)
        if occurrence is not None and before is None and "value" in seen:
            value = seen["value"]
            if not raw and isinstance(value, str):
                value = value[value.find(",", 0, 128) + 1:]
            self.remaining -= mc.encoded_length(value, self.remaining) or self.remaining
        return occurrence

    def image(self, position: str, image: Any, *, purpose: str, role: str = "tool",
              tool_call_id: Optional[str] = None, message_index: Optional[int] = None,
              part_index: Optional[int] = None, error: bool = False) -> Optional[Occurrence]:
        """An in-memory ``PIL.Image.Image``, PNG-encoded only after a pixel-bounded preflight.

        A file-backed image that is not loaded yet is skipped: encoding it would
        read the file. PIL is never imported here; a value is an image only when
        the application already imported PIL.
        """
        if not _pil_image(image):
            return None

        def capture():
            try:
                fp = getattr(image, "fp", None)
                if fp is not None and not isinstance(fp, io.BytesIO):
                    return None
                width, height = image.size
                bands = len(image.getbands())
                if not (0 < width and 0 < height and width * height <= MAX_IMAGE_PIXELS):
                    return None
                # PNG never exceeds the raw pixel bytes plus per-row filter bytes and chunk framing.
                bound = min(width * height * max(bands, 1) + height + 4 * KiB, self.remaining)
                policy = self._policy(True, error)
                options = dict(mime_type="image/png", direction=self.direction, purpose=purpose,
                               capture_policy=policy)
                ticket = self.manager.preflight(self.span, max_bytes=bound, **options)
                if not ticket.admitted:
                    return None
            except Exception:
                return None
            try:
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                data = buffer.getvalue()
            except Exception:
                data = b""
            try:
                if not data or len(data) > ticket.max_bytes:
                    ticket.release()
                    return None
                handle = ticket.attach(Bytes(data), **mc._fields(message_index, part_index, role, tool_call_id))
                if handle.admission != "queued":
                    return None
                self.remaining -= len(data)
                return Occurrence(handle.occurrence_id, "image/png")
            except Exception:
                return None

        return self._visit(position, capture)
