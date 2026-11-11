"""The disabled null object (design 8.1) and shared light handle types.

When attachments are OFF nothing here starts a thread or timer, registers a
fork/atexit hook, imports the wire adapter or ``jsonschema``, re-reads the
environment, or writes span attributes. Every call returns immediately.
"""

import asyncio
import threading
from typing import Optional

from .admission import AdmissionTicket
from .imports import ImportHandle
from .policy import AttachmentConfig, AttachmentPolicy
from .recording import RecordingWriter
from .stream import AttachmentStream
from .types import AttachmentHandle, AttachmentStatus, Completion, FlushResult


class OwnedRecordingHandle:
    """Result of ``attach_owned_recording``: tier ``single``, ``wav_recording`` or ``denied``."""

    def __init__(self, tier: str, reason: str, *, handle: Optional[AttachmentHandle] = None,
                 recording_id: Optional[str] = None, manager=None):
        self.tier = tier
        self.reason = reason
        self.recording_id = recording_id if tier == "wav_recording" else None
        self._handle = handle
        self._manager = manager
        self._job = None
        self._event = threading.Event()
        self._completion: Optional[Completion] = None
        if tier == "denied" or (handle is None and reason != "queued"):
            self._finish(reason)

    def _finish(self, status: str) -> None:
        if not self._event.is_set():
            self._completion = Completion(status)
            self._event.set()

    def result(self, timeout: float = 0) -> Completion:
        if self._handle is not None:
            return self._handle.result(timeout)
        if timeout and timeout > 0:
            self._event.wait(min(timeout, 30))
        return self._completion or Completion("unfinished")

    async def wait(self, timeout: float = 30.0) -> Completion:
        if self._handle is not None:
            return await self._handle.wait(timeout)
        if self._event.is_set():
            return self._completion
        return await asyncio.to_thread(self.result, timeout)

    def cancel(self) -> None:
        if self._manager is None:
            return
        if self._handle is not None:
            self._manager.cancel(self._handle)
        elif self._job is not None:
            self._job.cancelled = True
            self._manager.cancel(self)

    def __repr__(self) -> str:
        return "OwnedRecordingHandle(tier=%r, reason=%r)" % (self.tier, self.reason)


_EMPTY = FlushResult(0, 0)


class DisabledAttachmentManager:
    """Full public surface; every call is inert and returns ``disabled``."""

    capture_session_id = None
    correlation_nonce = None

    def __init__(self, *, config: Optional[AttachmentConfig] = None, trace_config=None):
        self.config = config or AttachmentConfig(enabled=False)
        self.policy = AttachmentPolicy(self.config, trace_config)

    def status(self) -> AttachmentStatus:
        return AttachmentStatus("disabled")

    @property
    def owned_bytes(self) -> int:
        return 0

    def ready(self, timeout: float = 5) -> bool:
        return False

    async def ready_async(self, timeout: float = 5) -> bool:
        return False

    def preflight(self, target=None, **_options) -> AdmissionTicket:
        return AdmissionTicket(None, "disabled")

    def attach(self, span_or_context=None, source=None, **_options) -> AttachmentHandle:
        return AttachmentHandle("disabled")

    def start_attachment_stream(self, target=None, options=None) -> AttachmentStream:
        return AttachmentStream.denied("disabled")

    def start_recording(self, target=None, options=None) -> RecordingWriter:
        return RecordingWriter(reason="disabled")

    def attach_owned_recording(self, target=None, source=None, **_options) -> OwnedRecordingHandle:
        return OwnedRecordingHandle("denied", "disabled")

    async def import_completed_recording(self, target=None, request=None, **_options) -> ImportHandle:
        # The resolver is never called.
        return ImportHandle("disabled")

    def update_policy(self, server) -> None:
        return None

    def cancel(self, handle) -> None:
        return None

    def flush(self, timeout: float = 5) -> FlushResult:
        return _EMPTY

    async def flush_async(self, timeout: float = 5) -> FlushResult:
        return _EMPTY

    def shutdown(self, timeout: Optional[float] = None) -> FlushResult:
        return _EMPTY

    def __repr__(self) -> str:
        return "DisabledAttachmentManager()"
