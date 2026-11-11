"""Opt-in import of a bound LiveKit session's existing local recording.

This adapter never enables recording. It intentionally supports only the exact
Agents versions exercised by the component suite: 1.8.10 and 1.8.11. The private
SDK lifecycle seam is confined here and fails closed on unknown versions.
"""

import asyncio
import inspect
import math
import os
import stat
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from fi_instrumentation.attachments import (
    CapturePolicy, CorrelationHandle, RecordingFileScope,
)

_SUPPORTED = frozenset(("1.8.10", "1.8.11"))
_BINDING = "_traceai_recording_binding"


def _admission(manager, capture, correlation, *, binding_time=False):
    """The core's public preflight decides; this adapter adds no weaker lattice.

    Nothing is retained: the one-byte ticket is released at once. At bind time
    a still-running bootstrap is not a denial (finalization re-checks).
    """
    if correlation is None:
        return "correlation_unavailable"
    try:
        ticket = manager.preflight(correlation, mime_type="audio/ogg", direction="mixed", max_bytes=1,
                                   operation="import_existing", source_kind="OwnedRecordingFile",
                                   purpose="conversation_recording", capture_policy=capture)
        reason = ticket.reason
        ticket.release()
    except Exception:
        return "source_unavailable"
    if reason == "queued" or (binding_time and reason == "bootstrap_pending"):
        return None
    return reason


def mark_conversation_root(span) -> bool:
    """Mark ``span`` as the conversation root ONLY when it is a true root.

    A true root is a live SDK span with no parent at all (local or remote).
    """
    try:
        from opentelemetry.sdk.trace import Span as SdkSpan
        if not isinstance(span, SdkSpan) or span.parent is not None or not span.is_recording():
            return False
        span.set_attribute("fi.span.kind", "CONVERSATION")
        span.set_attribute("gen_ai.span.kind", "CONVERSATION")
        return True
    except Exception:
        return False


def _native_denial(session, job, processors):
    # Native integration restrictions precede even recording-path inspection.
    # Text redaction never sanitizes binary recordings.
    from livekit.agents.telemetry.utils import allow_pii_from_env
    if allow_pii_from_env() is False or any(p._allow_pii is not True for p in processors):
        return "policy_denied"
    options = session.options.recording_options
    if (options.get("redaction", False) is not False
            or getattr(job, "_redaction_enabled", True) is not False):
        return "redaction_unavailable"
    if options.get("audio") is not True:
        return "policy_denied"
    return None


class SessionRecordingBinding:
    """A content-free status and awaitable, at-most-once finalizer.

    ``validating`` means the bounded file reached quarantine, NOT playable.
    SessionReport files larger than the core's limit remain ``too_large``.
    ``close()`` only detaches this adapter; it never stops the application's
    session, shuts down the shared manager or enables/deletes a recording.
    """

    def __init__(self, session, job_context, manager, *, enabled, timeout, capture):
        self._session = session
        self._job = job_context
        self._manager = manager
        self._capture = capture
        self._enabled = enabled
        self._timeout = timeout
        self._correlation = None
        self._privacy_processors = ()
        self._root = None
        self._original = None
        self._wrapper = None
        self._lock = asyncio.Lock()
        self._finished = False
        self._closed = False
        self._scope = None
        self._handle = None
        self.root_marked = False
        self.status = "session_bound"

    @property
    def handle(self):
        return self._handle

    async def finalize(self):
        """Await delivery before SDK tempdir cleanup, with an explicit deadline."""
        async with self._lock:
            if self._finished or self._closed:
                return self.status
            self._finished = True
            if not self._enabled:
                self.status = "disabled"
                self.close()
                return self.status
            reason = _admission(self._manager, self._capture, self._correlation)
            if reason:
                self.status = reason
                self.close()
                return self.status
            try:
                # All file work is in this finalization callback, never in an
                # inference/audio callback. A bounded source is handed to the
                # manager; no caller stream is read or advanced.
                await asyncio.wait_for(self._capture_report(), timeout=self._timeout)
            except asyncio.TimeoutError:
                self.status = "incomplete"
            except asyncio.CancelledError:
                self.status = "incomplete"
                raise
            except Exception:
                # Do not log repr, report, path, or exception text.
                self.status = "source_unavailable"
            finally:
                if self._handle is not None and self.status != "validating":
                    self._handle.cancel()
                if self._scope is not None:
                    self._scope.close()
                    self._scope = None
                self.close()
            return self.status

    async def _capture_report(self):
        from livekit.agents.voice.recorder_io import RecorderIO

        session, job = self._session, self._job
        reason = _native_denial(session, job, self._privacy_processors)
        if reason:
            self.status = reason
            return
        # The report's path is NOT authority. Only the exact primary session,
        # its SDK recorder and fixed SDK-owned filename under the bound job's
        # directory are eligible. No user-supplied root, LocalFile or URL.
        if job._primary_agent_session is not session:
            self.status = "source_unavailable"
            return
        recorder = session._recorder_io
        if not isinstance(recorder, RecorderIO) or recorder.recording:
            self.status = "source_unavailable"
            return
        expected = self._root / "audio.ogg"
        if recorder.output_path != expected or job.session_directory != self._root:
            self.status = "source_unavailable"
            return
        report = job.make_session_report(session)
        if report.audio_recording_path != expected:
            self.status = "source_unavailable"
            return
        # No root or file stat/open occurs on a policy-denied report.
        reason = _admission(self._manager, self._capture, self._correlation)
        if reason:
            self.status = reason
            return
        scope = RecordingFileScope(str(self._root), enabled=True)
        self._scope = scope
        # Deny-only pre-check (hard links, foreign owner, non-regular files).
        # The scope's no-follow open and identity comparison stay the authority.
        info = os.lstat(self._root / "audio.ogg")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            self.status = "source_unavailable"
            return
        identity = scope.identify("audio.ogg")
        if (identity.device, identity.inode, identity.size) != (info.st_dev, info.st_ino, info.st_size):
            self.status = "source_unavailable"
            return
        source = scope.source("audio.ogg", identity)
        # Encoded multi-channel report: tier ``single``, direction ``mixed`` and
        # no track or role claim (design section 7), the same as TS #685.
        handle = self._manager.attach_owned_recording(
            self._correlation, source, mime_type="audio/ogg", direction="mixed",
            purpose="conversation_recording", capture_policy=self._capture,
        )
        self._handle = handle
        self.status = handle.reason
        if handle.tier != "denied" and handle.reason == "queued":
            # Poll the content-free handle, not a blocking executor waiter.
            # Cancelling finalization must not leave asyncio.run() waiting for
            # a background Event.wait(timeout) during executor shutdown.
            while (result := handle.result()).status == "unfinished":
                await asyncio.sleep(0.01)
            self.status = result.status

    def close(self):
        self._closed = True
        if self._handle is not None and self._handle.result().status == "unfinished":
            self._handle.cancel()
        # Do not overwrite a hook installed by somebody else after this binding.
        if self._wrapper is not None and getattr(self._job, "_on_session_end", None) is self._wrapper:
            self._job._on_session_end = self._original
        if getattr(self._job, _BINDING, None) is self:
            delattr(self._job, _BINDING)
        self._session = self._job = self._root = None
        self._original = self._wrapper = None
        self._privacy_processors = ()


