"""Content-free handles and typed attachment sources (no implicit reads)."""

import asyncio
import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import Optional, Union

from opentelemetry import trace
from opentelemetry.trace import SpanContext


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


class AttachmentHandle:
    """Stable public IDs, local admission, and non-throwing completion.

    Neither source nor exception is stored here. Denied IDs are not resolvable
    references and must not be projected onto spans or diagnostics.
    """

    def __init__(self, admission: str):
        self._attachment_id = str(uuid.uuid4())
        self._occurrence_id = str(uuid.uuid4())
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

    def _finish(self, status: str) -> None:
        if not self._event.is_set():
            self._completion = Completion(status)
            self._event.set()

    def result(self, timeout: float = 0) -> Completion:
        self._event.wait(max(0, min(timeout, 30)))
        return self._completion or Completion("unfinished")

    async def wait(self, timeout: float = 5) -> Completion:
        return await asyncio.to_thread(self.result, timeout)


@dataclass(frozen=True)
class FlushResult:
    unfinished: int
    dropped: int
