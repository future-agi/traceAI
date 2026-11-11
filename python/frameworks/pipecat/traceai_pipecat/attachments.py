"""Bounded Pipecat AudioBufferProcessor capture bound to the real conversation.

The adapter only OBSERVES audio the application's own ``AudioBufferProcessor``
already emits (``on_audio_data`` or ``on_track_audio_data``). It never starts,
stops or reconfigures a recorder, never reads the processor's internal buffers
and never falls back to an ambient span. Every PCM chunk goes straight into a
core ``RecordingWriter`` (``start_recording``): segments, gaps and a recording
manifest on a sample-count clock, with the core's own memory bounds.

Recording is OFF unless attachments are enabled. Creating a NEW recorder
(``create_audio_capture``) additionally needs the local and server
``record_audio`` permissions (``operation="create_recording"``); observing an
existing app recorder is ``operation="observe_consumed"``.

Upstream bound: ``buffer_size`` must be within 1..128 KiB, so the processor
flushes bounded chunks on its own schedule. ``buffer_size=0`` keeps the whole
call in Pipecat memory until stop and is refused (``unbounded_source``), as is
``enable_turn_audio`` (per-turn buffers are not observed by this adapter).
"""

import asyncio
import math
import re
import threading
import weakref
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Optional

from fi_instrumentation.attachments import (
    CapturePolicy, CorrelationHandle, PcmFormat, RecordingOptions, TrackSpec,
)

# Exact versions exercised by python/voice-tests on a real pipeline. 1.15.0 is
# the first qualified release with ``on_recording_stopped`` and the
# PipelineWorker ``turn_trace_observer`` surface used here.
_SUPPORTED = frozenset(("1.15.0",))
_MAX_BUFFER = 128 * 1024
_TOKEN = re.compile(r"[a-zA-Z0-9_-]{1,128}")
_SENTINEL = CorrelationHandle("0" * 31 + "1", "0" * 15 + "1", True)
_BINDINGS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_BINDINGS_LOCK = threading.Lock()

CONVERSATION_KIND = "CONVERSATION"


@dataclass(frozen=True)
class AudioSessionContext:
    """The conversation a recording belongs to; never a late ambient lookup.

    ``from_pipeline(worker)`` resolves the conversation span that Pipecat's own
    ``TurnTraceObserver`` started (``PipelineWorker(enable_tracing=True,
    enable_turn_tracking=True)``) when the first audio chunk arrives.
    ``from_span(span)`` binds an explicit span or ``CorrelationHandle``.
    """

    correlation: Optional[CorrelationHandle] = None
    span: object = None
    worker: object = None
    conversation_id: Optional[str] = None

    @classmethod
    def from_span(cls, span, *, conversation_id: Optional[str] = None) -> "AudioSessionContext":
        correlation = CorrelationHandle.from_span(span)
        live = span if hasattr(span, "is_recording") and hasattr(span, "set_attribute") else None
        return cls(correlation=correlation, span=live, conversation_id=conversation_id)

    @classmethod
    def from_pipeline(cls, worker) -> "AudioSessionContext":
        if worker is None:
            raise ValueError("pipeline worker required")
        return cls(worker=worker)


def mark_conversation_root(span) -> bool:
    """Mark ``span`` as the conversation root ONLY when it is a true root.

    A true root is a live SDK span with no parent at all (local or remote).
    A ``SpanContext``/``NonRecordingSpan`` cannot prove root-ness: no marker.
    """
    try:
        from opentelemetry.sdk.trace import Span as SdkSpan
        if not isinstance(span, SdkSpan) or span.parent is not None or not span.is_recording():
            return False
        span.set_attribute("fi.span.kind", CONVERSATION_KIND)
        span.set_attribute("gen_ai.span.kind", CONVERSATION_KIND)
        return True
    except Exception:
        return False


def _token(value) -> Optional[str]:
    # Opaque ids that are not F01 tokens are dropped, never rewritten.
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def _probe(manager, operation, directions, capture) -> Optional[str]:
    """Deny-wins policy check through the public preflight; retains nothing.

    The sentinel identity only lets preflight run past correlation; the ticket
    is released at once. Returns None when at least one direction is admitted.
    """
    reasons = []
    for direction in directions:
        try:
            ticket = manager.preflight(_SENTINEL, mime_type="audio/wav", direction=direction, max_bytes=1,
                                       operation=operation, source_kind="Bytes", purpose="conversation_recording",
                                       capture_policy=capture)
        except Exception:
            return "source_unavailable"
        reason = ticket.reason
        ticket.release()
        # Bootstrap may still be running at bind time; the core re-admits (and
        # records ``bootstrap_pending`` gaps) once the session exists.
        if reason in ("queued", "bootstrap_pending"):
            return None
        reasons.append(reason)
    return reasons[0] if reasons else "policy_denied"