def bind_session(session, job_context, *, manager, owned_recording_files=False,
                 finalization_timeout=30, capture_policy=None):
    """Bind AFTER ``await session.start(...)``; never start a recorder.

    The actual session span is snapshotted now, before LiveKit ends/clears it.
    ``job_context._on_session_end`` is chained, not discarded. The SDK awaits
    this hook after session close and before its tempdir cleanup. User-provided
    AgentServer ``on_session_end`` runs earlier and remains untouched; if it
    deletes the source first, this adapter reports ``source_unavailable``.

    Configure the worker's session-end/shutdown grace to exceed this deadline
    plus the application's own callbacks. Hard process termination is lossy.
    """
    if type(owned_recording_files) is not bool:
        raise ValueError("owned recording permission must be explicit")
    if (type(finalization_timeout) not in (int, float)
            or not math.isfinite(finalization_timeout) or not 0 < finalization_timeout <= 30):
        raise ValueError("finalization deadline must be within 30 seconds")
    capture = capture_policy if capture_policy is not None else CapturePolicy()
    if not isinstance(capture, CapturePolicy) or capture.new_recording:
        raise ValueError("session import does not create a recording")
    previous = getattr(job_context, _BINDING, None)
    if previous is not None:
        if (previous._session is session and previous._manager is manager
                and previous._enabled == owned_recording_files
                and previous._timeout == finalization_timeout and previous._capture == capture):
            return previous
        raise ValueError("conflicting recording binding")
    binding = SessionRecordingBinding(session, job_context, manager, enabled=owned_recording_files,
                                      timeout=finalization_timeout, capture=capture)
    if not owned_recording_files:
        binding.status = "disabled"
        binding.close()
        return binding
    try:
        if version("livekit-agents") not in _SUPPORTED:
            binding.status = "unsupported_version"
            binding.close()
            return binding
        from livekit.agents import AgentSession, JobContext
        if not isinstance(session, AgentSession) or not isinstance(job_context, JobContext):
            binding.status = "unsupported_version"
            binding.close()
            return binding
        # Never fall back to an ambient span from an unrelated late callback.
        span = session._session_span
        if span is None:
            binding.status = "correlation_unavailable"
            binding.close()
            return binding
        binding._correlation = CorrelationHandle.from_span(span)
        from livekit.agents.telemetry.pii import _PIIFilteringSpanProcessor
        processors = getattr(getattr(span, "_span_processor", None), "_span_processors", ())
        binding._privacy_processors = tuple(p for p in processors if isinstance(p, _PIIFilteringSpanProcessor))
        reason = _admission(manager, capture, binding._correlation, binding_time=True)
        if reason:
            binding.status = reason
            binding.close()
            return binding
        if job_context._primary_agent_session is not session:
            binding.status = "source_unavailable"
            binding.close()
            return binding
        reason = _native_denial(session, job_context, binding._privacy_processors)
        if reason:
            binding.status = reason
            binding.close()
            return binding
        binding._root = Path(job_context.session_directory)
        original = job_context._on_session_end
        if not inspect.iscoroutinefunction(original):
            binding.status = "unsupported_version"
            binding.close()
            return binding

        async def chained():
            # Keep original cleanup/report delivery even if instrumentation is
            # cancelled. Ordinary adapter failures are already content-free.
            try:
                await binding.finalize()
            finally:
                await original()

        binding._original, binding._wrapper = original, chained
        job_context._on_session_end = chained
        setattr(job_context, _BINDING, binding)
        # Only a true parentless session span is marked as the Voice root.
        binding.root_marked = mark_conversation_root(span)
    except PackageNotFoundError:
        binding.status = "unsupported_version"
    except Exception:
        binding.status = "source_unavailable"
    if binding.status != "session_bound":
        binding.close()
    return binding
