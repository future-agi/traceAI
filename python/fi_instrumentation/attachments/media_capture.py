"""Shared provider-media helpers, built only on the public manager API.

Provider adapters (``traceai_openai``, ``traceai_anthropic``, ``traceai_groq``)
use these to capture inline base64/data-URI parts and speech audio. Rules:

- A source is never read or decoded before ``manager.preflight`` admitted it.
  An admitted ticket that is not used is released at once.
- Nothing here fetches a URL, opens a path, reads an open file object, or pulls
  from a caller's stream. Consumed audio is only the chunks the application
  itself already read (``ConsumedAudio``).
- A local ``TraceConfig`` with PII redaction or a hide flag turns capture off
  for that direction (deny wins). The manager's own policy still applies.
- One registry per manager remembers admitted (span, position) pairs, so a
  second observer of the same span and position gets the FIRST occurrence back
  and uploads nothing.
- A per-call ``CapturePolicy`` set in the OpenTelemetry context under
  ``CAPTURE_POLICY_KEY`` (``traceai.with_attachment_capture_policy``) can only
  add restrictions. A context marked under ``OWNER_KEY``
  (``traceai.with_attachment_ownership``) belongs to an adapter that captures
  the media itself, so nothing is captured here for that call.

Every public function is non-throwing and returns ``None``/empty on denial.
"""

import base64
import binascii
import dataclasses
import re
import threading
import weakref
from collections import OrderedDict
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Optional, Tuple

from opentelemetry import context as otel_context

from .policy import CapturePolicy
from .stream import StreamOptions
from .types import Bytes, CorrelationHandle

KiB = 1024
MiB = 1024 * KiB
MAX_INLINE_BYTES = 256 * KiB
MAX_PARTS = 256
MAX_SPANS = 1024
# Consumed-stream reservations, largest first. A server whose max_upload_bytes is
# below 4 MiB denies the larger ones with too_large (after every privacy step),
# so stepping down is safe and needs no private limit accessor.
STREAM_RESERVATIONS = (4 * MiB, 2 * MiB, 1 * MiB, 512 * KiB, 256 * KiB)
IMAGE_MIMES = ("image/png", "image/jpeg", "image/webp")
FILE_MIMES = ("application/pdf", "application/octet-stream")
TOOL_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}")
_DATA_URI = re.compile(r"data:([a-z]+/[a-z0-9.+-]+);base64")
_BASE64 = re.compile(r"[A-Za-z0-9+/]*={0,2}")
_INPUT_HIDE = ("hide_inputs", "hide_input_messages", "hide_input_text")
_OUTPUT_HIDE = ("hide_outputs", "hide_output_messages", "hide_output_text")
# Speech response_format values -> MIME. aac (raw ADTS), pcm (headerless) and
# mulaw have no F01 MIME, so they are never captured.
SPEECH_FORMATS = {"mp3": "audio/mpeg", "opus": "audio/ogg", "ogg": "audio/ogg", "flac": "audio/flac",
                  "wav": "audio/wav"}
# Context keys shared BY NAME with the auto module (``traceai``) and any adapter, like the
# TypeScript ``Symbol.for`` keys: plain strings, so no package has to import another.
CAPTURE_POLICY_KEY = "traceai.auto.attachment_capture_policy.v1"
OWNER_KEY = "traceai.auto.attachment_owner.v1"
# CapturePolicy facts that grant capture; every other fact only restricts.
_PERMISSIONS = frozenset(("capture_content", "record_inputs", "record_outputs", "requested"))
_DENY_ALL = CapturePolicy(capture_content=False, requested=False)


class Occurrence(NamedTuple):
    occurrence_id: str
    mime_type: str


EMPTY: Mapping[str, Occurrence] = {}


def active(manager) -> bool:
    """A manager that can admit anything. The disabled null object never can."""
    if manager is None:
        return False
    from ._disabled import DisabledAttachmentManager

    return not isinstance(manager, DisabledAttachmentManager) and callable(getattr(manager, "preflight", None))


def get(value: Any, key: str) -> Any:
    """Read a field of a request dict or an SDK response model; never raises."""
    try:
        if isinstance(value, Mapping):
            return value.get(key)
        return getattr(value, key, None)
    except Exception:
        return None


def items(value: Any) -> list:
    return list(value[:MAX_PARTS]) if isinstance(value, (list, tuple)) else []


def _restricted(config, name: str) -> bool:
    if config is None:
        return False
    try:
        return bool(getattr(config, name, False))
    except Exception:
        # An unreadable restriction is a restriction, never permission.
        return True


