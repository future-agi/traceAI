"""Completed-recording import (design section 5, F01 ``mode: completed_recording``).

The core never talks to providers: a resolver turns a provider-specific
completed output into an opaque F01 ``ImportSource`` plus exact declared facts.
The import is submitted with the ORIGINAL eligible identity, never a write
capability. There is no span projection and no background polling.
"""

import asyncio
import threading
from dataclasses import dataclass
from typing import Optional, Protocol, Tuple, Union

from .types import Completion


@dataclass(frozen=True)
class CompletedRecordingRequest:
    provider: str        # Token, e.g. "livekit_egress"
    provider_ref: str    # Token, opaque provider id; never a URL or path
    direction: str = "mixed"


@dataclass(frozen=True)
class ProviderFileSource:
    connection_id: str
    registered_output_id: str


@dataclass(frozen=True)
class EnvelopeSource:
    envelope_id: str     # minted by a trusted server-side importer, never by this SDK


@dataclass(frozen=True)
class ResolvedCompletedRecording:
    source: Union[ProviderFileSource, EnvelopeSource]
    declared_bytes: int
    declared_duration_ms: int
    mime_type: str
    direction: str


class CompletedRecordingResolver(Protocol):
    async def resolve(self, request: CompletedRecordingRequest, *, deadline: float
                      ) -> Union[ResolvedCompletedRecording, str]: ...


class UnavailableResolver:
    """Core default: no provider lookup is available."""

    async def resolve(self, request, *, deadline):
        return "source_unavailable"


class EgressOutputRegistry(Protocol):
    """Injected by the application; the backend (B08) provides the implementation."""

    async def lookup(self, *, connection_id: str, egress_id: str, file_index: int) -> Optional[str]: ...


class ImportHandle:
    """``reason == "queued"`` means submitted and accepted (202) or in flight."""

    def __init__(self, reason: str, recording_id: Optional[str] = None, manager=None):
        self.reason = reason
        self.recording_id = recording_id if reason == "queued" else None
        self.job_id: Optional[str] = None
        self._manager = manager
        self._event = threading.Event()
        self._completion: Optional[Completion] = None
        if reason != "queued":
            self._finish(reason)

    def _finish(self, status: str, job_id: Optional[str] = None) -> None:
        if not self._event.is_set():
            if job_id:
                self.job_id = job_id
            self._completion = Completion(status)
            self._event.set()

    def result(self, timeout: float = 0) -> Completion:
        if timeout and timeout > 0:
            self._event.wait(min(timeout, 30))
        return self._completion or Completion("unfinished")

    async def wait(self, timeout: float = 30.0) -> Completion:
        if self._event.is_set():
            return self._completion
        return await asyncio.to_thread(self.result, timeout)

    async def status(self, *, timeout: float = 2.0) -> Tuple[str, str]:
        """ONE ``GET imports/{job_id}/status`` with the original credentials."""
        if self._manager is None or self.job_id is None:
            return ("failed", self.reason if self.reason != "queued" else "incomplete")
        return await asyncio.to_thread(self._manager._import_status, self.job_id, timeout)

    def __repr__(self) -> str:
        return "ImportHandle(reason=%r)" % self.reason
