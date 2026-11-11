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