def merge_capture_policies(*policies: Any) -> CapturePolicy:
    """Deny-wins intersection: a grant needs every policy, a restriction needs any one.

    ``None`` adds nothing. Any other value that is not a ``CapturePolicy`` denies.
    """
    present = [policy for policy in policies if policy is not None]
    if any(not isinstance(policy, CapturePolicy) for policy in present):
        return _DENY_ALL
    if not present:
        return CapturePolicy()
    values = {}
    for item in dataclasses.fields(CapturePolicy):
        facts = [getattr(policy, item.name) for policy in present]
        values[item.name] = all(facts) if item.name in _PERMISSIONS else any(facts)
    return CapturePolicy(**values)


def context_capture_policy(context=None) -> Any:
    """The per-call policy in the (current) OpenTelemetry context; None when unset."""
    try:
        return otel_context.get_value(CAPTURE_POLICY_KEY, context)
    except Exception:
        # An unreadable restriction is a restriction, never permission.
        return _DENY_ALL


def owned_elsewhere(context=None) -> bool:
    """True when another adapter marked this call's media as its own."""
    try:
        return otel_context.get_value(OWNER_KEY, context) is not None
    except Exception:
        return True


def local_capture_policy(trace_config=None, *, direction: str, image: bool = False, error_detail: bool = False,
                         base: Optional[CapturePolicy] = None) -> CapturePolicy:
    """Fold the instrumentor's local TraceConfig into a deny-only CapturePolicy.

    Any hide flag for the direction, or ``pii_redaction``, sets
    ``capture_content=False``; the core cannot relax it, not even with
    ``allow_unredacted_media_with_text_pii``. Input media keeps model-input
    ancestry, so a tool screenshot is still treated as model input.
    """
    base = base if isinstance(base, CapturePolicy) else CapturePolicy()
    base = merge_capture_policies(base, context_capture_policy())
    names = _INPUT_HIDE if direction == "input" else _OUTPUT_HIDE
    hidden = any(_restricted(trace_config, name) for name in names) or _restricted(trace_config, "pii_redaction")
    if image and direction == "input" and _restricted(trace_config, "hide_input_images"):
        hidden = True
    if error_detail and _restricted(trace_config, "hide_error_details"):
        hidden = True
    changes = {}
    if hidden:
        changes["capture_content"] = False
    if direction == "input":
        changes["model_input"] = True
    if error_detail:
        changes["error_detail"] = True
    if owned_elsewhere():
        changes["requested"] = False
    return dataclasses.replace(base, **changes)


def span_key(span) -> Optional[str]:
    """The emitted identity of a sampled, recording span, else None."""
    try:
        correlation = CorrelationHandle.from_span(span)
    except Exception:
        return None
    return "%s/%s" % (correlation.trace_id, correlation.span_id) if correlation.sampled else None


