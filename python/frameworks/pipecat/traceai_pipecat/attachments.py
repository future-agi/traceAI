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
call in Pipecat memory until stop and is refused (``unbounded_source``).
``enable_turn_audio`` is refused the same way unless the caller opts in to
per-turn uploads with explicit ``TurnAudioCaps`` (count, bytes and duration per
call); each turn then uploads once, bound to Pipecat's own turn span.

Short calls: the conversation identity is snapshotted while Pipecat's
conversation span is live (at bind, on ``on_recording_started`` and on every
chunk). Pipecat runs these handlers as tasks, so a call shorter than
``buffer_size`` delivers its only chunk on stop, possibly after the worker has
ended the conversation span; the recording then binds to the snapshot instead
of coming out ``unsampled`` or ``correlation_unavailable``.
"""

import asyncio
import math
import re
import struct
import threading
import time
import weakref
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Optional

from fi_instrumentation.attachments import (
    Bytes, CapturePolicy, CorrelationHandle, PcmFormat, RecordingOptions, TrackSpec,
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

# Hard ceilings for TurnAudioCaps; a caller can only choose lower caps.
_TURN_CEILINGS = {"max_turns": 1000, "max_bytes": 64 * 1024 * 1024, "max_duration_ms": 2 * 60 * 60 * 1000}
_WAV_HEADER = 44
_TURN_SIDES = {"on_user_turn_audio": ("input", "user"), "on_bot_turn_audio": ("output", "assistant")}


@dataclass(frozen=True)
class TurnAudioCaps:
    """Explicit per-call caps for per-turn uploads. There are no defaults.

    ``max_turns`` caps how many turn uploads one call (one binding) admits,
    ``max_bytes`` the summed WAV bytes and ``max_duration_ms`` the summed audio
    duration. A turn that would cross a cap is skipped whole (never truncated):
    ``queue_full`` for the count, ``too_large`` for bytes or duration.
    """

    max_turns: int
    max_bytes: int
    max_duration_ms: int

    def __post_init__(self):
        for name, ceiling in _TURN_CEILINGS.items():
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= ceiling:
                raise ValueError("%s must be an int within 1..%d" % (name, ceiling))


def _wav_header(data_bytes: int, sample_rate: int) -> bytes:
    # Mono 16-bit PCM, the format Pipecat's TurnAudioData carries.
    return b"".join((b"RIFF", struct.pack("<I", 36 + data_bytes), b"WAVE", b"fmt ",
                     struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16),
                     b"data", struct.pack("<I", data_bytes)))


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


def _span_id(span) -> Optional[str]:
    try:
        return format(span.get_span_context().span_id, "016x")
    except Exception:
        return None


def _token(value) -> Optional[str]:
    # Opaque ids that are not F01 tokens are dropped, never rewritten.
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def _probe(manager, operation, directions, capture, purpose="conversation_recording") -> Optional[str]:
    """Deny-wins policy check through the public preflight; retains nothing.

    The sentinel identity only lets preflight run past correlation; the ticket
    is released at once. Returns None when at least one direction is admitted.
    """
    reasons = []
    for direction in directions:
        try:
            ticket = manager.preflight(_SENTINEL, mime_type="audio/wav", direction=direction, max_bytes=1,
                                       operation=operation, source_kind="Bytes", purpose=purpose,
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

    Per-turn uploads (only with ``turn_audio=TurnAudioCaps(...)``) report
    separately: ``turn_status`` is ``off``, ``session_bound``, the last turn's
    admission (``queued`` or a denial reason); ``turn_handles`` holds
    ``(turn_number, direction, AttachmentHandle)`` for each admitted turn (at
    most ``max_turns``) and ``turn_denials`` counts skipped turns by reason.
    """

    def __init__(self, status: str, processor=None, *, manager=None, context=None, layout=None, tracks=None,
                 operation="observe_consumed", capture=None, timeout=30.0, turn_audio=None):
        self.status = status
        self.processor = processor
        self.results = []
        self.root_marked = False
        self.turn_status = "off"
        self.turn_handles = []
        self.turn_denials = {}
        self._manager = manager
        self._context = context
        self._layout = layout
        self._tracks = tracks
        self._operation = operation
        self._capture = capture
        self._timeout = timeout
        self._turn_audio = turn_audio
        self._turn_used = [0, 0, 0]  # uploads, WAV bytes, duration ms admitted so far in this call
        self._writer = None
        self._format = None
        self._cycle_denied = False
        self._snapshot = None  # (CorrelationHandle, span_id, conversation_id) of the live conversation
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

    def _on_recording_started(self, _processor):
        # The conversation span exists by now (the observer starts it on the
        # StartFrame push); remember its identity while it is still live.
        if not self._closed:
            self._remember()

    def _on_recording_stopped(self, _processor):
        try:
            self._finish_cycle()
        except Exception:
            self._fail()

    def _on_user_turn_audio(self, _processor, data):
        self._turn("on_user_turn_audio", data)

    def _on_bot_turn_audio(self, _processor, data):
        self._turn("on_bot_turn_audio", data)

    # ---------------------------------------------------------- internals
    def _fail(self) -> None:
        # Never log the exception: it can carry caller objects. Drop retention.
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.abort()
        self.status = "source_unavailable"

    def _observer(self):
        return getattr(self._context.worker, "turn_trace_observer", None)

    def _remember(self) -> None:
        """Snapshot the observer's conversation identity while its span is live."""
        try:
            if self._context.correlation is not None:
                return
            observer = self._observer()
            # Version-qualified seam (1.15.0): the observer's own conversation span.
            span = getattr(observer, "_conversation_span", None) if observer is not None else None
            if span is None or not span.is_recording():
                return
            if self._snapshot is not None and self._snapshot[1] == _span_id(span):
                return
            correlation = CorrelationHandle.from_span(span)
            self._snapshot = (correlation, correlation.span_id, _token(getattr(observer, "_conversation_id", None)))
            # Admitted binding + live true root: mark it now, because a short
            # call may only deliver audio after the span has ended.
            if correlation.sampled and mark_conversation_root(span):
                self.root_marked = True
        except Exception:
            return

    def _resolve(self):
        context = self._context
        if context.correlation is not None:
            return None, context.correlation, context.span, _token(context.conversation_id)
        observer = self._observer()
        if observer is None:
            return "correlation_unavailable", None, None, None
        span = getattr(observer, "_conversation_span", None)
        snapshot = self._snapshot
        if span is not None and span.is_recording():
            self._remember()
            snapshot = self._snapshot
            if snapshot is not None:
                return None, snapshot[0], span, snapshot[2]
        if snapshot is not None and (span is None or _span_id(span) == snapshot[1]):
            # The worker ended (and cleared) the conversation span before this
            # chunk ran: bind to the identity captured while it was live.
            return None, snapshot[0], None, snapshot[2]
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
        live = None
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

    def _turn(self, event, data) -> None:
        """Upload one finished turn for one side, bound to Pipecat's turn span."""
        try:
            if self._closed or self._turn_audio is None:
                return
            reason = self._turn_upload(event, data)
        except Exception:
            reason = "source_unavailable"
        if reason != "queued":
            self.turn_denials[reason] = self.turn_denials.get(reason, 0) + 1
        self.turn_status = reason

    def _turn_upload(self, event, data) -> str:
        direction, role = _TURN_SIDES[event]
        caps, used = self._turn_audio, self._turn_used
        turn_number, rate = getattr(data, "turn_number", None), getattr(data, "sample_rate", None)
        audio, channels = getattr(data, "audio", None), getattr(data, "num_channels", None)
        if (type(turn_number) is not int or turn_number < 1 or type(rate) is not int or rate <= 0
                or channels != 1 or type(audio) is not bytes or not audio or len(audio) % 2):
            return "source_unavailable"
        # Caps first, on the length alone: an over-cap turn is never copied.
        size = _WAV_HEADER + len(audio)
        duration = math.ceil(len(audio) * 1000 / (2 * rate))
        if used[0] >= caps.max_turns:
            return "queue_full"
        if used[1] + size > caps.max_bytes or used[2] + duration > caps.max_duration_ms:
            return "too_large"
        # Pipecat's own turn span (public API); never the conversation or an ambient span.
        observer = self._observer()
        span_context = observer.get_turn_context(turn_number) if observer is not None else None
        if span_context is None:
            return "correlation_unavailable"
        correlation = CorrelationHandle.from_span(span_context)
        ticket = self._manager.preflight(correlation, mime_type="audio/wav", direction=direction, max_bytes=size,
                                         operation=self._operation, source_kind="Bytes", purpose="turn_recording",
                                         capture_policy=self._capture)
        if not ticket.admitted:
            ticket.release()
            return ticket.reason
        used[0], used[1], used[2] = used[0] + 1, used[1] + size, used[2] + duration
        conversation_id = self._snapshot[2] if self._snapshot is not None else _token(self._context.conversation_id)
        payload = _wav_header(len(audio), rate) + audio
        handle = ticket.attach(Bytes(payload), role=role, conversation_id=conversation_id, turn_id=str(turn_number))
        self.turn_handles.append((turn_number, direction, handle))
        return handle.admission

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
        Per-turn uploads are awaited within the same deadline (``turn_handles``).
        """
        budget = self._timeout if timeout is None else min(max(float(timeout), 0.0), self._timeout)
        deadline = time.monotonic() + budget
        self._finish_cycle()
        pending = list(self._pending)
        if pending:
            await asyncio.wait(pending, timeout=budget + 1)
        # Turn handles complete on the core's workers; poll without a thread each.
        while time.monotonic() < deadline and any(
                handle.result(0).status == "unfinished" for _, _, handle in self.turn_handles):
            await asyncio.sleep(0.02)
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


def _qualified_processor(processor, turn_audio):
    if version("pipecat-ai") not in _SUPPORTED:
        return "unsupported_version"
    from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
    if not isinstance(processor, AudioBufferProcessor):
        return "unsupported_version"
    size = processor._buffer_size
    if type(size) is not int or not 0 < size <= _MAX_BUFFER:
        return "unbounded_source"
    if bool(processor._enable_turn_audio) != (turn_audio is not None):
        if turn_audio is None:
            # Pipecat keeps each turn in memory for its turn events; without
            # explicit caps that is an unbounded source we refuse to observe.
            return "unbounded_source"
        raise ValueError("turn_audio needs AudioBufferProcessor(enable_turn_audio=True)")
    return None


def _validate_turn_audio(turn_audio):
    if turn_audio is not None and not isinstance(turn_audio, TurnAudioCaps):
        raise ValueError("turn_audio must be TurnAudioCaps(max_turns=, max_bytes=, max_duration_ms=)")
    return turn_audio


def _bind(processor, session_context, manager, *, layout, timeout, capture, operation, turn_audio=None):
    if not isinstance(session_context, AudioSessionContext):
        return AudioCaptureBinding("correlation_unavailable")
    try:
        reason = _qualified_processor(processor, turn_audio)
        if reason:
            return AudioCaptureBinding(reason)
        selected, tracks = _layout(processor, layout)
        if selected is None:
            return AudioCaptureBinding("unsupported_version")
        directions = ("input", "output") if selected == "separate_tracks" else ("mixed",)
        reason = _probe(manager, operation, directions, capture)
        if reason:
            return AudioCaptureBinding(reason)
        # Per-turn uploads: admitted only if at least one side may upload a
        # turn_recording; otherwise no turn handler is registered at all.
        turn_reason = None
        if turn_audio is not None:
            turn_reason = _probe(manager, operation, ("input", "output"), capture, purpose="turn_recording")
        with _BINDINGS_LOCK:
            previous = _BINDINGS.get(processor)
            if previous is not None and not previous._closed:
                if (previous._manager is manager and previous._context == session_context
                        and previous._layout == selected and previous._operation == operation
                        and previous._capture == capture and previous._timeout == timeout
                        and previous._turn_audio == turn_audio):
                    return previous
                raise ValueError("conflicting recording binding")
            binding = AudioCaptureBinding("session_bound", processor, manager=manager, context=session_context,
                                          layout=selected, tracks=tracks, operation=operation, capture=capture,
                                          timeout=timeout, turn_audio=turn_audio)
            _BINDINGS[processor] = binding
        if selected == "separate_tracks":
            handlers = (("on_track_audio_data", binding._on_track_audio_data),)
        else:
            handlers = (("on_audio_data", binding._on_audio_data),)
        handlers += (("on_recording_started", binding._on_recording_started),
                     ("on_recording_stopped", binding._on_recording_stopped))
        if turn_audio is not None:
            binding.turn_status = turn_reason or "session_bound"
            if turn_reason is None:
                handlers += (("on_user_turn_audio", binding._on_user_turn_audio),
                             ("on_bot_turn_audio", binding._on_bot_turn_audio))
        for event, handler in handlers:
            processor.add_event_handler(event, handler)
            binding._handlers.append((event, handler))
        binding._remember()
        return binding
    except ValueError:
        raise
    except PackageNotFoundError:
        return AudioCaptureBinding("unsupported_version")
    except Exception:
        return AudioCaptureBinding("source_unavailable")


def bind_audio_processor(processor, session_context, *, manager, layout="auto", finalization_timeout=30,
                         capture_policy=None, turn_audio=None):
    """Observe the app's EXISTING AudioBufferProcessor; never start or modify it.

    Bind before the pipeline runs. ``layout="auto"`` follows the processor:
    mono -> ``mixed``; stereo -> ``stereo`` (user left/input, bot right/output).
    ``layout="separate_tracks"`` observes ``on_track_audio_data`` and keeps one
    track per side, so a hide flag on one side drops only that side.

    ``turn_audio=TurnAudioCaps(max_turns=, max_bytes=, max_duration_ms=)``
    additionally uploads each finished turn (``on_user_turn_audio`` as input,
    ``on_bot_turn_audio`` as output, purpose ``turn_recording``) bound to that
    turn's own Pipecat span via ``get_turn_context(turn_number)``. It is off by
    default and needs ``AudioBufferProcessor(enable_turn_audio=True)`` plus a
    worker with tracing and turn tracking; an explicit-span context has no turn
    spans, so its turns end ``correlation_unavailable``.

    Policy is checked before the processor is inspected, and again by the core
    on every chunk. Disabled attachments return ``disabled`` and register
    nothing. Await ``binding.finish()`` after the pipeline ends.
    """
    capture = _validate_options(layout, finalization_timeout, capture_policy)
    turn_audio = _validate_turn_audio(turn_audio)
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
                 capture=capture, operation="observe_consumed", turn_audio=turn_audio)


def create_audio_capture(session_context, *, manager, sample_rate=None, num_channels=1, buffer_size=32 * 1024,
                         layout="auto", finalization_timeout=30, capture_policy=None, turn_audio=None):
    """Create a NEW, un-started AudioBufferProcessor only with both permissions.

    Needs local ``AttachmentConfig(record_audio=True)`` AND server
    ``record_audio``. The returned ``binding.processor`` must be inserted into
    the pipeline by the application, which also starts it (for example with
    ``AudioBufferStartRecordingFrame``). Nothing is created when denied.
    ``turn_audio`` (``TurnAudioCaps``) uploads turns as in
    ``bind_audio_processor``. The ``turn_recording`` policy is checked BEFORE
    the processor is built: only when at least one side is admitted is it
    created with ``enable_turn_audio=True``. Otherwise Pipecat never buffers
    turns, no turn handler is registered and ``turn_status`` is the reason.
    """
    capture = _validate_options(layout, finalization_timeout, capture_policy)
    turn_audio = _validate_turn_audio(turn_audio)
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
    # Turn policy before construction: a recorder built with enable_turn_audio
    # keeps every turn in Pipecat memory, so build it that way only when at
    # least one side may upload a turn_recording.
    turn_reason = None
    if turn_audio is not None:
        turn_reason = _probe(manager, "create_recording", ("input", "output"), capture, purpose="turn_recording")
    turns = turn_audio if turn_reason is None else None
    try:
        if version("pipecat-ai") not in _SUPPORTED:
            return AudioCaptureBinding("unsupported_version")
        from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
        processor = AudioBufferProcessor(sample_rate=sample_rate, num_channels=num_channels,
                                         buffer_size=buffer_size, enable_turn_audio=turns is not None)
    except PackageNotFoundError:
        return AudioCaptureBinding("unsupported_version")
    except Exception:
        return AudioCaptureBinding("source_unavailable")
    binding = _bind(processor, session_context, manager, layout=layout, timeout=finalization_timeout,
                    capture=capture, operation="create_recording", turn_audio=turns)
    if turn_reason is not None and binding.status == "session_bound":
        binding.turn_status = turn_reason
    return binding