def _layout(processor, layout):
    channels = processor.num_channels
    if layout == "separate_tracks":
        return "separate_tracks", (TrackSpec((0,), "input", "user"), TrackSpec((1,), "output", "assistant"))
    if layout != "auto":
        return None, None
    if channels == 1:
        return "mixed", (TrackSpec((0,), "mixed", "mixed"),)
    if channels == 2:
        # Pipecat's stereo output puts the user on the left, the bot on the right.
        return "stereo", (TrackSpec((0,), "input", "user"), TrackSpec((1,), "output", "assistant"))
    return None, None


class AudioCaptureBinding:
    """Content-free status of one processor binding.

    ``status``: ``session_bound`` (waiting for audio), ``recording``, a denial
    reason, or the last finished ``RecordingResult.state``. ``results`` holds
    each finished recording cycle (one per start/stop of the app's recorder).
    ``close()`` detaches; it never stops the application's recorder.
    """

    def __init__(self, status: str, processor=None, *, manager=None, context=None, layout=None, tracks=None,
                 operation="observe_consumed", capture=None, timeout=30.0):
        self.status = status
        self.processor = processor
        self.results = []
        self.root_marked = False
        self._manager = manager
        self._context = context
        self._layout = layout
        self._tracks = tracks
        self._operation = operation
        self._capture = capture
        self._timeout = timeout
        self._writer = None
        self._format = None
        self._cycle_denied = False
        self._closed = status != "session_bound"
        self._handlers = []
        self._pending = set()

    # ---------------------------------------------------------- handlers
    def _on_audio_data(self, _processor, audio, sample_rate, num_channels):
        try:
            self._write(((audio, 0),), sample_rate, num_channels)
        except Exception:
            self._fail()

    def _on_track_audio_data(self, _processor, user_audio, bot_audio, sample_rate, _num_channels):
        try:
            # Each Pipecat track buffer is mono PCM at the processor rate.
            self._write(((user_audio, 0), (bot_audio, 1)), sample_rate, 1)
        except Exception:
            self._fail()

    def _on_recording_stopped(self, _processor):
        try:
            self._finish_cycle()
        except Exception:
            self._fail()

    # ---------------------------------------------------------- internals
    def _fail(self) -> None:
        # Never log the exception: it can carry caller objects. Drop retention.
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.abort()
        self.status = "source_unavailable"

    def _resolve(self):
        context = self._context
        if context.correlation is not None:
            return None, context.correlation, context.span, _token(context.conversation_id)
        observer = getattr(context.worker, "turn_trace_observer", None)
        if observer is None:
            return "correlation_unavailable", None, None, None
        # Version-qualified seam (1.15.0): the observer's own conversation span.
        span = getattr(observer, "_conversation_span", None)
        if span is None:
            return "correlation_unavailable", None, None, None
        try:
            correlation = CorrelationHandle.from_span(span)
        except Exception:
            return "correlation_unavailable", None, None, None
        return None, correlation, span, _token(getattr(observer, "_conversation_id", None))

    def _open(self, sample_rate, channels):
        if self._cycle_denied:
            return None
        reason, correlation, span, conversation_id = self._resolve()
        if reason is None and not correlation.sampled:
            reason = "unsampled"
        if reason is None:
            options = RecordingOptions(format=PcmFormat(int(sample_rate)), layout=self._layout, tracks=self._tracks,
                                       operation=self._operation, purpose="conversation_recording",
                                       capture_policy=self._capture, conversation_id=conversation_id)
            live = span if span is not None and span.is_recording() else None
            writer = self._manager.start_recording(live if live is not None else correlation, options)
            reason = writer.reason if writer.reason != "queued" else None
        if reason is not None:
            self._cycle_denied = True
            self.status = reason
            return None
        if live is not None and mark_conversation_root(live):
            self.root_marked = True
        self._writer, self._format = writer, (int(sample_rate), channels)
        self.status = "recording"
        return writer

    def _write(self, chunks, sample_rate, channels):
        if self._closed:
            return
        expected = 2 if self._layout == "stereo" else 1
        writer = self._writer
        if writer is None:
            if type(sample_rate) is not int or channels != expected:
                return
            writer = self._open(sample_rate, channels)
            if writer is None:
                return
        if (sample_rate, channels) != self._format:
            # Format drift: keep the shared clock with an explicit gap.
            size = max(len(chunk) for chunk, _ in chunks)
            if type(sample_rate) is int and sample_rate > 0 and channels in (1, 2):
                duration = math.ceil(size * 1000 / (2 * channels * sample_rate))
                if duration > 0:
                    writer.mark_gap(duration, reason="source_unavailable")
            return
        for chunk, track in chunks:
            writer.try_write(chunk, track=track if self._layout == "separate_tracks" else 0)

    def _finish_cycle(self):
        writer, self._writer = self._writer, None
        self._format, self._cycle_denied = None, False
        if writer is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._record(writer.finish(self._timeout))
            return
        task = loop.create_task(writer.finish_async(self._timeout))
        self._pending.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task):
        self._pending.discard(task)
        if task.cancelled():
            self.status = "incomplete"
            return
        error = task.exception()
        if error is not None:
            self.status = "source_unavailable"
            return
        self._record(task.result())

    def _record(self, result):
        self.results.append(result)
        self.status = result.state

    # ------------------------------------------------------------- public
    async def finish(self, timeout: Optional[float] = None):
        """Finish the open recording cycle and await uploads, within a deadline.

        Call before process exit (after the pipeline ends). Pipecat already
        finishes a cycle on ``on_recording_stopped``; this only awaits it.
        Returns the last ``RecordingResult`` or None. A hard kill is lossy.
        """
        budget = self._timeout if timeout is None else min(max(float(timeout), 0.0), self._timeout)
        self._finish_cycle()
        pending = list(self._pending)
        if pending:
            await asyncio.wait(pending, timeout=budget + 1)
        return self.results[-1] if self.results else None

    def close(self) -> None:
        """Detach handlers and drop any unfinished cycle (no I/O)."""
        self._closed = True
        processor = self.processor
        for event, handler in self._handlers:
            try:
                processor.remove_event_handler(event, handler)
            except Exception:
                pass
        self._handlers = []
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.abort()
            self.status = "incomplete"
        if processor is not None:
            with _BINDINGS_LOCK:
                if _BINDINGS.get(processor) is self:
                    del _BINDINGS[processor]

    def __repr__(self) -> str:
        return "AudioCaptureBinding(status=%r)" % self.status


