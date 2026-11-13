"""Preflight tickets (design section 1): admission before any capture or decode.

A ticket is single use. It holds one descriptor and a byte budget, never the
source. It is re-checked when used and can only narrow. Expiry is lazy (no
timer); a ticket collected unused returns its budget through ``weakref.finalize``.
"""

import time
import weakref
from typing import Optional

from .types import AttachmentHandle, CorrelationHandle


class _Reservation:
    """Budget owned by a ticket/stream; released exactly once."""

    __slots__ = ("_manager", "nbytes", "descriptors", "released", "__weakref__")

    def __init__(self, manager, nbytes: int, descriptors: int = 1):
        self._manager = weakref.ref(manager)
        self.nbytes, self.descriptors, self.released = nbytes, descriptors, False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        manager = self._manager()
        if manager is not None:
            manager._release(self.nbytes, self.descriptors)

    def shrink(self, nbytes: int) -> None:
        manager = self._manager()
        if manager is not None and not self.released and nbytes < self.nbytes:
            manager._release(self.nbytes - nbytes, 0)
            self.nbytes = nbytes


class AdmissionTicket:
    """Result of ``AttachmentManager.preflight``. ``admitted`` iff reason == "queued"."""

    def __init__(self, manager, reason: str, *, max_bytes: int = 0, correlation: Optional[CorrelationHandle] = None,
                 span=None, mime_type: str = "", direction: str = "mixed", operation: str = "observe_consumed",
                 source_kind: str = "Bytes", purpose: Optional[str] = None, capture=None, ttl: float = 10.0,
                 reservation: Optional[_Reservation] = None):
        self.reason = reason
        self.admitted = reason == "queued"
        self.max_bytes = max_bytes if self.admitted else 0
        self.correlation = correlation if self.admitted else None
        self._manager = manager if self.admitted else None
        self._span = span if self.admitted else None
        self._spec = (mime_type, direction, operation, source_kind, purpose, capture)
        self._expires = time.monotonic() + max(0.0, ttl)
        self._used = not self.admitted
        self._lapsed = False
        self._revoked: Optional[str] = None
        self._reservation = reservation
        if reservation is not None:
            self._finalizer = weakref.finalize(self, reservation.release)
            manager._track_ticket(self)

    @property
    def expired(self) -> bool:
        return self.admitted and not self._used and time.monotonic() >= self._expires

    def _consume(self) -> Optional[str]:
        """Mark used; returns a denial reason when the ticket cannot be used."""
        if not self.admitted:
            return self.reason
        if self._lapsed:
            return "expired"
        if self._revoked:
            return self._revoked
        if self._used:
            return "conflict"
        self._used = True
        if time.monotonic() >= self._expires:
            self._drop()
            return "expired"
        return None

    def _drop(self) -> None:
        if self._reservation is not None:
            self._reservation.release()
            self._finalizer.detach()
        self._span = None

    def _revoke(self, reason: str) -> None:
        """Policy tightened before use: the reservation is returned; use reports ``reason``."""
        if not self._used:
            self._used, self._revoked = True, reason
            self._drop()

    def _sweep(self) -> bool:
        """Lazy expiry from a manager entry point. True when budget was returned."""
        if self.expired:
            self._used = self._lapsed = True
            self._drop()
            return True
        return False

    def attach(self, source, *, message_index: Optional[int] = None, part_index: Optional[int] = None,
               role: Optional[str] = None, channel: Optional[int] = None, conversation_id: Optional[str] = None,
               turn_id: Optional[str] = None, tool_call_id: Optional[str] = None) -> AttachmentHandle:
        reason = self._consume()
        if reason:
            return AttachmentHandle(reason)
        manager = self._manager
        try:
            return manager._commit_attach(self, source, dict(
                message_index=message_index, part_index=part_index, role=role, channel=channel,
                conversation_id=conversation_id, turn_id=turn_id, tool_call_id=tool_call_id))
        except Exception:
            self._drop()
            return AttachmentHandle("source_unavailable")
        finally:
            self._span = None

    def start_stream(self, options) -> "object":
        from .stream import AttachmentStream

        reason = self._consume()
        if reason:
            return AttachmentStream.denied(reason)
        try:
            return self._manager._commit_stream(self, options)
        except Exception:
            self._drop()
            return AttachmentStream.denied("source_unavailable")

    def release(self) -> None:
        if not self._used:
            self._used = True
        self._drop()

    def __enter__(self) -> "AdmissionTicket":
        return self

    def __exit__(self, *exc) -> None:
        if not self._used:
            self.release()

    def __repr__(self) -> str:
        return "AdmissionTicket(reason=%r, max_bytes=%r)" % (self.reason, self.max_bytes)