class _Registry:
    """Admitted positions per manager and span. Holds IDs only, never sources."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._managers: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

    def get(self, manager, key: str, position: str) -> Optional[Occurrence]:
        with self._lock:
            spans = self._managers.get(manager)
            positions = spans.get(key) if spans is not None else None
            return positions.get(position) if positions is not None else None

    def put(self, manager, key: str, position: str, occurrence: Occurrence) -> None:
        with self._lock:
            spans = self._managers.get(manager)
            if spans is None:
                spans = OrderedDict()
                self._managers[manager] = spans
            positions = spans.get(key)
            if positions is None:
                if len(spans) >= MAX_SPANS:
                    spans.popitem(last=False)
                positions = spans[key] = {}
            if len(positions) < MAX_PARTS:
                positions[position] = occurrence


REGISTRY = _Registry()


def encoded_length(encoded: Any, max_bytes: int = MAX_INLINE_BYTES) -> int:
    """Exact decoded length from the string's shape only (no decode); 0 when out of bounds."""
    if not isinstance(encoded, str):
        return 0
    length = len(encoded)
    if not length or length % 4 or length > 4 * -(-max_bytes // 3):
        return 0
    return (length // 4) * 3 - (2 if encoded.endswith("==") else 1 if encoded.endswith("=") else 0)


def split_inline(value: Any, mime_types: Tuple[str, ...], raw: bool,
                 max_bytes: int = MAX_INLINE_BYTES) -> Optional[Tuple[str, str, int]]:
    """Structural parse: (mime, encoded, exact decoded length) or None. Bounded before slicing."""
    if not isinstance(value, str):
        return None
    mime, start = mime_types[0], 0
    if not raw:
        comma = value.find(",", 0, 128)
        match = _DATA_URI.fullmatch(value, 0, comma) if comma > 5 else None
        if match is None or match.group(1) not in mime_types:
            return None
        mime, start = match.group(1), comma + 1
    if not 0 < len(value) - start <= 4 * -(-max_bytes // 3):
        return None
    encoded = value[start:]
    bound = encoded_length(encoded, max_bytes)
    return (mime, encoded, bound) if bound else None


def strict_decode(encoded: str, max_bytes: int, cap: int = MAX_INLINE_BYTES) -> Optional[bytes]:
    """Strict alphabet, canonical padding, and the ticket's budget as a hard bound."""
    if not encoded_length(encoded, cap) or not _BASE64.fullmatch(encoded):
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not data or len(data) > max_bytes or len(data) > cap or base64.b64encode(data).decode("ascii") != encoded:
        return None
    return data


def _fields(message_index=None, part_index=None, role=None, tool_call_id=None) -> dict:
    fields = {"message_index": message_index, "part_index": part_index, "role": role}
    if isinstance(tool_call_id, str) and TOOL_ID.fullmatch(tool_call_id):
        fields["tool_call_id"] = tool_call_id
    return fields


def attach_inline(manager, span, read: Callable[[], Any], mime_types: Tuple[str, ...], *, raw: bool,
                  direction: str, purpose: Optional[str], capture: CapturePolicy,
                  message_index: Optional[int] = None, part_index: Optional[int] = None,
                  role: Optional[str] = None, tool_call_id: Optional[str] = None,
                  max_bytes: int = MAX_INLINE_BYTES) -> Optional[Occurrence]:
    """Preflight, then read once, decode strictly and attach. Never raises."""
    try:
        state = {}

        def parsed():
            if "value" not in state:
                state["value"] = split_inline(read(), mime_types, raw, max_bytes)
            return state["value"]

        def preflight(mime):
            options = dict(mime_type=mime, direction=direction, purpose=purpose, capture_policy=capture)
            ticket = manager.preflight(span, max_bytes=max_bytes, **options)
            # too_large (negotiated limit below the cap) and queue_full (budget nearly used) come after
            # every privacy, correlation and sampling step, so sizing exactly and retrying once is safe.
            if ticket.reason in ("too_large", "queue_full"):
                found = parsed()
                if found:
                    ticket = manager.preflight(span, max_bytes=found[2], **options)
            return ticket

        ticket = preflight(mime_types[0])
        if not ticket.admitted:
            return None
        found = parsed()
        if not found:
            ticket.release()
            return None
        mime, encoded, _ = found
        if mime != mime_types[0]:
            # A data URI names a sibling MIME of the same modality: re-admit under the exact type.
            ticket.release()
            ticket = preflight(mime)
            if not ticket.admitted:
                return None
        data = strict_decode(encoded, ticket.max_bytes, max_bytes)
        if data is None:
            ticket.release()
            return None
        handle = ticket.attach(Bytes(data), **_fields(message_index, part_index, role, tool_call_id))
        return Occurrence(handle.occurrence_id, mime) if handle.admission == "queued" else None
    except Exception:
        return None


class Observation:
    """One adapter observation of one span: registry lookup plus admitted positions."""

    def __init__(self, manager, span, namespace: str):
        self.manager, self.span, self.namespace = manager, span, namespace
        self.key = span_key(span) if active(manager) and not owned_elsewhere() else None
        self.captured: "OrderedDict[str, Occurrence]" = OrderedDict()
        self._visited = 0

    @property
    def usable(self) -> bool:
        return self.key is not None

    def visit(self, position: str, capture: Callable[[], Optional[Occurrence]]) -> None:
        if self.key is None:
            return
        self._visited += 1
        if self._visited > MAX_PARTS:
            return
        registry_position = "%s/%s" % (self.namespace, position)
        previous = REGISTRY.get(self.manager, self.key, registry_position)
        if previous is not None:
            self.captured[position] = previous
            return
        occurrence = capture()
        if occurrence is not None:
            REGISTRY.put(self.manager, self.key, registry_position, occurrence)
            self.captured[position] = occurrence


# ------------------------------------------------------------------ audio
def sniff_audio(head: Any) -> Optional[str]:
    """MIME from container magic bytes the application already holds; None when unknown."""
    if not isinstance(head, (bytes, bytearray, memoryview)):
        return None
    head = bytes(head[:12])
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head[:4] == b"OggS":
        return "audio/ogg"
    if head[:4] == b"fLaC":
        return "audio/flac"
    if head[:3] == b"ID3":
        return "audio/mpeg"
    # MPEG audio frame sync with a defined layer. Layer bits 00 is AAC ADTS, which is not captured.
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0 and (head[1] >> 1) & 3:
        return "audio/mpeg"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "audio/webm"
    return None


def audio_mime(head: Any, declared_format: Optional[str] = None) -> Optional[str]:
    """The sniffed container, cross-checked against a declared format when one was sent."""
    sniffed = sniff_audio(head)
    if sniffed is None:
        return None
    if declared_format is not None and SPEECH_FORMATS.get(declared_format) != sniffed:
        return None
    return sniffed


def attach_audio(manager, span, data: Any, *, direction: str, purpose: Optional[str], capture: CapturePolicy,
                 declared_format: Optional[str] = None, message_index: Optional[int] = None,
                 part_index: Optional[int] = None, role: Optional[str] = None) -> Optional[Occurrence]:
    """Complete in-memory audio the SDK already holds (never a stream or file). Never raises."""
    try:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            return None
        length = data.nbytes if isinstance(data, memoryview) else len(data)
        if length <= 0:
            return None
        options = dict(direction=direction, purpose=purpose, capture_policy=capture, max_bytes=length)
        # Admit the modality before looking at the bytes, then re-admit under the exact container type.
        ticket = manager.preflight(span, mime_type="audio/wav", **options)
        if not ticket.admitted:
            return None
        mime = audio_mime(data[:12], declared_format)
        if mime is None:
            ticket.release()
            return None
        if mime != "audio/wav":
            ticket.release()
            ticket = manager.preflight(span, mime_type=mime, **options)
            if not ticket.admitted:
                return None
        handle = ticket.attach(Bytes(bytes(data) if not isinstance(data, bytes) else data),
                               **_fields(message_index, part_index, role))
        return Occurrence(handle.occurrence_id, mime) if handle.admission == "queued" else None
    except Exception:
        return None


class ConsumedAudio:
    """Forward audio chunks the APPLICATION consumed; never pulls, clones or tees.

    Admission happens on the first consumed bytes. Exceeding the reservation,
    a tightened policy, ``abort()``, or ending without ``finish()`` uploads
    nothing. Only ``finish()`` after a complete read uploads.
    """

    _HEAD = 12

    def __init__(self, manager, span, *, direction: str, purpose: Optional[str], capture: CapturePolicy,
                 declared_format: Optional[str] = None, role: Optional[str] = None):
        self._manager, self._span = manager, span
        self._direction, self._purpose, self._capture = direction, purpose, capture
        self._declared, self._role = declared_format, role
        self._head = bytearray()
        self._stream = None
        self._done = False
        self.reason: Optional[str] = None
        self.occurrence: Optional[Occurrence] = None
        self._mime: Optional[str] = None

    def _start(self) -> None:
        mime = audio_mime(bytes(self._head), self._declared)
        if mime is None:
            self._stop("source_unavailable")
            return
        ticket = None
        for reserve in STREAM_RESERVATIONS:
            ticket = self._manager.preflight(self._span, mime_type=mime, direction=self._direction,
                                             max_bytes=reserve, purpose=self._purpose,
                                             capture_policy=self._capture)
            if ticket.reason != "too_large":
                break
        if ticket is None or not ticket.admitted:
            self._stop(getattr(ticket, "reason", "policy_denied"))
            return
        stream = ticket.start_stream(StreamOptions(
            mime_type=mime, direction=self._direction, max_bytes=ticket.max_bytes, purpose=self._purpose,
            capture_policy=self._capture, role=self._role))
        if not stream.open:
            self._stop(stream.reason)
            return
        self._stream, self._mime = stream, mime
        head, self._head = bytes(self._head), bytearray()
        if not stream.try_write(head):
            self._stop(stream.reason)

    def _stop(self, reason: Optional[str]) -> None:
        self._done = True
        self.reason = reason or "incomplete"
        self._head = bytearray()
        if self._stream is not None:
            self._stream.abort()
            self._stream = None

    def write(self, chunk: Any) -> None:
        try:
            if self._done or not isinstance(chunk, (bytes, bytearray, memoryview)) or not len(chunk):
                return
            if self._stream is None:
                need = self._HEAD - len(self._head)
                self._head += bytes(chunk[:need])
                chunk = chunk[need:]
                if len(self._head) < self._HEAD:
                    return
                self._start()
                if self._stream is None or not len(chunk):
                    return
            if not self._stream.try_write(chunk):
                self._stop(self._stream.reason)
        except Exception:
            self._stop("source_unavailable")

    def finish(self) -> Optional[Occurrence]:
        try:
            if self._done or self._stream is None:
                self._stop(self.reason or "incomplete")
                return None
            stream, self._stream = self._stream, None
            self._done = True
            handle = stream.finish()
            self.reason = handle.admission
            if handle.admission == "queued":
                self.occurrence = Occurrence(handle.occurrence_id, self._mime)
            return self.occurrence
        except Exception:
            self._stop("source_unavailable")
            return None

    def abort(self) -> None:
        if not self._done:
            self._stop("incomplete")


def request_files(files: Any) -> Iterable[Tuple[int, Any]]:
    """(index, content) of multipart file entries; content is whatever the SDK will send."""
    try:
        entries = files.items() if isinstance(files, Mapping) else files
        for index, entry in enumerate(list(entries or [])[:MAX_PARTS]):
            value = entry[1] if isinstance(entry, tuple) and len(entry) == 2 else None
            content = value[1] if isinstance(value, tuple) and len(value) >= 2 else value
            yield index, content
    except Exception:
        return