def _validate_options(layout, finalization_timeout, capture_policy):
    if layout not in ("auto", "separate_tracks"):
        raise ValueError("layout must be 'auto' or 'separate_tracks'")
    if (type(finalization_timeout) not in (int, float) or not math.isfinite(finalization_timeout)
            or not 0 < finalization_timeout <= 30):
        raise ValueError("finalization deadline must be within 30 seconds")
    capture = capture_policy if capture_policy is not None else CapturePolicy()
    if not isinstance(capture, CapturePolicy):
        raise ValueError("capture policy must be a CapturePolicy")
    return capture


def _qualified_processor(processor):
    if version("pipecat-ai") not in _SUPPORTED:
        return "unsupported_version"
    from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
    if not isinstance(processor, AudioBufferProcessor):
        return "unsupported_version"
    size = processor._buffer_size
    if type(size) is not int or not 0 < size <= _MAX_BUFFER or processor._enable_turn_audio:
        return "unbounded_source"
    return None


def _bind(processor, session_context, manager, *, layout, timeout, capture, operation):
    if not isinstance(session_context, AudioSessionContext):
        return AudioCaptureBinding("correlation_unavailable")
    try:
        reason = _qualified_processor(processor)
        if reason:
            return AudioCaptureBinding(reason)
        selected, tracks = _layout(processor, layout)
        if selected is None:
            return AudioCaptureBinding("unsupported_version")
        directions = ("input", "output") if selected == "separate_tracks" else ("mixed",)
        reason = _probe(manager, operation, directions, capture)
        if reason:
            return AudioCaptureBinding(reason)
        with _BINDINGS_LOCK:
            previous = _BINDINGS.get(processor)
            if previous is not None and not previous._closed:
                if (previous._manager is manager and previous._context == session_context
                        and previous._layout == selected and previous._operation == operation
                        and previous._capture == capture and previous._timeout == timeout):
                    return previous
                raise ValueError("conflicting recording binding")
            binding = AudioCaptureBinding("session_bound", processor, manager=manager, context=session_context,
                                          layout=selected, tracks=tracks, operation=operation, capture=capture,
                                          timeout=timeout)
            _BINDINGS[processor] = binding
        if selected == "separate_tracks":
            handlers = (("on_track_audio_data", binding._on_track_audio_data),)
        else:
            handlers = (("on_audio_data", binding._on_audio_data),)
        handlers += (("on_recording_stopped", binding._on_recording_stopped),)
        for event, handler in handlers:
            processor.add_event_handler(event, handler)
            binding._handlers.append((event, handler))
        return binding
    except ValueError:
        raise
    except PackageNotFoundError:
        return AudioCaptureBinding("unsupported_version")
    except Exception:
        return AudioCaptureBinding("source_unavailable")


