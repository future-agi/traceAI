import os
from dataclasses import replace

import pytest
from fi_instrumentation.attachments import RecordingFileScope
from fi_instrumentation.attachments.protocol import TransferError
from test_policy_runtime import CONTEXT


def make_source(tmp_path, payload=b"synthetic-recording"):
    path = tmp_path / "recording.wav"
    path.write_bytes(payload)
    info = path.stat()
    scope = RecordingFileScope(str(tmp_path), enabled=True)
    source = scope.source(path.name, device=info.st_dev, inode=info.st_ino, size=info.st_size)
    return scope, source, path


def test_independent_lease_does_not_advance_app_file(manager, transport, tmp_path):
    scope, source, path = make_source(tmp_path)
    try:
        with path.open("rb") as application:
            application.read(3)
            handle = manager.attach(CONTEXT, source, mime_type="audio/wav", direction="mixed")
            assert handle.result(2).status == "validating"
            assert application.tell() == 3
        assert transport.payloads == [b"synthetic-recording"]
    finally:
        scope.close()


def test_no_lease_under_policy_denial(manager, transport, tmp_path, monkeypatch):
    scope, source, _ = make_source(tmp_path)
    manager.update_policy(replace(manager._session.policy, enabled=False))
    monkeypatch.setattr(scope, "_open", lambda source: pytest.fail("denied file opened"))
    assert manager.attach(CONTEXT, source, mime_type="audio/wav", direction="mixed").admission == "policy_denied"
    assert not transport.requests
    scope.close()


@pytest.mark.parametrize("kind", ["symlink", "replaced", "fifo", "directory", "missing"])
def test_file_identity_and_type_races_fail_closed(manager, transport, tmp_path, kind):
    scope, source, path = make_source(tmp_path)
    alternate = tmp_path / "alternate"
    alternate.write_bytes(b"secret")
    path.unlink()
    if kind == "symlink":
        path.symlink_to(alternate)
    elif kind == "replaced":
        path.write_bytes(b"different")
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    try:
        assert manager.attach(CONTEXT, source, mime_type="audio/wav", direction="mixed").result(2).status == "source_unavailable"
        assert not transport.requests and not transport.payloads
    finally:
        scope.close()


def test_content_changed_after_lease_is_not_completed(tmp_path):
    scope, source, path = make_source(tmp_path)
    lease = source._open()
    try:
        path.write_bytes(b"changed")
        with pytest.raises(TransferError):
            lease.check()
    finally:
        lease.close()
        scope.close()


@pytest.mark.parametrize("relative", ["../private", "/private", "a/../private", "a//private", "./private"])
def test_traversal_not_authorized(tmp_path, relative):
    scope = RecordingFileScope(str(tmp_path), enabled=True)
    try:
        with pytest.raises(ValueError):
            scope.source(relative, device=1, inode=1, size=1)
    finally:
        scope.close()


def test_directory_symlink_and_implicit_permission_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        RecordingFileScope(str(link), enabled=True)
    with pytest.raises(ValueError):
        RecordingFileScope(str(target))


# ---- T-LS: owned-recording tiers and leases (design section 4) ----
import hashlib
import struct
import time

from fi_instrumentation.attachments import FileIdentity, TrackSpec
from fi_instrumentation.attachments.protocol import Limits, wire_valid
from fi_instrumentation.attachments.sources import parse_wav, wav_header

from conftest import TransportDouble, make_manager

MiB = 1024 * 1024


def big_manager(**limits):
    transport = TransportDouble(limits=Limits(max_upload_bytes=50 * MiB, **limits))
    return transport, make_manager(transport)


def write_wav(path, channels, rate, frames, pattern=b"\x01\x02"):
    with open(path, "wb") as handle:
        handle.write(wav_header(channels, rate, frames))
        chunk = pattern * channels * 65536
        remaining = frames * channels * 2
        while remaining:
            part = chunk[:min(len(chunk), remaining)]
            handle.write(part)
            remaining -= len(part)


def test_identify_uses_opened_file(tmp_path):
    scope, _, path = make_source(tmp_path)
    try:
        identity = scope.identify(path.name)
        info = path.stat()
        assert identity == FileIdentity(info.st_dev, info.st_ino, info.st_size)
    finally:
        scope.close()


