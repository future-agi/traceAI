"""T-ST: live PCM recordings, segments, gaps and manifests (design section 2)."""

import json
import struct
import threading
import time
import uuid
from dataclasses import replace

import pytest
from fi_instrumentation.attachments import (
    AttachmentConfig, AttachmentManager, PcmFormat, RecordingOptions, TrackSpec,
)
from fi_instrumentation.attachments.errors import TransferError
from fi_instrumentation.attachments.protocol import Limits, validate_manifest, wire_valid
from fi_instrumentation.attachments.recording import segment_frames, to_ms
from opentelemetry.sdk.trace import TracerProvider

from conftest import TransportDouble, make_manager
from test_policy_runtime import CONTEXT

MONO = (TrackSpec((0,), "mixed", "unknown"),)


def options(rate=24000, layout="mixed", tracks=MONO, **kw):
    # Tests feed audio faster than real time; a 4-segment window keeps the
    # transport double from turning that speed into queue_full gaps.
    kw.setdefault("window_segments", 4)
    return RecordingOptions(PcmFormat(rate), layout, tracks, kw.pop("operation", "observe_consumed"), **kw)


def pcm(frames, channels=1, value=1):
    return struct.pack("<h", value) * frames * channels


def feed(writer, frames, *, channels=1, step=480, track=0):
    sent = 0
    ok = True
    while sent < frames:
        n = min(step, frames - sent)
        ok = writer.try_write(pcm(n, channels), track=track) and ok
        sent += n
    return ok


@pytest.fixture
def short():
    """A manager negotiated with 1 s segments."""
    transport = TransportDouble(limits=Limits(segment_duration_ms=1000))
    manager = make_manager(transport)
    yield manager, transport
    transport.release.set()
    manager.shutdown(2)


def last_manifest(transport):
    assert transport.manifests, "no manifest PUT"
    body = transport.manifests[-1][1]
    assert wire_valid("RecordingManifest", body)
    return body


@pytest.mark.parametrize("rate,frame_bytes,frames,size,ms", [
    (24000, 2, 720000, 1440044, 30000), (48000, 4, 1440000, 5760044, 30000), (48000, 16, 524256, 8388140, 10922),
])
def test_worked_vectors(rate, frame_bytes, frames, size, ms):
    """T-ST-01/04 vectors."""
    got = segment_frames(rate, frame_bytes, 30000, 8 * 1024 * 1024)
    assert (got, 44 + got * frame_bytes, to_ms(got, rate)) == (frames, size, ms)


def test_thirty_seconds_mono(manager, transport):
    """T-ST-01."""
    writer = manager.start_recording(CONTEXT, options())
    assert writer.reason == "queued"
    assert feed(writer, 720000, step=24000)
    result = writer.finish(10)
    assert result.state == "published", result
    assert [len(p) for p in transport.payloads] == [1440044]
    header = transport.payloads[0][:44]
    assert header[:4] == b"RIFF" and header[8:16] == b"WAVEfmt " and struct.unpack_from("<I", header, 40)[0] == 1440000
    manifest = last_manifest(transport)
    assert manifest["segments"] == [{"occurrence_id": manifest["segments"][0]["occurrence_id"], "index": 0,
                                     "start_ms": 0, "duration_ms": 30000}]


def test_sixty_one_seconds(manager, transport):
    """T-ST-02."""
    writer = manager.start_recording(CONTEXT, options())
    feed(writer, 61 * 24000, step=24000)
    result = writer.finish(15)
    assert result.state == "published" and result.capture_complete
    assert [len(p) for p in transport.payloads] == [1440044, 1440044, 48044]
    manifest = last_manifest(transport)
    assert manifest["duration_ms"] == 61000 and manifest["capture_complete"] and not manifest["gaps"]
    assert [s["duration_ms"] for s in manifest["segments"]] == [30000, 30000, 1000]
    assert [r["segment_index"] for r in transport.requests] == [0, 1, 2]
    assert all(r["recording_id"] == writer.recording_id and "track_id" not in r for r in transport.requests)


def test_44k_rounding(manager, transport):
    """T-ST-03."""
    writer = manager.start_recording(CONTEXT, options(rate=44100))
    feed(writer, 10000, step=1000)
    assert writer.finish(5).state == "published"
    assert len(transport.payloads[0]) == 20044
    assert last_manifest(transport)["duration_ms"] == 227