def bind_audio_processor(processor, session_context, *, manager, layout="auto", finalization_timeout=30,
                         capture_policy=None):
    """Observe the app's EXISTING AudioBufferProcessor; never start or modify it.

    Bind before the pipeline runs. ``layout="auto"`` follows the processor:
    mono -> ``mixed``; stereo -> ``stereo`` (user left/input, bot right/output).
    ``layout="separate_tracks"`` observes ``on_track_audio_data`` and keeps one
    track per side, so a hide flag on one side drops only that side.

    Policy is checked before the processor is inspected, and again by the core
    on every chunk. Disabled attachments return ``disabled`` and register
    nothing. Await ``binding.finish()`` after the pipeline ends.
    """
    capture = _validate_options(layout, finalization_timeout, capture_policy)
    if capture.new_recording:
        raise ValueError("observing an existing recorder does not create a recording")
    try:
        state = manager.status().state
    except Exception:
        return AudioCaptureBinding("source_unavailable")
    if state == "disabled":
        return AudioCaptureBinding("disabled")
    reason = _probe(manager, "observe_consumed", ("input", "output", "mixed"), capture)
    if reason:
        return AudioCaptureBinding(reason)
    return _bind(processor, session_context, manager, layout=layout, timeout=finalization_timeout,
                 capture=capture, operation="observe_consumed")


def create_audio_capture(session_context, *, manager, sample_rate=None, num_channels=1, buffer_size=32 * 1024,
                         layout="auto", finalization_timeout=30, capture_policy=None):
    """Create a NEW, un-started AudioBufferProcessor only with both permissions.

    Needs local ``AttachmentConfig(record_audio=True)`` AND server
    ``record_audio``. The returned ``binding.processor`` must be inserted into
    the pipeline by the application, which also starts it (for example with
    ``AudioBufferStartRecordingFrame``). Nothing is created when denied.
    """
    capture = _validate_options(layout, finalization_timeout, capture_policy)
    if (type(buffer_size) is not int or not 0 < buffer_size <= _MAX_BUFFER
            or num_channels not in (1, 2)):
        raise ValueError("buffer_size must be within 1..131072 and num_channels 1 or 2")
    try:
        state = manager.status().state
    except Exception:
        return AudioCaptureBinding("source_unavailable")
    if state == "disabled":
        return AudioCaptureBinding("disabled")
    directions = ("input", "output") if layout == "separate_tracks" else ("mixed",)
    reason = _probe(manager, "create_recording", directions, capture)
    if reason:
        return AudioCaptureBinding(reason)
    if not isinstance(session_context, AudioSessionContext):
        return AudioCaptureBinding("correlation_unavailable")
    try:
        if version("pipecat-ai") not in _SUPPORTED:
            return AudioCaptureBinding("unsupported_version")
        from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
        processor = AudioBufferProcessor(sample_rate=sample_rate, num_channels=num_channels,
                                         buffer_size=buffer_size, enable_turn_audio=False)
    except PackageNotFoundError:
        return AudioCaptureBinding("unsupported_version")
    except Exception:
        return AudioCaptureBinding("source_unavailable")
    return _bind(processor, session_context, manager, layout=layout, timeout=finalization_timeout,
                 capture=capture, operation="create_recording")
