"""Live PCM recordings (design section 2): WAV segments, gaps and manifests.

Timing is a sample-count clock, never wall time. Every segment is at most
30 s and 8 MiB including its 44-byte header; all tracks share the same cut
points. Segments upload strictly in order (one in flight per recording), with
a moving window of sealed-but-not-uploaded segments. Manifests are cumulative
and prefix-monotonic, authorized by the latest completed segment's capability.
``try_write`` is synchronous, never does I/O, and never raises.
"""

import json
import math
import threading
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .sources import wav_header
from .types import GAP_REASONS, TRACK_ROLES

MAX_DURATION_MS = 7200000
MAX_SEGMENT_BYTES = 8 * 1024 * 1024
MAX_SEGMENT_MS = 30000
MAX_PLAYOUT = 4096


@dataclass(frozen=True)
class PcmFormat:
    sample_rate_hz: int
    sample_format: str = "s16le"


@dataclass(frozen=True)
class TrackSpec:
    channels: Tuple[int, ...]
    direction: str
    role: str


@dataclass(frozen=True)
class RecordingOptions:
    format: PcmFormat
    layout: str
    tracks: Tuple[TrackSpec, ...]
    operation: str
    purpose: str = "conversation_recording"
    capture_policy: Optional[object] = None
    conversation_id: Optional[str] = None
    turn_id: Optional[str] = None
    window_segments: int = 2


@dataclass(frozen=True)
class RecordingResult:
    recording_id: str
    state: str                 # published | partial | unpublished | denied
    reason: Optional[str]
    segments_published: int
    gap_ms: int
    capture_complete: bool
    manifest_revision: int


@dataclass(frozen=True)
class RecordingStatus:
    state: str                 # pending | recording | truncated | terminal | finished | denied
    reason: str
    segments_published: int
    segments_pending: int
    gap_ms: int
    manifest_revision: int


def boundary_step(rate: int) -> int:
    return rate // math.gcd(rate, 1000)