def test_eight_channel_segment_bound(manager, transport):
    """T-ST-04 (writer): 8 interleaved channels cut at 524 256 frames."""
    tracks = (TrackSpec(tuple(range(8)), "mixed", "unknown"),)
    writer = manager.start_recording(CONTEXT, options(rate=48000, tracks=tracks))
    feed(writer, 524256 + 48, channels=8, step=16384)
    writer.finish(15)
    assert len(transport.payloads[0]) == 8388140
    assert last_manifest(transport)["segments"][0]["duration_ms"] == 10922


def test_negotiated_segment_limits():
    """T-ST-05."""
    transport = TransportDouble(limits=Limits(segment_duration_ms=10000, segment_bytes=1024 * 1024))
    manager = make_manager(transport)
    try:
        writer = manager.start_recording(CONTEXT, options())
        feed(writer, 24000 * 25, step=24000)
        writer.finish(10)
        manifest = last_manifest(transport)
        assert all(s["duration_ms"] <= 10000 for s in manifest["segments"])
        assert all(len(p) <= 1024 * 1024 for p in transport.payloads)
    finally:
        manager.shutdown(1)


def test_separate_tracks_with_starved_track(manager, transport):
    """T-ST-06: per-track gaps carry track_id and validate."""
    tracks = (TrackSpec((0,), "input", "user"), TrackSpec((1,), "output", "assistant"))
    writer = manager.start_recording(CONTEXT, options(layout="separate_tracks", tracks=tracks))
    for second in range(10):
        writer.try_write(pcm(24000), track=0)
        if second >= 5:
            writer.try_write(pcm(24000), track=1)
    result = writer.finish(10)
    manifest = last_manifest(transport)
    assert result.state == "partial"
    ids = [t["track_id"] for t in manifest["tracks"]]
    assert all(s["track_id"] in ids for s in manifest["segments"])
    assert all(g["track_id"] in ids for g in manifest["gaps"])
    assert any(g["track_id"] == ids[1] and g["reason"] == "source_unavailable" for g in manifest["gaps"])
    assert {r["direction"] for r in transport.requests} == {"input", "output"}


def test_track_identity_rules(manager, transport):
    """T-ST-07/08: mixed/stereo never carry track_id; channel mapping is checked locally."""
    tracks = (TrackSpec((0,), "input", "user"), TrackSpec((1,), "output", "assistant"))
    writer = manager.start_recording(CONTEXT, options(layout="stereo", tracks=tracks))
    feed(writer, 24000, channels=2, step=4800)
    writer.finish(5)
    manifest = last_manifest(transport)
    assert manifest["channels"] == 2 and [t["channels"] for t in manifest["tracks"]] == [[0], [1]]
    assert all("track_id" not in s for s in manifest["segments"] + manifest["gaps"])
    broken = json.loads(json.dumps(manifest))
    broken["tracks"][1]["channels"] = [0]
    with pytest.raises(TransferError):
        validate_manifest(broken)
    broken = json.loads(json.dumps(manifest))
    broken["segments"][0]["track_id"] = str(uuid.uuid4())
    with pytest.raises(TransferError):
        validate_manifest(broken)


def test_budget_exhaustion_records_exact_gap(transport):
    """T-ST-09."""
    manager = make_manager(transport, max_owned_bytes=24000 * 2)
    try:
        transport.release.clear()
        writer = manager.start_recording(CONTEXT, options())
        assert writer.try_write(pcm(24000))          # exactly the budget
        assert writer.try_write(pcm(2400)) is False  # 100 ms rejected
        transport.release.set()
        result = writer.finish(5)
        manifest = last_manifest(transport) if transport.manifests else None
        assert result.state in ("partial", "unpublished")
        if manifest:
            assert {"start_ms": 1000, "duration_ms": 100, "reason": "queue_full"} in manifest["gaps"]
    finally:
        transport.release.set()
        manager.shutdown(1)


def test_window_full_with_blocked_transport(transport):
    """T-ST-10."""
    transport.release.clear()
    limits = Limits(segment_duration_ms=1000)
    transport.limits = limits
    manager = make_manager(transport)
    try:
        writer = manager.start_recording(CONTEXT, options(window_segments=2))
        accepted = [writer.try_write(pcm(24000)) for _ in range(6)]
        assert accepted[:2] == [True, True] and not all(accepted)
        assert writer.status().segments_pending <= 2 + 1
        transport.release.set()
        writer.finish(5)
        assert any(g["reason"] == "queue_full" for g in last_manifest(transport)["gaps"])
    finally:
        transport.release.set()
        manager.shutdown(1)


