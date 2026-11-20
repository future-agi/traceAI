"""T-LC: strict no-op when disabled, bounded shutdown, fork safety (design section 8)."""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fi_instrumentation.attachments import (
    AttachmentConfig, AttachmentManager, Bytes, CompletedRecordingRequest, DisabledAttachmentManager, PcmFormat,
    RecordingOptions, ServerPolicy, StreamOptions, TrackSpec, create_attachment_manager,
)
from fi_instrumentation.attachments import manager as manager_module

from conftest import TransportDouble, make_manager
from test_policy_runtime import CONTEXT

PROBE = Path(__file__).with_name("exit_probe.py")


def run_probe(case, limit):
    start = time.monotonic()
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    result = subprocess.run([sys.executable, str(PROBE), case], capture_output=True, timeout=limit, env=env)
    elapsed = time.monotonic() - start
    assert result.returncode == 0, result.stderr.decode()[-2000:]
    assert b"DONE" in result.stdout
    assert elapsed < limit
    return elapsed


@pytest.mark.parametrize("case,limit", [
    ("disabled", 10), ("enabled-idle", 10), ("enabled-blocked", 10), ("enabled-no-shutdown", 10),
    ("fork", 15), ("mid-recording", 15), ("import-pending", 10),
])
def test_process_exit_probes(case, limit):
    """T-LC-06..11: separate interpreters exit 0 within W."""
    if case == "fork" and not hasattr(os, "fork"):
        pytest.skip("no fork on this platform")
    run_probe(case, limit)


def test_disabled_factory_is_null_object(monkeypatch):
    """T-LC-01/03: no thread, no fork hook, no network; every call immediate."""
    calls = []
    monkeypatch.setattr(os, "register_at_fork", lambda **kw: calls.append(kw))
    before = {t.ident for t in threading.enumerate()}
    manager = create_attachment_manager(config=AttachmentConfig(), project_name="synthetic")
    assert isinstance(manager, DisabledAttachmentManager)
    assert not calls
    start = time.monotonic()
    assert manager.status().state == "disabled" and manager.status().owned_bytes == 0
    assert manager.ready(5) is False
    assert manager.preflight(CONTEXT, mime_type="audio/wav", direction="mixed", max_bytes=1).reason == "disabled"
    assert manager.attach(CONTEXT, Bytes(b"x"), mime_type="audio/wav", direction="mixed").result().status == "disabled"
    stream = manager.start_attachment_stream(CONTEXT, StreamOptions("audio/wav", "mixed", 10))
    assert stream.reason == "disabled" and stream.try_write(b"x") is False
    writer = manager.start_recording(CONTEXT, RecordingOptions(PcmFormat(24000), "mixed",
                                                               (TrackSpec((0,), "mixed", "unknown"),),
                                                               "observe_consumed"))
    assert writer.reason == "disabled" and writer.try_write(b"\x00\x00") is False
    assert writer.finish(1).state == "denied"
    assert manager.attach_owned_recording(CONTEXT, None, mime_type="audio/wav", direction="mixed").reason == "disabled"
    manager.update_policy(ServerPolicy(enabled=True))
    assert manager.status().state == "disabled"           # T-LC-05
    assert manager.flush(5) == manager.shutdown(5)
    assert (manager.flush(5).unfinished, manager.flush(5).dropped) == (0, 0)
    assert time.monotonic() - start < 0.5
    assert {t.ident for t in threading.enumerate()} == before


def test_disabled_import_never_calls_resolver():
    import asyncio

    class Resolver:
        async def resolve(self, request, *, deadline):
            raise AssertionError("resolver called")

    manager = create_attachment_manager(project_name="synthetic")
    handle = asyncio.run(manager.import_completed_recording(
        CONTEXT, CompletedRecordingRequest("livekit_egress", "EG_1"), resolver=Resolver()))
    assert handle.reason == "disabled"


def test_env_disable_returns_null_object(monkeypatch):
    monkeypatch.setenv("FI_ATTACHMENTS_ENABLED", "false")
    manager = create_attachment_manager(config=AttachmentConfig(enabled=True), project_name="synthetic")
    assert isinstance(manager, DisabledAttachmentManager)


def test_disabled_register_writes_no_span_attributes():
    """T-LC-04."""
    from fi_instrumentation import register

    provider = register(project_name="synthetic", batch=False, set_global_tracer_provider=False, verbose=False)
    try:
        with provider.get_tracer("t").start_as_current_span("s") as span:
            pass
        assert not any(key.startswith("fi.attachment.") for key in (span.attributes or {}))
        assert isinstance(provider.attachment_manager, DisabledAttachmentManager)
    finally:
        provider.shutdown()


def test_fork_hook_registered_once(monkeypatch):
    """T-LC-14: 1000 enabled managers register exactly one fork hook."""
    calls = []
    monkeypatch.setattr(manager_module, "_FORK_HOOK_REGISTERED", False)
    monkeypatch.setattr(os, "register_at_fork", lambda **kw: calls.append(kw))
    for _ in range(1000):
        AttachmentManager(config=AttachmentConfig(enabled=True), project_name="")
    assert len(calls) == 1


def test_workers_start_lazily(transport):
    manager = make_manager(transport)
    try:
        assert not manager._threads
        assert manager.attach(CONTEXT, Bytes(b"x"), mime_type="audio/wav", direction="mixed").result(2).status == "validating"
        assert len(manager._threads) == manager.config.workers
        assert all(t.daemon and t.name.startswith("fi-attachment-worker-") for t in manager._threads)
    finally:
        manager.shutdown(1)


def test_shutdown_idempotent_and_concurrent(transport):
    """T-LC-15."""
    manager = make_manager(transport)
    results = []
    threads = [threading.Thread(target=lambda: results.append(manager.shutdown(1))) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(5) for t in threads]
    assert len(results) == 2 and results[0] == results[1]
    assert manager.shutdown(1) == results[0]


def test_shutdown_bounded_with_blocked_transport(transport):
    """T-LC-16: returns within timeout + 250 ms even while a transfer blocks."""
    transport.release.clear()
    transport.block_forever = True
    manager = make_manager(transport)
    manager.attach(CONTEXT, Bytes(b"x" * 10), mime_type="audio/wav", direction="mixed")
    assert transport.entered.wait(2)
    start = time.monotonic()
    result = manager.shutdown(0.5)
    assert time.monotonic() - start < 0.75
    assert result.unfinished >= 1


def test_ready_state_name(transport):
    """T-LC-12."""
    manager = make_manager(transport)
    try:
        assert manager.status().state == "ready"
    finally:
        manager.shutdown(1)


def test_no_untimed_waits():
    source = Path(manager_module.__file__).read_text()
    assert ".wait()" not in source
