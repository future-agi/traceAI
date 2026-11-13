"""Content-free handles, shared vocabulary and typed sources (no implicit reads)."""

import asyncio
import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import Optional, Union

from opentelemetry import trace
from opentelemetry.trace import SpanContext

# F01 MIME -> Modality (contract.schema.json allOf mapping).
MIME_MODALITIES = {
    "audio/wav": "audio", "audio/ogg": "audio", "audio/mpeg": "audio", "audio/mp4": "audio",
    "audio/webm": "audio", "audio/flac": "audio", "audio/L16": "audio",
    "image/png": "image", "image/jpeg": "image", "image/webp": "image",
    "video/mp4": "video", "video/webm": "video",
    "application/pdf": "document", "application/octet-stream": "file",
}
AUDIO_MIMES = frozenset(m for m, k in MIME_MODALITIES.items() if k == "audio")
PURPOSES = ("conversation_recording", "turn_recording", "model_input", "model_output", "tool_result",
            "computer_screenshot", "diagnostic", "document")
REFERENCE_ROLES = ("user", "assistant", "tool", "system", "unknown")
TRACK_ROLES = ("user", "assistant", "mixed", "unknown")
DIRECTIONS = ("input", "output", "mixed")
GAP_REASONS = ("queue_full", "bootstrap_pending", "source_unavailable", "policy_denied", "truncated", "incomplete")
ADMISSION_REASONS = frozenset((
    "queued", "disabled", "bootstrap_pending", "policy_denied", "credential_scope_unsupported",
    "source_unavailable", "unsampled", "too_large", "queue_full", "unsupported_version",
    "endpoint_unconfigured", "scope_unresolved", "correlation_unavailable", "redaction_unavailable",
    "recording_disabled", "expired", "deleted", "incomplete",
))
COMPLETION_STATUSES = frozenset((
    "validating", "importing", "incomplete", "expired", "deleted", "policy_denied", "too_large", "throttled",
    "conflict", "not_found", "credential_scope_unsupported", "validation_failed", "unsupported_version",
    "source_unavailable", "redaction_unavailable",
))
SAFE_REASONS = ADMISSION_REASONS | COMPLETION_STATUSES
TOKEN = re.compile(r"[a-zA-Z0-9_-]{1,128}")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def canonical_uuid(value) -> Optional[str]:
    if not isinstance(value, str) or not UUID_RE.fullmatch(value) or not uuid.UUID(value).int:
        return None
    return value


@dataclass(frozen=True)
class Bytes:
    data: Union[bytes, bytearray, memoryview] = field(repr=False)


@dataclass(frozen=True)
class RemoteUrl:
    url: str = field(repr=False)


@dataclass(frozen=True)
class LocalFile:
    path: str = field(repr=False)


@dataclass(frozen=True)
class ProviderFile:
    provider: str
    file_id: str = field(repr=False)


@dataclass(frozen=True)
class CorrelationHandle:
    """Snapshot of an actual emitted identity, usable after the span ends.

    Capture this while the span is recording. No subsequent ambient span lookup
    occurs. A sampled SpanContext is also accepted; the server still has to bind
    the association to this writer before anything becomes readable.
    """

    trace_id: str
    span_id: str
    sampled: bool

    def __post_init__(self) -> None:
        value = self.trace_id
        if not isinstance(value, str) or not re.fullmatch(
            r"[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value
        ):
            raise ValueError("invalid trace identity")
        canonical = uuid.UUID(value)
        if not canonical.int or not isinstance(self.span_id, str) or not re.fullmatch(r"[0-9a-f]{16}", self.span_id) or not int(self.span_id, 16):
            raise ValueError("invalid correlation identity")
        if type(self.sampled) is not bool:
            raise ValueError("invalid sampling decision")
        object.__setattr__(self, "trace_id", canonical.hex)

    @property
    def trace_uuid(self) -> str:
        return str(uuid.UUID(self.trace_id))

    @classmethod
    def from_span(cls, span_or_context=None) -> "CorrelationHandle":
        if isinstance(span_or_context, cls):
            return span_or_context
        span = trace.get_current_span() if span_or_context is None else span_or_context
        if isinstance(span, SpanContext):
            context, recording = span, True
        else:
            context, recording = span.get_span_context(), span.is_recording()
        if not context.is_valid:
            raise ValueError("correlation unavailable")
        return cls(format(context.trace_id, "032x"), format(context.span_id, "016x"),
                   bool(context.trace_flags.sampled and recording))


@dataclass(frozen=True)
class Completion:
    status: str
    # "validating" is transfer completion, not a storage/readiness claim.
    # "unfinished" is only the local result of a timed-out wait.


@dataclass(frozen=True)
class AttachmentStatus:
    state: str
    queued: int = 0
    active: int = 0
    open_tickets: int = 0
    open_streams: int = 0
    open_recordings: int = 0
    owned_bytes: int = 0
    dropped: int = 0


class AttachmentHandle:
    """Stable public IDs, local admission, and non-throwing completion.

    Neither source nor exception is stored here. Denied IDs are not resolvable
    references and must not be projected onto spans or diagnostics.
    """

    def __init__(self, admission: str, attachment_id: Optional[str] = None, occurrence_id: Optional[str] = None):
        self._attachment_id = attachment_id or str(uuid.uuid4())
        self._occurrence_id = occurrence_id or str(uuid.uuid4())
        self._admission = admission
        self._event = threading.Event()
        self._completion: Optional[Completion] = None
        if admission != "queued":
            self._finish(admission)

    @property
    def attachment_id(self) -> str:
        return self._attachment_id

    @property
    def occurrence_id(self) -> str:
        return self._occurrence_id

    @property
    def admission(self) -> str:
        return self._admission

    @property
    def reason(self) -> str:
        return self._admission

    def _finish(self, status: str) -> None:
        if not self._event.is_set():
            self._completion = Completion(status)
            self._event.set()

    def result(self, timeout: float = 0) -> Completion:
        if timeout and timeout > 0:
            self._event.wait(min(timeout, 30))
        return self._completion or Completion("unfinished")

    async def wait(self, timeout: float = 5) -> Completion:
        if self._event.is_set():
            return self._completion
        return await asyncio.to_thread(self.result, timeout)

    def __repr__(self) -> str:
        return "AttachmentHandle(admission=%r)" % self._admission


@dataclass(frozen=True)
class FlushResult:
    unfinished: int
    dropped: int