def test_bootstrap_pending_then_ready(transport):
    """T-ST-11."""
    gate = threading.Event()
    original = transport.bootstrap
    transport.bootstrap = lambda *a, **kw: (gate.wait(5), original(*a, **kw))[1]
    manager = AttachmentManager(config=AttachmentConfig(enabled=True), project_name="synthetic", transport=transport)
    try:
        writer = manager.start_recording(CONTEXT, options())
        assert writer.reason == "queued" and writer.status().state == "pending"
        assert writer.try_write(pcm(2400)) is False
        assert manager.owned_bytes == 0
        gate.set()
        assert manager.ready(5)
        assert writer.try_write(pcm(24000))
        writer.finish(5)
        manifest = last_manifest(transport)
        assert manifest["gaps"][0] == {"start_ms": 0, "duration_ms": 100, "reason": "bootstrap_pending"}
        assert manifest["segments"][0]["start_ms"] == 100
    finally:
        gate.set()
        manager.shutdown(1)


def test_policy_tightened_mid_recording(manager, transport):
    """T-ST-12."""
    writer = manager.start_recording(CONTEXT, options())
    transport.limits = manager._limits()
    feed(writer, 24000 * 31, step=24000)
    manager.update_policy(replace(manager._session.policy, enabled=False))
    assert writer.try_write(pcm(2400)) is False
    assert manager.status().owned_bytes == 0 or writer.status().state == "terminal"
    writer.finish(5)
    if transport.manifests:
        assert last_manifest(transport)["gaps"][-1]["reason"] == "policy_denied"


def test_recording_duration_cap(transport):
    """T-ST-13."""
    transport.limits = Limits(recording_duration_ms=60000)
    manager = make_manager(transport)
    try:
        writer = manager.start_recording(CONTEXT, options())
        feed(writer, 24000 * 61, step=24000)
        result = writer.finish(15)
        manifest = last_manifest(transport)
        assert not manifest["capture_complete"] and manifest["gaps"][-1]["reason"] == "truncated"
        assert result.state == "partial"
    finally:
        manager.shutdown(1)


def test_segment_failure_reuses_index(short):
    manager, transport = short
    """T-ST-14."""
    writer = manager.start_recording(CONTEXT, options())
    transport.failures = [TransferError("incomplete", True)] * 3
    feed(writer, 24000 * 3, step=24000)
    result = writer.finish(10)
    manifest = last_manifest(transport)
    assert transport.aborts, "abort not called"
    assert [r["segment_index"] for r in transport.requests] == [0, 0, 1]
    assert {"start_ms": 0, "duration_ms": 1000, "reason": "incomplete"} in manifest["gaps"]
    assert result.state == "partial"


def test_reused_index_conflict_closes_partial(short):
    manager, transport = short
    writer = manager.start_recording(CONTEXT, options())
    transport.failures = [TransferError("incomplete", True)] * 3 + []
    feed(writer, 24000, step=24000)
    deadline = time.monotonic() + 5
    while not transport.aborts and time.monotonic() < deadline:
        time.sleep(0.01)
    transport.init_failures = [TransferError("conflict", http_status=409)]
    feed(writer, 24000 * 2, step=24000)
    result = writer.finish(5)
    assert result.state in ("partial", "unpublished")


def test_manifests_are_prefix_monotonic(short):
    manager, transport = short
    """T-ST-15."""
    writer = manager.start_recording(CONTEXT, options())
    for _ in range(45):
        writer.try_write(pcm(24000))
        time.sleep(0.005)
    writer.finish(15)
    bodies = [m[1] for m in transport.manifests]
    keys = [m[2] for m in transport.manifests]
    assert len(bodies) >= 3
    for earlier, later in zip(bodies, bodies[1:]):
        assert later["segments"][:len(earlier["segments"])] == earlier["segments"]
        assert later["duration_ms"] >= earlier["duration_ms"]
    hexid = uuid.UUID(writer.recording_id).hex
    revs = [int(k.rsplit("-", 1)[1]) for k in keys]
    assert all(k.startswith("mf-" + hexid + "-") for k in keys) and revs == sorted(set(revs))