def segment_frames(rate: int, frame_bytes: int, seg_ms: int, seg_bytes: int) -> int:
    """Largest multiple of g within both the duration and the byte bound (2.5)."""
    g = boundary_step(rate)
    frames = min(seg_ms * rate // 1000, (seg_bytes - 44) // frame_bytes)
    frames -= frames % g
    return frames if frames >= g else 0


def to_ms(frames: int, rate: int, final: bool = False) -> int:
    value = frames * 1000
    return -(-value // rate) if final else value // rate


def validate_options(options: RecordingOptions) -> Optional[str]:
    """Local shape rules; returns a denial reason or None."""
    fmt = options.format
    if (not isinstance(fmt, PcmFormat) or type(fmt.sample_rate_hz) is not int
            or not 8000 <= fmt.sample_rate_hz <= 192000 or fmt.sample_format != "s16le"):
        return "source_unavailable"
    if options.layout not in ("mixed", "stereo", "separate_tracks") or options.operation not in (
            "observe_consumed", "create_recording"):
        return "policy_denied"
    if options.purpose not in ("conversation_recording", "turn_recording"):
        return "policy_denied"
    tracks = tuple(options.tracks)
    if not 1 <= len(tracks) <= 32 or not 1 <= options.window_segments <= 4:
        return "policy_denied"
    channels = []
    for track in tracks:
        if (not isinstance(track, TrackSpec) or track.direction not in ("input", "output", "mixed")
                or track.role not in TRACK_ROLES or not track.channels):
            return "policy_denied"
        if any(type(c) is not int or not 0 <= c <= 31 for c in track.channels):
            return "policy_denied"
        channels.extend(track.channels)
    if sorted(channels) != list(range(len(channels))):
        return "policy_denied"
    if options.layout == "mixed" and (len(tracks) != 1 or tracks[0].direction != "mixed"):
        return "policy_denied"
    if options.layout == "stereo" and (len(tracks) != 2 or len(channels) != 2):
        return "policy_denied"
    return None


def build_manifest(recording_id: str, rate: int, layout: str, tracks: list, segments: list, gaps: list,
                   playout: list, duration_ms: int, capture_complete: bool) -> dict:
    return {
        "schema_version": 1, "recording_id": recording_id, "timebase": "session_relative_ms",
        "sample_format": "s16le", "sample_rate_hz": rate,
        "channels": sum(len(t["channels"]) for t in tracks), "codec": "pcm", "container": "wav",
        "layout": layout, "tracks": tracks, "segments": segments, "gaps": gaps, "playout": playout,
        "capture_complete": bool(capture_complete and not gaps and segments), "duration_ms": duration_ms,
    }


class SegmentSource:
    """Header + retained frames without concatenating them."""

    def __init__(self, header: bytes, frames: bytes):
        self._header, self._frames = header, frames

    def __len__(self) -> int:
        return 44 + len(self._frames)

    def read(self, offset: int, size: int) -> bytes:
        result = b""
        if offset < 44:
            result = self._header[offset:min(44, offset + size)]
            size -= len(result)
            offset = 44
        if size > 0:
            result += self._frames[offset - 44:offset - 44 + size]
        return result

    def check(self) -> None:
        return None


class _Entry:
    __slots__ = ("occurrence_id", "attachment_id", "f0", "f1", "track_id", "state", "index", "upload",
                 "body", "fail", "nbytes", "source")

    def __init__(self, f0, f1, track_id, source, nbytes):
        self.occurrence_id, self.attachment_id = str(uuid.uuid4()), str(uuid.uuid4())
        self.f0, self.f1, self.track_id = f0, f1, track_id
        self.state, self.index, self.upload, self.body, self.fail = "pending", None, None, None, None
        self.source, self.nbytes = source, nbytes


class _Window:
    __slots__ = ("f0", "f1", "entries", "gaps")

    def __init__(self, f0, f1):
        self.f0, self.f1, self.entries, self.gaps = f0, f1, [], []

    def settled(self) -> bool:
        return all(e.state != "pending" for e in self.entries)


class _Stream:
    """One PCM input: the whole interleaved stream (mixed/stereo) or one track."""

    __slots__ = ("track_id", "frame_bytes", "carry", "cursor", "run_start", "run", "intervals", "denied",
                 "direction", "channels")

    def __init__(self, track_id, channels, direction):
        self.track_id, self.channels, self.direction = track_id, channels, direction
        self.frame_bytes = channels * 2
        self.carry, self.cursor, self.run_start, self.run = b"", 0, None, bytearray()
        self.intervals, self.denied = [], None


class RecordingWriter:
    """Created by ``AttachmentManager.start_recording``. Inert unless ``reason == "queued"``."""

    def __init__(self, manager=None, options: Optional[RecordingOptions] = None, reason: str = "queued", *,
                 correlation=None, pending: bool = False, track_denials: Optional[List[Optional[str]]] = None):
        self.recording_id = str(uuid.uuid4())
        self.reason = reason
        self._manager = manager
        self._options = options
        self._lock = threading.Lock()
        self._closed = reason != "queued"
        self._state = "denied" if reason != "queued" else ("pending" if pending else "recording")
        self._result: Optional[RecordingResult] = None
        if self._closed:
            return
        self._correlation = correlation
        self._epoch = manager._epoch
        session = manager._session_or_none()
        limits = manager._limits()
        layout = options.layout
        self._rate = options.format.sample_rate_hz
        self._g = boundary_step(self._rate)
        self._tracks = [{"track_id": str(uuid.uuid4()), "channels": list(t.channels), "direction": t.direction,
                         "role": t.role} for t in options.tracks]
        if layout == "separate_tracks":
            self._streams = [_Stream(t["track_id"], len(t["channels"]), t["direction"]) for t in self._tracks]
        else:
            direction = "mixed"
            self._streams = [_Stream(None, sum(len(t["channels"]) for t in self._tracks), direction)]
        for stream, denial in zip(self._streams, track_denials or []):
            stream.denied = denial
        seg_ms = min(limits.segment_duration_ms, MAX_SEGMENT_MS)
        seg_bytes = min(limits.segment_bytes, limits.max_upload_bytes, MAX_SEGMENT_BYTES)
        self._frames = min(segment_frames(self._rate, s.frame_bytes, seg_ms, seg_bytes) for s in self._streams)
        self._max_bytes = min(limits.recording_bytes, 1073741824)
        self._max_frames = min(limits.recording_duration_ms, MAX_DURATION_MS) * self._rate // 1000
        self._ceiling_frames = MAX_DURATION_MS * self._rate // 1000
        self._window = _Window(0, self._frames)
        self._windows: List[_Window] = []
        self._retained = 0
        # Cumulative bytes this recording has sealed into segments, 44-byte WAV
        # headers included. Unlike _retained (memory still held), it never goes
        # down when a segment uploads: it is what the recording_bytes cap limits.
        self._sealed_bytes = 0
        self._completed = 0
        self._published_prefix = 0      # frames covered by the last successful manifest
        self._revision = 0
        self._last_put_completed = 0
        self._manifest_pending = False
        self._playout: list = []
        self._playout_dropped = 0
        self._truncated = False
        self._fallback = False
        self._abort_index: Optional[int] = None
        self._closing_partial = False
        self._final_end: Optional[int] = None
        self._lane = manager._new_lane(self)
        self._session_ref = session
        del session
        if self._frames == 0:
            self._deny("too_large")

    # ----------------------------------------------------------- helpers
    def _deny(self, reason: str) -> None:
        self.reason, self._state, self._closed = reason, "denied", True
        if self._manager is not None:
            self._manager._close_recording(self)

    @property
    def _open(self) -> bool:
        return not self._closed

    def _frame_end(self, stream: _Stream, frames: int) -> int:
        return min(stream.cursor + frames, self._ceiling_frames)

    def _close_run(self, stream: _Stream) -> None:
        if stream.run_start is not None:
            data = bytes(stream.run)
            stream.intervals.append(("data", stream.run_start, stream.cursor, data))
            stream.run_start, stream.run = None, bytearray()

    def _add_gap(self, stream: _Stream, frames: int, reason: str) -> None:
        """Advance the clock by a gap, splitting at window boundaries; coalesce within a window."""
        self._close_run(stream)
        reason = reason if reason in GAP_REASONS else "incomplete"
        while frames > 0 and stream.cursor < self._ceiling_frames:
            step = min(frames, self._window.f1 - stream.cursor, self._ceiling_frames - stream.cursor)
            if step <= 0:
                self._seal()
                continue
            last = stream.intervals[-1] if stream.intervals else None
            if last is not None and last[0] == "gap" and last[3] == reason and last[2] == stream.cursor:
                stream.intervals[-1] = ("gap", last[1], stream.cursor + step, reason)
            else:
                stream.intervals.append(("gap", stream.cursor, stream.cursor + step, reason))
            stream.cursor += step
            frames -= step
            if stream.cursor >= self._window.f1:
                self._seal()

    def _add_data(self, stream: _Stream, view: memoryview) -> None:
        frames = len(view) // stream.frame_bytes
        offset = 0
        while frames > 0:
            step = min(frames, self._window.f1 - stream.cursor)
            if step <= 0:
                self._seal()
                continue
            if stream.run_start is None:
                stream.run_start = stream.cursor
            stream.run += view[offset:offset + step * stream.frame_bytes]
            offset += step * stream.frame_bytes
            stream.cursor += step
            frames -= step
            if stream.cursor >= self._window.f1:
                self._seal()

    def _seal(self, final_end: Optional[int] = None) -> None:
        """Seal the current window at its end (or at ``final_end``) for every stream."""
        window = self._window
        end = window.f1 if final_end is None else final_end
        for stream in self._streams:
            if stream.cursor < end:
                self._close_run(stream)
                stream.intervals.append(("gap", stream.cursor, end, "source_unavailable"))
                stream.cursor = end
            self._close_run(stream)
        window.f1 = end
        final = final_end is not None
        items = []
        for ordinal, stream in enumerate(self._streams):
            for interval in stream.intervals:
                f0, f1 = interval[1], interval[2]
                if to_ms(f1, self._rate, final and f1 == end) - to_ms(f0, self._rate) <= 0:
                    if interval[0] == "data":
                        self._manager._release(len(interval[3]), 0)
                        self._retained -= len(interval[3])
                    continue
                items.append((f0, ordinal, interval, stream))
            stream.intervals = []
        for f0, _, interval, stream in sorted(items, key=lambda item: (item[0], item[1])):
            if interval[0] == "data":
                frames = interval[2] - interval[1]
                header = wav_header(stream.channels, self._rate, frames)
                entry = _Entry(interval[1], interval[2], stream.track_id, SegmentSource(header, interval[3]),
                               len(interval[3]))
                window.entries.append(entry)
                self._sealed_bytes += 44 + len(interval[3])
            else:
                window.gaps.append((interval[1], interval[2], interval[3], stream.track_id))
        self._windows.append(window)
        for entry in window.entries:
            self._manager._enqueue_segment(self, entry)
        self._window = _Window(end, end + self._frames)

    def _projected_bytes(self, stream: _Stream, nbytes: int, frames: int) -> int:
        """Total recording bytes if ``nbytes`` more were accepted on ``stream``.

        Sealed segments plus every unsealed data run (each will become a segment
        with its own 44-byte header), plus a header for each new segment the part
        would start: one when the stream has no open run, and one per window
        boundary the part crosses.
        """
        total = self._sealed_bytes + nbytes
        for s in self._streams:
            if s.run_start is not None:
                total += 44 + len(s.run)
            for interval in s.intervals:
                if interval[0] == "data":
                    total += 44 + len(interval[3])
        if stream.run_start is None:
            total += 44
        beyond = stream.cursor + frames - self._window.f1
        if beyond > 0 and self._frames:
            total += 44 * (-(-beyond // self._frames))
        return total

    def _pending_segments(self) -> int:
        return sum(1 for w in self._windows for e in w.entries if e.state == "pending")

    def _check_admission(self, stream: _Stream) -> Optional[str]:
        """Returns a gap reason when these frames cannot be retained."""
        manager = self._manager
        if self._state == "pending":
            state = manager._runtime_state()
            if state == "bootstrap_pending":
                return "bootstrap_pending"
            denials = manager._recording_denials(self._options) if state == "ready" else None
            if denials is None or all(denials):
                self._deny_terminal_pending(state if denials is None else next(d for d in denials if d))
                return "denied"
            for s, denial in zip(self._streams, denials):
                s.denied = denial
            self._state, self._epoch = "recording", manager._epoch
        if self._truncated:
            return "truncated"
        if self._state == "terminal":
            return self.reason if self.reason in GAP_REASONS else "incomplete"
        if manager._epoch != self._epoch:
            denials = manager._recording_denials(self._options)
            self._epoch = manager._epoch
            if denials is None or all(denials):
                self._terminal("policy_denied")
                return "policy_denied"
            for s, denial in zip(self._streams, denials):
                if denial and not s.denied:
                    s.denied = denial
                    self._drop_run(s, "policy_denied")
        if stream.denied:
            return "policy_denied"
        if manager._runtime_state() == "expired":
            return "incomplete"
        return None

    def _deny_terminal_pending(self, reason: str) -> None:
        # Denied after pending: no manifest is ever published; counters discarded.
        self.reason, self._state, self._closed = reason, "denied", True
        for stream in self._streams:
            stream.intervals, stream.run, stream.run_start = [], bytearray(), None
        self._manager._close_recording(self)

    def _drop_run(self, stream: _Stream, reason: str) -> None:
        if stream.run_start is not None:
            frames = stream.cursor - stream.run_start
            self._manager._release(len(stream.run), 0)
            self._retained -= len(stream.run)
            stream.cursor = stream.run_start
            stream.run_start, stream.run = None, bytearray()
            self._add_gap(stream, frames, reason)

    def _terminal(self, reason: str) -> None:
        """Stop retaining: free every unsealed byte and turn it into a gap of ``reason``."""
        self.reason, self._state = reason, "terminal"
        gap = reason if reason in GAP_REASONS else "incomplete"
        for stream in self._streams:
            self._drop_run(stream, gap)
            kept = []
            for interval in stream.intervals:
                if interval[0] == "data":
                    self._manager._release(len(interval[3]), 0)
                    self._retained -= len(interval[3])
                    interval = ("gap", interval[1], interval[2], gap)
                if kept and kept[-1][0] == "gap" and kept[-1][3] == interval[3] and kept[-1][2] == interval[1]:
                    kept[-1] = ("gap", kept[-1][1], interval[2], interval[3])
                else:
                    kept.append(interval)
            stream.intervals = kept

    # ------------------------------------------------------------- public
    def try_write(self, pcm, *, track: int = 0) -> bool:
        try:
            view = memoryview(pcm).cast("B")
            with self._lock:
                if self._closed or type(track) is not int or not 0 <= track < len(self._streams):
                    return False
                manager = self._manager
                if manager._stopping or manager._stopped:
                    return False
                stream = self._streams[track]
                pieces = []
                if stream.carry:
                    need = stream.frame_bytes - len(stream.carry)
                    if len(view) < need:
                        stream.carry += bytes(view)
                        return True
                    pieces.append(memoryview(stream.carry + bytes(view[:need])))
                    view = view[need:]
                    stream.carry = b""
                whole = len(view) - len(view) % stream.frame_bytes
                if whole != len(view):
                    stream.carry = bytes(view[whole:])
                pieces.append(view[:whole])
                step = max(manager.config.chunk_bytes - manager.config.chunk_bytes % stream.frame_bytes,
                           stream.frame_bytes)
                retained = True
                for piece in pieces:
                    for offset in range(0, len(piece), step):
                        part = piece[offset:offset + step]
                        if not self._accept(stream, part):
                            retained = False
                        if self._closed:
                            return False
                return retained
        except Exception:
            return False

    def _accept(self, stream: _Stream, part: memoryview) -> bool:
        frames = len(part) // stream.frame_bytes
        if not frames:
            return True
        reason = self._check_admission(stream)
        if reason == "denied":
            return False
        if reason is None and (stream.cursor + frames > self._max_frames
                               or self._projected_bytes(stream, len(part), frames) > self._max_bytes):
            self._truncated = True
            for s in self._streams:
                self._close_run(s)
            reason = "truncated"
        if reason is None and self._pending_segments() >= self._options.window_segments * len(self._streams):
            reason = "queue_full"
        if reason is None and not self._manager._reserve(len(part)):
            reason = "queue_full"
        if reason is not None:
            self._add_gap(stream, frames, reason)
            return False
        self._retained += len(part)
        self._add_data(stream, part)
        return True

    def mark_gap(self, duration_ms: int, *, reason: str = "source_unavailable", track: Optional[int] = None) -> None:
        try:
            with self._lock:
                if self._closed or type(duration_ms) is not int or duration_ms <= 0:
                    return
                frames = -(-duration_ms * self._rate // 1000)
                frames += (-frames) % self._g
                streams = self._streams if track is None else [self._streams[track]]
                for stream in streams:
                    self._add_gap(stream, frames, reason)
        except Exception:
            return

    def mark_playout(self, start_ms: int, duration_ms: int, evidence: str) -> None:
        try:
            with self._lock:
                if self._closed or evidence not in ("generated", "played", "unknown", "truncated"):
                    return
                if type(start_ms) is not int or type(duration_ms) is not int or start_ms < 0 or duration_ms <= 0:
                    return
                if len(self._playout) >= MAX_PLAYOUT:
                    self._playout_dropped += 1
                    return
                self._playout.append({"start_ms": start_ms, "duration_ms": duration_ms, "evidence": evidence})
        except Exception:
            return

    # ------------------------------------------------- worker callbacks
    def _next_index(self) -> int:
        with self._lock:
            return self._completed

    def _segment_done(self, entry: _Entry, status: str, upload=None, body=None, initialized: bool = False,
                      index: Optional[int] = None) -> None:
        with self._lock:
            self._retained -= entry.nbytes
            entry.source = None
            if status == "validating":
                entry.state, entry.index, entry.upload, entry.body = "completed", index, upload, body
                self._completed += 1
                self._abort_index = None
            else:
                entry.state, entry.fail = "failed", status
                if status == "conflict" and index is not None and self._abort_index == index:
                    self._closing_partial = True
                    self._terminal("incomplete")
                elif initialized and index is not None:
                    self._abort_index = index
            self._maybe_manifest()

    def _settled_end(self) -> int:
        end = 0
        for window in self._windows:
            if not window.settled():
                break
            end = window.f1
        return end

    def _maybe_manifest(self) -> None:
        # Caller holds self._lock.
        if self._closed or self._manifest_pending or self._fallback:
            return
        end = self._settled_end()
        if end <= self._published_prefix or not self._completed:
            return
        if (self._completed - self._last_put_completed >= 4
                or to_ms(end, self._rate) - to_ms(self._published_prefix, self._rate) >= 30000):
            body = self._manifest_body(end, final=False)
            if body is not None:
                self._manifest_pending = True
                self._revision += 1
                self._manager._enqueue_manifest(self, body, self._revision, end, self._completed)

    def _capability_entries(self) -> list:
        return [e for w in self._windows for e in w.entries if e.state == "completed" and e.upload is not None]

    def _manifest_body(self, end: int, *, final: bool, drop_unsettled: bool = False) -> Optional[bytes]:
        """Cumulative manifest over the settled prefix (or everything when final)."""
        rate = self._rate
        segments, gaps = [], []

        def interval(f0, f1):
            start = to_ms(f0, rate)
            return start, to_ms(f1, rate, final and f1 == end) - start

        for window in self._windows:
            if window.f1 > end:
                break
            settled = window.settled()
            if not settled and not final:
                break
            for f0, f1, reason, track_id in window.gaps:
                start, duration = interval(f0, f1)
                if duration > 0:
                    gap = {"start_ms": start, "duration_ms": duration, "reason": reason}
                    if track_id:
                        gap["track_id"] = track_id
                    gaps.append(gap)
            for entry in window.entries:
                start, duration = interval(entry.f0, entry.f1)
                if duration <= 0:
                    continue
                if entry.state == "completed":
                    segment = {"occurrence_id": entry.occurrence_id, "index": entry.index, "start_ms": start,
                               "duration_ms": duration}
                    if entry.track_id:
                        segment["track_id"] = entry.track_id
                    segments.append(segment)
                else:
                    reason = "policy_denied" if entry.fail == "policy_denied" else "incomplete"
                    gap = {"start_ms": start, "duration_ms": duration, "reason": reason}
                    if entry.track_id:
                        gap["track_id"] = entry.track_id
                    gaps.append(gap)
        if not segments:
            return None
        segments.sort(key=lambda s: s["index"])
        gaps.sort(key=lambda g: (g["start_ms"], g.get("track_id", "")))
        duration = to_ms(end, rate, final)
        playout = [p for p in self._playout if p["start_ms"] + p["duration_ms"] <= duration]
        manifest = build_manifest(self.recording_id, rate, self._options.layout, self._tracks, segments, gaps,
                                  playout, duration, final)
        return json.dumps(manifest, separators=(",", ":")).encode()

    def _manifest_done(self, ok: bool, revision: int, end: int, completed: int, fallback: bool = False) -> None:
        with self._lock:
            self._manifest_pending = False
            if ok:
                self._published_prefix = max(self._published_prefix, end)
                self._last_put_completed = completed
                self._last_ok_revision = revision
            elif fallback:
                self._fallback = True
            self._maybe_manifest()

    # ------------------------------------------------------- finish/abort
    def finish(self, timeout: float = 30.0) -> RecordingResult:
        manager = self._manager
        if self._state == "denied" or manager is None:
            return self._finish_result("denied", self.reason)
        try:
            budget = min(max(float(timeout), 0.0), manager.config.finalization_timeout)
        except Exception:
            budget = 0.0
        deadline = manager._bounded_deadline(time.monotonic() + budget)
        with self._lock:
            if self._result is not None:
                return self._result
            if self._closed and self._state == "aborted":
                return self._finish_result(*self._aborted_state())
            pending_only = self._state == "pending"
            self._closed = True
            if not pending_only:
                end = max(s.cursor for s in self._streams)
                if end > self._window.f0 or any(s.run_start is not None or s.intervals for s in self._streams):
                    self._seal(final_end=end)
                self._final_end = end
        if pending_only:
            manager._close_recording(self)
            return self._finish_result("unpublished", "bootstrap_pending")
        reserve = min(manager.config.request_timeout, max(deadline - time.monotonic(), 0.0) / 2)
        manager._drain_lane(self._lane, deadline - reserve)
        manager._cancel_lane(self._lane, "incomplete")
        with self._lock:
            end = self._final_end or 0
            body = self._manifest_body(end, final=True)
            self._revision += 1
            revision = self._revision
            candidates = self._capability_entries()
        ok = False
        if body is not None and candidates:
            ok = manager._put_manifest_now(self, body, revision, candidates, deadline)
        with self._lock:
            if ok:
                self._last_ok_revision = revision
            last_ok = getattr(self, "_last_ok_revision", 0)
            manifest = json.loads(body) if body is not None else None
        manager._close_recording(self)
        if manifest is None or not last_ok:
            return self._finish_result("unpublished", "incomplete" if self._completed == 0 else "conflict",
                                       revision=last_ok)
        gap_ms = sum(g["duration_ms"] for g in manifest["gaps"]) if ok else 0
        complete = ok and manifest["capture_complete"]
        state = "published" if complete else "partial"
        return self._finish_result(state, None if complete else (self.reason if self.reason != "queued"
                                                                    else "incomplete"),
                                   gap_ms=gap_ms, complete=complete, revision=last_ok)

    async def finish_async(self, timeout: float = 30.0) -> RecordingResult:
        import asyncio

        return await asyncio.to_thread(self.finish, timeout)

    def _aborted_state(self):
        return ("partial", "incomplete") if getattr(self, "_last_ok_revision", 0) else ("unpublished", "incomplete")

    def _finish_result(self, state: str, reason: Optional[str], *, gap_ms: int = 0, complete: bool = False,
                       revision: Optional[int] = None) -> RecordingResult:
        if self._result is None:
            self._state = "denied" if state == "denied" else "finished"
            self._result = RecordingResult(self.recording_id, state, reason, getattr(self, "_completed", 0),
                                           gap_ms, complete,
                                           revision if revision is not None else getattr(self, "_last_ok_revision", 0))
        return self._result

    def abort(self) -> None:
        """Synchronous, no I/O: drop retained bytes and cancel queued segments."""
        manager = self._manager
        if manager is None:
            return
        try:
            with self._lock:
                if self._closed:
                    return
                self._closed, self._state = True, "aborted"
                for stream in self._streams:
                    if stream.run_start is not None:
                        manager._release(len(stream.run), 0)
                        self._retained -= len(stream.run)
                    for interval in stream.intervals:
                        if interval[0] == "data":
                            manager._release(len(interval[3]), 0)
                            self._retained -= len(interval[3])
                    stream.run, stream.run_start, stream.intervals = bytearray(), None, []
            manager._cancel_lane(self._lane, "incomplete")
            manager._close_recording(self)
        except Exception:
            return

    def status(self) -> RecordingStatus:
        if self._state == "denied" or self._manager is None:
            return RecordingStatus("denied", self.reason, 0, 0, 0, 0)
        with self._lock:
            return RecordingStatus(self._state if not self._truncated else "truncated",
                                   self.reason, self._completed, self._pending_segments(), 0,
                                   getattr(self, "_last_ok_revision", 0))

    def _reset_after_fork(self) -> None:
        # Child: never touch the inherited lock. The parent owns every byte.
        self._lock = threading.Lock()
        self._closed, self._state, self.reason = True, "terminal", "incomplete"
        if hasattr(self, "_streams"):
            for stream in self._streams:
                stream.run, stream.intervals = bytearray(), []
            self._windows = []

    def __repr__(self) -> str:
        return "RecordingWriter(reason=%r)" % self.reason
