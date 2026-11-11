"""Consumed-stream observation (design section 6): a single bounded object.

The core never pulls from, clones or tees a caller's stream. An adapter on the
application's own consumption path forwards chunks the application already
read. The whole ``max_bytes`` is reserved at admission; exceeding it discards
everything (``too_large``). A reference is projected only at ``finish()``.
"""

import threading
import time
import weakref
from dataclasses import dataclass
from typing import Optional

from .types import AttachmentHandle


@dataclass(frozen=True)
class StreamOptions:
    mime_type: str
    direction: str
    max_bytes: int
    purpose: Optional[str] = None
    capture_policy: Optional[object] = None
    message_index: Optional[int] = None
    part_index: Optional[int] = None
    role: Optional[str] = None
    channel: Optional[int] = None
    conversation_id: Optional[str] = None
    turn_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    idle_ttl: float = 60.0


class AttachmentStream:
    """Synchronous, non-throwing, no I/O on ``try_write``."""

    def __init__(self, manager=None, ticket=None, options: Optional[StreamOptions] = None, reason: str = "queued"):
        self.reason = reason
        self.attachment_id: Optional[str] = None
        self.occurrence_id: Optional[str] = None
        self._manager = manager
        self._ticket = ticket
        self._options = options
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._epoch = manager._epoch if manager is not None else 0
        self._last = time.monotonic()
        self._idle = min(max(float(options.idle_ttl), 0.0), 300.0) if options is not None else 0.0
        self._done = reason != "queued"
        self._handle: Optional[AttachmentHandle] = None
        if not self._done:
            self.attachment_id = ticket._attachment_id
            self.occurrence_id = ticket._occurrence_id
            reservation = ticket._reservation
            self._finalizer = weakref.finalize(self, reservation.release)
            manager._track_stream(self)

    @classmethod
    def denied(cls, reason: str) -> "AttachmentStream":
        return cls(reason=reason)

    @property
    def open(self) -> bool:
        return not self._done

    def _discard(self, reason: str) -> None:
        # Caller holds self._lock.
        self._done = True
        self.reason = reason
        self._buffer = bytearray()
        self.attachment_id = self.occurrence_id = None
        if self._ticket is not None:
            self._ticket._drop()
            self._finalizer.detach()
            self._ticket = None

    def try_write(self, chunk) -> bool:
        try:
            with self._lock:
                if self._done:
                    return False
                manager = self._manager
                if manager._stopped or manager._stopping:
                    self._discard("incomplete")
                    return False
                if manager._epoch != self._epoch:
                    reason = manager._recheck_ticket(self._ticket)
                    if reason:
                        self._discard(reason)
                        return False
                    self._epoch = manager._epoch
                view = memoryview(chunk).cast("B")
                if len(self._buffer) + len(view) > self._ticket.max_bytes:
                    self._discard("too_large")
                    return False
                step = manager.config.chunk_bytes
                for offset in range(0, len(view), step):
                    self._buffer += view[offset:offset + step]
                self._last = time.monotonic()
                return True
        except Exception:
            return False

    def finish(self) -> AttachmentHandle:
        with self._lock:
            if self._done:
                return self._handle or AttachmentHandle(self.reason)
            self._done = True
            data, self._buffer = bytes(self._buffer), bytearray()
            ticket, self._ticket = self._ticket, None
            self._finalizer.detach()
        if not data:
            ticket._drop()
            self.reason = "incomplete"
            self._handle = AttachmentHandle("incomplete")
            return self._handle
        try:
            self._handle = self._manager._commit_stream_finish(ticket, self._options, data)
        except Exception:
            ticket._drop()
            self._handle = AttachmentHandle("source_unavailable")
        self.reason = self._handle.admission
        return self._handle

    def abort(self, reason: str = "incomplete") -> None:
        with self._lock:
            if not self._done:
                self._discard("policy_denied" if reason == "policy_denied" else "incomplete")

    def _sweep(self, now: float) -> bool:
        with self._lock:
            if not self._done and now - self._last >= self._idle:
                self._discard("incomplete")
                return True
        return False