def test_manifest_uses_latest_completed_capability(manager, transport):
    """T-ST-16."""
    writer = manager.start_recording(CONTEXT, options())
    feed(writer, 24000 * 31, step=24000)
    writer.finish(10)
    content_caps = [cap for kind, cap in transport.capability_used if kind == "complete"]
    manifest_caps = [cap for kind, cap in transport.capability_used if kind == "manifest"]
    assert manifest_caps[-1] == content_caps[-1]
    assert all(cap.startswith("cap_") for cap in manifest_caps)


def test_capture_complete_requires_no_gaps(manager, transport):
    """T-ST-17."""
    writer = manager.start_recording(CONTEXT, options())
    feed(writer, 24000, step=2400)
    writer.mark_gap(500)
    feed(writer, 2400)
    writer.finish(5)
    manifest = last_manifest(transport)
    assert manifest["gaps"] and not manifest["capture_complete"]
    forged = dict(manifest, capture_complete=True)
    with pytest.raises(TransferError):
        validate_manifest(forged)


def test_try_write_never_does_io(manager, transport):
    """T-ST-18: no transport call on the caller thread."""
    transport.release.clear()
    writer = manager.start_recording(CONTEXT, options())
    caller = threading.current_thread().name
    started = time.perf_counter()
    for _ in range(2000):
        writer.try_write(pcm(480))
    elapsed = time.perf_counter() - started
    assert caller not in transport.content_thread
    print("try_write mean us", elapsed / 2000 * 1e6)
    transport.release.set()
    writer.abort()


def test_finish_bounded_with_blocked_transport(transport):
    """T-ST-19."""
    transport.release.clear()
    transport.block_forever = True
    manager = make_manager(transport)
    try:
        writer = manager.start_recording(CONTEXT, options())
        feed(writer, 24000, step=4800)
        start = time.monotonic()
        result = writer.finish(1)
        assert time.monotonic() - start <= 1.25
        assert result.state in ("partial", "unpublished")
    finally:
        transport.block_forever = False
        transport.release.set()
        manager.shutdown(1)


def test_no_segment_references_and_live_stamp(manager, transport):
    """T-ST-20."""
    provider = TracerProvider(shutdown_on_exit=False)
    span = provider.get_tracer("t").start_span("root")
    writer = manager.start_recording(span, options())
    feed(writer, 24000, step=4800)
    writer.finish(5)
    attributes = dict(span.attributes)
    assert attributes["fi.attachment.refs"] == "[]" and attributes["fi.attachment.schema_version"] == 1
    assert attributes["fi.attachment.capture_session_id"] == manager.capture_session_id
    assert wire_valid("SpanAttributes", {k: v for k, v in attributes.items() if k.startswith("fi.attachment.")})
    span.end()
    provider.shutdown()


def test_abort_is_synchronous_and_uploads_no_manifest(manager, transport):
    transport.release.clear()
    writer = manager.start_recording(CONTEXT, options())
    feed(writer, 24000, step=4800)
    start = time.monotonic()
    writer.abort()
    assert time.monotonic() - start < 0.1
    transport.release.set()
    assert writer.finish(1).state == "unpublished"
    assert not transport.manifests


def test_create_recording_requires_both_flags(transport):
    transport.policy = replace(transport.policy, record_audio=True)
    manager = make_manager(transport)
    try:
        assert manager.start_recording(CONTEXT, options(operation="create_recording")).reason == "recording_disabled"
    finally:
        manager.shutdown(1)


def test_partial_frames_carry(manager, transport):
    writer = manager.start_recording(CONTEXT, options())
    data = pcm(1000)
    assert writer.try_write(data[:3]) and writer.try_write(data[3:])
    writer.finish(5)
    assert len(transport.payloads[0]) == 44 + 2000


# --- Review finding (P1): a stereo recording carries both sides in every byte ---
STEREO = (TrackSpec((0,), "input", "user"), TrackSpec((1,), "output", "assistant"))