def test_symlinked_intermediate_directory(tmp_path):
    """T-LS-03."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.wav").write_bytes(b"x")
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    scope = RecordingFileScope(str(tmp_path), enabled=True)
    try:
        with pytest.raises(TransferError):
            scope.identify("link/a.wav")
    finally:
        scope.close()


def test_modified_during_transfer(tmp_path):
    """T-LS-01."""
    transport, manager = big_manager()
    path = tmp_path / "r.ogg"
    path.write_bytes(b"o" * (600 * 1024))
    scope = RecordingFileScope(str(tmp_path), enabled=True)
    try:
        original = transport.put_content

        def mutate(session, upload, source, **kw):
            with open(path, "ab") as handle:
                handle.write(b"tamper")
            return original(session, upload, source, **kw)
        transport.put_content = mutate
        source = scope.source(path.name, scope.identify(path.name))
        handle = manager.attach_owned_recording(CONTEXT, source, mime_type="audio/ogg", direction="mixed")
        assert handle.tier == "single" and handle.result(3).status == "source_unavailable"
        assert transport.aborts
    finally:
        scope.close()
        manager.shutdown(1)


def test_single_tier_streams_from_lease(tmp_path):
    """T-LS-06: 12 MiB OGG uploads directly from the file within one slice of owned memory."""
    transport, manager = big_manager()
    path = tmp_path / "r.ogg"
    path.write_bytes(b"\x4f" * (12 * MiB))
    scope = RecordingFileScope(str(tmp_path), enabled=True)
    try:
        source = scope.source(path.name, scope.identify(path.name))
        handle = manager.attach_owned_recording(CONTEXT, source, mime_type="audio/ogg", direction="mixed")
        assert handle.tier == "single"
        assert manager.owned_bytes <= manager.config.chunk_bytes
        assert handle.result(10).status == "validating"
        assert len(transport.payloads[0]) == 12 * MiB
        assert "recording_id" not in transport.requests[0]
    finally:
        scope.close()
        manager.shutdown(1)


def test_encoded_over_cap_too_large(tmp_path, monkeypatch):
    """T-LS-07: 70 MiB OGG is refused with zero bytes read."""
    transport, manager = big_manager()
    path = tmp_path / "big.ogg"
    with open(path, "wb") as handle:
        handle.truncate(70 * MiB)
    scope = RecordingFileScope(str(tmp_path), enabled=True)
    try:
        source = scope.source(path.name, scope.identify(path.name))
        monkeypatch.setattr(scope, "_open", lambda s: pytest.fail("opened"))
        handle = manager.attach_owned_recording(CONTEXT, source, mime_type="audio/ogg", direction="mixed")
        assert (handle.tier, handle.reason) == ("denied", "too_large")
    finally:
        scope.close()
        manager.shutdown(1)


def test_large_wav_becomes_segmented_recording(tmp_path):
    """T-LS-08: >50 MiB 24 kHz stereo WAV with roles -> stereo manifest from lease ranges."""
    transport, manager = big_manager()
    frames = (52 * MiB) // 4
    path = tmp_path / "call.wav"
    write_wav(path, 2, 24000, frames)
    scope = RecordingFileScope(str(tmp_path), enabled=True)
    try:
        source = scope.source(path.name, scope.identify(path.name))
        tracks = (TrackSpec((0,), "input", "user"), TrackSpec((1,), "output", "assistant"))
        handle = manager.attach_owned_recording(CONTEXT, source, mime_type="audio/wav", direction="mixed",
                                                wav_tracks=tracks)
        assert handle.tier == "wav_recording" and handle.recording_id
        assert manager.owned_bytes <= manager.config.chunk_bytes
        assert handle.result(60).status == "validating"
        manifest = transport.manifests[-1][1]
        assert wire_valid("RecordingManifest", manifest) and manifest["layout"] == "stereo"
        assert [t["role"] for t in manifest["tracks"]] == ["user", "assistant"] and manifest["capture_complete"]
        data = path.read_bytes()[44:]
        decoded = b"".join(p[44:] for p in transport.payloads)
        assert hashlib.sha256(decoded).digest() == hashlib.sha256(data).digest()
        assert all(len(p) <= 8 * MiB for p in transport.payloads)
        assert all(r["recording_id"] == handle.recording_id for r in transport.requests)
    finally:
        scope.close()
        manager.shutdown(1)


@pytest.mark.parametrize("kind", ["bad-length", "float", "24-bit"])
def test_unsupported_wav_falls_back(tmp_path, kind):
    """T-LS-09: tier single when within the cap; refused above it."""
    transport, manager = big_manager()
    header = bytearray(wav_header(1, 16000, 1000))
    if kind == "bad-length":
        struct.pack_into("<I", header, 40, 5)
    elif kind == "float":
        struct.pack_into("<H", header, 20, 3)
    else:
        struct.pack_into("<H", header, 34, 24)
    assert parse_wav(bytes(header) + b"\x00" * 2000, 44 + 2000) is None
    path = tmp_path / "odd.wav"
    path.write_bytes(bytes(header) + b"\x00" * 2000)
    scope = RecordingFileScope(str(tmp_path), enabled=True)
    try:
        source = scope.source(path.name, scope.identify(path.name))
        handle = manager.attach_owned_recording(CONTEXT, source, mime_type="audio/wav", direction="mixed",
                                                wav_tracks=(TrackSpec((0,), "mixed", "unknown"),))
        assert handle.result(5).status == "validating"
        assert "recording_id" not in transport.requests[0]
    finally:
        scope.close()
        manager.shutdown(1)


def test_finalization_deadline(tmp_path):
    """T-LS-10."""
    transport, manager = big_manager()
    transport.release.clear()
    transport.block_forever = True
    path = tmp_path / "r.ogg"
    path.write_bytes(b"o" * 4096)
    scope = RecordingFileScope(str(tmp_path), enabled=True)
    try:
        source = scope.source(path.name, scope.identify(path.name))
        handle = manager.attach_owned_recording(CONTEXT, source, mime_type="audio/ogg", direction="mixed")
        start = time.monotonic()
        assert handle.result(1).status == "unfinished"
        handle.cancel()
        transport.block_forever = False
        assert handle.result(2).status == "incomplete"
        assert time.monotonic() - start < 3.25
    finally:
        transport.release.set()
        scope.close()
        manager.shutdown(1)


# --- Review finding (P1): owned WAV admission must match the wire direction ---
# Every wav_recording segment is sent as ``mixed`` (design 2.4/2.6/7), so a hide
# flag on EITHER side must deny the whole file whatever label the caller passed.
STEREO_ROLES = (TrackSpec((0,), "input", "user"), TrackSpec((1,), "output", "assistant"))


def _owned(tmp_path, channels, *, trace_config=None, direction, wav_tracks):
    from fi_instrumentation.attachments import AttachmentPolicy

    transport, manager = big_manager()
    if trace_config is not None:
        manager.policy = AttachmentPolicy(manager.config, trace_config)
    path = tmp_path / "call.wav"
    write_wav(path, channels, 24000, 24000 * 2)
    scope = RecordingFileScope(str(tmp_path), enabled=True)
    source = scope.source(path.name, scope.identify(path.name))
    handle = manager.attach_owned_recording(CONTEXT, source, mime_type="audio/wav", direction=direction,
                                            wav_tracks=wav_tracks)
    return transport, manager, scope, handle


@pytest.mark.parametrize("flag", ["hide_inputs", "hide_outputs"])
@pytest.mark.parametrize("direction", ["input", "output", "mixed"])
@pytest.mark.parametrize("channels,tracks", [(2, STEREO_ROLES), (1, (TrackSpec((0,), "mixed", "user"),))])
def test_owned_wav_one_side_hidden_denies_whole_file(tmp_path, monkeypatch, flag, direction, channels, tracks):
    from fi_instrumentation.instrumentation.config import TraceConfig

    transport, manager, scope, handle = _owned(tmp_path, channels, trace_config=TraceConfig(**{flag: True}),
                                               direction=direction, wav_tracks=tracks)
    try:
        assert (handle.tier, handle.reason) == ("denied", "policy_denied")
        assert not transport.requests and not transport.payloads and not transport.manifests
        assert manager.owned_bytes == 0
    finally:
        scope.close()
        manager.shutdown(1)


@pytest.mark.parametrize("direction", ["input", "output", "mixed"])
def test_owned_stereo_wav_wire_direction_is_mixed(tmp_path, direction):
    transport, manager, scope, handle = _owned(tmp_path, 2, direction=direction, wav_tracks=STEREO_ROLES)
    try:
        assert handle.tier == "wav_recording"
        assert handle.result(20).status == "validating"
        assert transport.requests and {r["direction"] for r in transport.requests} == {"mixed"}
        manifest = transport.manifests[-1][1]
        assert manifest["layout"] == "stereo"
        assert [t["direction"] for t in manifest["tracks"]] == ["input", "output"]
    finally:
        scope.close()
        manager.shutdown(1)


def test_owned_wav_invalid_direction_denied(tmp_path):
    transport, manager, scope, handle = _owned(tmp_path, 2, direction="left", wav_tracks=STEREO_ROLES)
    try:
        assert (handle.tier, handle.reason) == ("denied", "policy_denied")
        assert not transport.requests
    finally:
        scope.close()
        manager.shutdown(1)


def test_owned_wav_policy_tightening_mid_file_stops_upload(tmp_path):
    """Per-chunk re-checks evaluate ``mixed`` too: hiding one side mid-file stops the rest."""
    from fi_instrumentation.attachments import AttachmentPolicy
    from fi_instrumentation.instrumentation.config import TraceConfig

    transport, manager = big_manager(segment_duration_ms=1000)
    path = tmp_path / "call.wav"
    write_wav(path, 2, 24000, 24000 * 4)
    scope = RecordingFileScope(str(tmp_path), enabled=True)
    try:
        source = scope.source(path.name, scope.identify(path.name))
        transport.release.clear()
        handle = manager.attach_owned_recording(CONTEXT, source, mime_type="audio/wav", direction="input",
                                                wav_tracks=STEREO_ROLES)
        assert handle.tier == "wav_recording"
        assert transport.entered.wait(5)
        manager.policy = AttachmentPolicy(manager.config, TraceConfig(hide_outputs=True))
        transport.release.set()
        assert handle.result(20).status != "validating"
        assert len(transport.payloads) == 0
    finally:
        transport.release.set()
        scope.close()
        manager.shutdown(1)
