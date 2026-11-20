"""Separate-interpreter exit probes (design 8.5, T-LC-06..11).

Run as ``python exit_probe.py <case>``. Prints ``DONE`` and exits 0 on success.
Never imported by pytest: forking a threaded pytest process is unsafe (the
latest OpenTelemetry SDK's own at-fork handler can deadlock a child).
"""

import asyncio
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fi_instrumentation.attachments import (  # noqa: E402
    AttachmentConfig, AttachmentManager, Bytes, CompletedRecordingRequest, CorrelationHandle, PcmFormat,
    RecordingOptions, TrackSpec,
)

CONTEXT = CorrelationHandle("1234567890abcdef1234567890abcdef", "1234567890abcdef", True)


def TransportDouble():
    # Imported lazily: the disabled case must not import the wire adapter.
    from conftest import TransportDouble as Double

    return Double()


def manager(transport, **config):
    result = AttachmentManager(config=AttachmentConfig(enabled=True, **config), project_name="synthetic",
                               transport=transport)
    assert result.ready(5)
    return result


def case_disabled():
    import atexit

    registered = []
    original_fork, original_atexit = os.register_at_fork, atexit.register
    def owner(function):
        function = getattr(function, "__func__", function)
        return getattr(function, "__module__", "") or ""

    os.register_at_fork = lambda **kw: (registered.extend(owner(f) for f in kw.values()), original_fork(**kw))[1]
    atexit.register = lambda f, *a, **kw: (registered.append(owner(f)), original_atexit(f, *a, **kw))[1]
    from fi_instrumentation import register

    before = threading.active_count()
    provider = register(project_name="synthetic", batch=False, set_global_tracer_provider=False, verbose=False)
    atexit.register = original_atexit
    os.register_at_fork = original_fork
    with provider.get_tracer("probe").start_as_current_span("span") as span:
        attributes = dict(span.attributes or {})
    provider.shutdown()
    assert provider.attachment_manager.status().state == "disabled"
    # The OpenTelemetry SDK registers its own hooks; attachments registers none.
    assert not [m for m in registered if m.startswith("fi_instrumentation.attachments")], registered
    from fi_instrumentation.attachments import manager as attachment_manager

    assert attachment_manager._FORK_HOOK_REGISTERED is False
    assert "fi_instrumentation.attachments.protocol" not in sys.modules
    # fi_instrumentation.otel itself imports jsonschema; attachments must not
    # compile its privacy table or load the wire schema when disabled.
    from fi_instrumentation.attachments import policy

    assert policy._table.cache_info().currsize == 0
    assert not any(k.startswith("fi.attachment.") for k in attributes)
    assert not [t.name for t in threading.enumerate() if t.name.startswith("fi-attachment")]
    assert threading.active_count() == before, (before, threading.active_count())


def case_enabled_idle():
    result = manager(TransportDouble())
    assert result.status().state == "ready"
    assert result.shutdown(1).unfinished == 0


def case_enabled_blocked():
    transport = TransportDouble()
    transport.release.clear()
    transport.block_forever = True
    result = manager(transport, shutdown_timeout=1)
    handle = result.attach(CONTEXT, Bytes(b"x" * 64), mime_type="audio/wav", direction="mixed")
    assert handle.admission == "queued"
    assert transport.entered.wait(5)
    start = time.monotonic()
    flushed = result.shutdown()
    assert time.monotonic() - start < 1.25, time.monotonic() - start
    assert flushed.unfinished >= 1


def case_enabled_no_shutdown():
    transport = TransportDouble()
    transport.release.clear()
    transport.block_forever = True
    result = manager(transport)
    result.attach(CONTEXT, Bytes(b"x" * 64), mime_type="audio/wav", direction="mixed")
    assert transport.entered.wait(5)
    # Return without shutdown: daemon threads must not hold the process.


def case_fork():
    transport = TransportDouble()
    transport.release.clear()
    result = manager(transport)
    parent_session = result.capture_session_id
    parent = result.attach(CONTEXT, Bytes(b"parent"), mime_type="audio/wav", direction="mixed")
    assert transport.entered.wait(5)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_fd)
            transport.release.set()
            ok = result.owned_bytes == 0 and result.capture_session_id != parent_session
            ok = ok and result.status().state == "bootstrap_pending" and result.ready(5)
            child = result.attach(CONTEXT, Bytes(b"child"), mime_type="audio/wav", direction="mixed")
            ok = ok and child.result(5).status == "validating"
            ok = ok and result.flush(5).unfinished == 0
            ok = ok and transport.bootstrap_bodies[-1]["capture_session_id"] != parent_session
            os.write(write_fd, b"ok" if ok else b"failed")
        finally:
            os._exit(0)
    os.close(write_fd)
    answer = os.read(read_fd, 20)
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    assert answer == b"ok" and status == 0, (answer, status)
    transport.release.set()
    assert parent.result(5).status == "validating"
    assert result.shutdown(2).unfinished == 0


def case_mid_recording():
    transport = TransportDouble()
    result = manager(transport)
    options = RecordingOptions(PcmFormat(24000), "mixed", (TrackSpec((0,), "mixed", "unknown"),),
                               "observe_consumed")
    writer = result.start_recording(CONTEXT, options)
    assert writer.reason == "queued", writer.reason
    chunk = b"\x00\x01" * 480          # 20 ms at 24 kHz mono
    for _ in range(250):               # 5 s
        writer.try_write(chunk)
    start = time.monotonic()
    result.shutdown(5)
    assert time.monotonic() - start < 5.25
    final = writer.finish(0)
    assert final.state in ("published", "partial"), final
    assert transport.manifests, "final manifest not PUT"


def case_import_pending():
    transport = TransportDouble()
    result = manager(transport)

    class Never:
        async def resolve(self, request, *, deadline):
            await asyncio.sleep(3600)

    async def run():
        return await result.import_completed_recording(
            CONTEXT, CompletedRecordingRequest("livekit_egress", "EG_synthetic"), resolver=Never(), timeout=1)

    handle = asyncio.run(run())
    assert handle.reason == "source_unavailable", handle.reason
    assert result.shutdown(1).unfinished == 0


CASES = {"disabled": case_disabled, "enabled-idle": case_enabled_idle, "enabled-blocked": case_enabled_blocked,
         "enabled-no-shutdown": case_enabled_no_shutdown, "fork": case_fork, "mid-recording": case_mid_recording,
         "import-pending": case_import_pending}

if __name__ == "__main__":
    CASES[sys.argv[1]]()
    print("DONE", flush=True)