@pytest.mark.parametrize("flag", ["hide_inputs", "hide_outputs"])
def test_live_stereo_one_side_hidden_denies_whole_recording(transport, flag):
    """Design 2.4: stereo is evaluated as ``mixed``; hiding one channel is not removal."""
    from fi_instrumentation.attachments import AttachmentPolicy
    from fi_instrumentation.instrumentation.config import TraceConfig

    manager = make_manager(transport)
    manager.policy = AttachmentPolicy(manager.config, TraceConfig(**{flag: True}))
    try:
        writer = manager.start_recording(CONTEXT, options(layout="stereo", tracks=STEREO))
        assert writer.reason == "policy_denied"
        assert writer.try_write(pcm(2400, 2)) is False
        assert writer.finish(2).state == "denied"
        assert not transport.requests and not transport.payloads and not transport.manifests
        assert manager.owned_bytes == 0
        # Control: separate tracks keep the visible side and drop only the hidden one.
        separate = manager.start_recording(CONTEXT, options(layout="separate_tracks", tracks=STEREO))
        assert separate.reason == "queued"
        separate.abort()
    finally:
        manager.shutdown(1)


# --- Review finding (P1): recording_bytes caps the TOTAL uploaded, not what is buffered ---
def test_recording_bytes_cap_is_cumulative(transport):
    """Design 0.7/2.6: ``truncated`` once the total reaches min(limits.recording_bytes, 1 GiB),
    even when earlier segments have already uploaded and released their memory."""
    limit = 3_000_000
    transport.limits = Limits(recording_bytes=limit)
    manager = make_manager(transport)
    try:
        writer = manager.start_recording(CONTEXT, options())
        accepted = 0
        for _ in range(120):                       # 120 s of 24 kHz mono, 48 000 B/s
            if writer.try_write(pcm(24000)):
                accepted += 48000
            deadline = time.monotonic() + 5
            while writer.status().segments_pending and time.monotonic() < deadline:
                time.sleep(0.01)                   # drain: the old check only saw buffered bytes
        assert writer.status().state == "truncated"
        result = writer.finish(15)
        manifest = last_manifest(transport)
        uploaded = sum(len(p) for p in transport.payloads)
        assert uploaded <= limit, uploaded
        assert accepted <= limit
        assert uploaded >= 2 * 1440044            # two full 30 s segments fit under 3 MB
        assert not manifest["capture_complete"] and manifest["gaps"][-1]["reason"] == "truncated"
        assert manifest["duration_ms"] == 120000
        assert result.state == "partial"
    finally:
        manager.shutdown(1)


def test_recording_bytes_cap_counts_every_track(transport):
    """Separate tracks share one recording total: headers and both tracks' bytes count."""
    limit = 2_000_000
    transport.limits = Limits(recording_bytes=limit, segment_duration_ms=5000)
    manager = make_manager(transport)
    try:
        writer = manager.start_recording(CONTEXT, options(layout="separate_tracks", tracks=STEREO))
        assert writer.reason == "queued"
        for _ in range(60):
            writer.try_write(pcm(24000), track=0)
            writer.try_write(pcm(24000), track=1)
            deadline = time.monotonic() + 5
            while writer.status().segments_pending and time.monotonic() < deadline:
                time.sleep(0.01)
        writer.finish(15)
        assert sum(len(p) for p in transport.payloads) <= limit
        assert any(g["reason"] == "truncated" for g in last_manifest(transport)["gaps"])
    finally:
        manager.shutdown(1)


def test_recording_bytes_cap_counts_segment_headers():
    """Every sealed segment's 44-byte header counts toward recording_bytes.

    1 s segments of 24 kHz mono are 48 044 bytes. Counting the two sealed headers
    (96 088 bytes sealed), a third segment fits its own header and 4 writes of 960
    bytes under this limit; ignoring them (96 000 bytes) would admit a 5th write
    and upload 48 bytes past the cap.
    """
    full = 44 + 48000
    limit = 2 * 48000 + 44 + 5 * 960 + 40          # 100 884
    transport = TransportDouble(limits=Limits(segment_duration_ms=1000, recording_bytes=limit))
    manager = make_manager(transport)
    try:
        writer = manager.start_recording(CONTEXT, options())
        feed(writer, 24000 * 3, step=480)
        assert writer.status().state == "truncated"
        writer.finish(15)
        sizes = [len(p) for p in transport.payloads]
        assert sizes == [full, full, 44 + 4 * 960], sizes
        assert sum(sizes) <= limit
        assert last_manifest(transport)["gaps"][-1]["reason"] == "truncated"
    finally:
        manager.shutdown(1)
