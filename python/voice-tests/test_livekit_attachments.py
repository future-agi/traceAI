"""Synthetic component tests, not a real SFU call or product-playback claim."""

import asyncio
import inspect
import io
import os
import subprocess
import time
from dataclasses import replace
from importlib.metadata import version
from types import SimpleNamespace

import av
import pytest
from livekit import api
from livekit.agents import AgentSession, JobContext
from livekit.agents.voice.recorder_io import RecorderIO
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, attach, detach, set_value
from opentelemetry.sdk.trace import TracerProvider

from fi_instrumentation.attachments import AttachmentConfig, AttachmentManager, CapturePolicy
from fi_instrumentation.instrumentation.config import TraceConfig
from traceai_livekit import bind_session, import_completed_egress


def owned_session(tmp_path, *, recording=True):
    """Real AgentSession, RecorderIO and report method; no RTC/network startup."""
    provider = TracerProvider()
    span = provider.get_tracer("synthetic").start_span("actual-conversation")
    session = AgentSession()
    session._session_span = span
    session._opts.recording_options = {"audio": True, "redaction": False}
    recorder = RecorderIO(agent_session=session)
    recorder._output_path = tmp_path / "audio.ogg"
    session._recorder_io = recorder if recording else None
    # Build only the job shell that make_session_report needs. Constructors that
    # connect to an SFU are not exercised by these deterministic components.
    job = JobContext.__new__(JobContext)
    job._session_directory = tmp_path
    job._primary_agent_session = session
    job._redaction_enabled = False
    job._tagger = SimpleNamespace(tags=set())
    job._info = SimpleNamespace(job=api.Job(id="synthetic-job", room=api.Room(name="synthetic", sid="synthetic-room")))
    original_calls = []

    async def original():
        original_calls.append("original")

    job._on_session_end = original
    return session, job, recorder, span, provider, original_calls


def make_ogg(path):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=0.1:sample_rate=48000", "-ac", "2",
                    "-c:a", "libopus", "-y", str(path)], check=True, capture_output=True)
    return path.read_bytes()


@pytest.mark.asyncio
async def test_real_report_tls_upload_survives_span_end_before_cleanup(server, tmp_path):
    wire, state = server
    manager = AttachmentManager(config=AttachmentConfig(enabled=True), project_name="synthetic", transport=wire)
    assert manager.ready(2)
    session, job, recorder, span, provider, calls = owned_session(tmp_path)
    raw = make_ogg(tmp_path / "audio.ogg")
    # This is the real SDK report, not a report-path test double.
    assert job.make_session_report(session).audio_recording_path == recorder.output_path
    binding = bind_session(session, job, manager=manager, owned_recording_files=True)
    assert binding.status == "session_bound"
    expected_trace = format(span.get_span_context().trace_id, "032x")
    span.end()
    session._session_span = None
    try:
        with provider.get_tracer("unrelated").start_as_current_span("another-session"):
            await job._on_session_end()
        assert binding.status == "validating"
        assert state["payloads"] == [raw]
        request = next(body for path, body, _ in state["requests"] if path.endswith("/uploads"))
        assert request["association"]["parent"]["trace_id"].replace("-", "") == expected_trace
        assert request["direction"] == "mixed"
        assert "audio.ogg" not in str(state["requests"])
        assert calls == ["original"]
        # Independent codec decode verifies actual bytes and channel count.
        with av.open(io.BytesIO(state["payloads"][0])) as media:
            frames = list(media.decode(audio=0))
            assert sum(frame.samples for frame in frames) == 4800
            assert len(frames[0].layout.channels) == 2
        os.unlink(tmp_path / "audio.ogg")
        assert await binding.finalize() == "validating"
        assert binding._session is None and binding._job is None
    finally:
        manager.shutdown(1)
        provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_INPUT_MESSAGES",
                                 "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT",
                                 "FI_PII_REDACTION"])
async def test_denial_precedes_sdk_report_or_filesystem(manager, tmp_path, monkeypatch, flag):
    session, job, _, _, provider, _ = owned_session(tmp_path)
    binding = bind_session(session, job, manager=manager, owned_recording_files=True)
    monkeypatch.setenv(flag, "true")
    def forbidden(*args, **kwargs):
        pytest.fail("denied capture inspected a report or file")
    monkeypatch.setattr(job, "make_session_report", forbidden)
    monkeypatch.setattr("traceai_livekit.attachments.RecordingFileScope", forbidden)
    assert await binding.finalize() in ("policy_denied", "redaction_unavailable")
    assert binding.handle is None
    provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["absent", "missing", "symlink", "hardlink", "foreign_path", "active", "wrong_session",
                                  "recording_off", "redaction", "server_redaction"])
async def test_unavailable_or_denied_owned_recordings_never_transfer(manager, transport, tmp_path, case):
    session, job, recorder, _, provider, _ = owned_session(tmp_path, recording=case != "absent")
    path = tmp_path / "audio.ogg"
    if case not in ("absent", "missing"):
        path.write_bytes(b"synthetic")
    if case == "symlink":
        path.rename(tmp_path / "other")
        path.symlink_to(tmp_path / "other")
    elif case == "hardlink":
        os.link(path, tmp_path / "other")
    elif case == "foreign_path":
        recorder._output_path = tmp_path / "other"
    elif case == "active":
        recorder._started = True
    elif case == "recording_off":
        session._opts.recording_options["audio"] = False
    elif case == "redaction":
        session._opts.recording_options["redaction"] = True
    elif case == "server_redaction":
        job._redaction_enabled = True
    binding = bind_session(session, job, manager=manager, owned_recording_files=True)
    if case == "wrong_session":
        job._primary_agent_session = AgentSession()
    assert await binding.finalize() in ("source_unavailable", "policy_denied", "redaction_unavailable")
    assert not transport.payloads
    provider.shutdown()


@pytest.mark.asyncio
async def test_delayed_transfer_deadline_and_original_callback(manager, transport, tmp_path):
    session, job, _, _, provider, calls = owned_session(tmp_path)
    (tmp_path / "audio.ogg").write_bytes(b"synthetic")
    transport.release.clear()
    binding = bind_session(session, job, manager=manager, owned_recording_files=True, finalization_timeout=.1)
    start = time.monotonic()
    await job._on_session_end()
    assert time.monotonic() - start < 1
    assert binding.status == "incomplete" and calls == ["original"]
    assert binding._scope is None
    transport.release.set()
    assert (await manager.flush_async(2)).unfinished == 0
    assert not transport.payloads
    provider.shutdown()


@pytest.mark.asyncio
async def test_file_replacement_after_identity_snapshot_is_rejected(manager, transport, tmp_path, monkeypatch):
    session, job, _, _, provider, _ = owned_session(tmp_path)
    path = tmp_path / "audio.ogg"
    path.write_bytes(b"owned")
    original = manager.attach
    def replace_before_attach(*args, **kwargs):
        path.rename(tmp_path / "original")
        path.write_bytes(b"rogue")
        return original(*args, **kwargs)
    monkeypatch.setattr(manager, "attach", replace_before_attach)
    binding = bind_session(session, job, manager=manager, owned_recording_files=True)
    assert await binding.finalize() == "source_unavailable"
    assert not transport.payloads
    provider.shutdown()


@pytest.mark.asyncio
async def test_oversize_file_preserves_caller_position(manager, transport, tmp_path):
    session, job, _, _, provider, _ = owned_session(tmp_path)
    path = tmp_path / "audio.ogg"
    with path.open("wb") as file:
        file.truncate(8 * 1024 * 1024 + 1)
    with path.open("rb") as file:
        file.seek(3)
        binding = bind_session(session, job, manager=manager, owned_recording_files=True)
        assert await binding.finalize() == "too_large"
        assert file.tell() == 3
    assert not transport.payloads
    provider.shutdown()


@pytest.mark.asyncio
async def test_disable_unstarted_and_duplicate_binding(manager, tmp_path):
    session, job, _, span, provider, _ = owned_session(tmp_path)
    original = job._on_session_end
    assert bind_session(session, job, manager=manager).status == "disabled"
    assert job._on_session_end == original
    binding = bind_session(session, job, manager=manager, owned_recording_files=True)
    assert bind_session(session, job, manager=manager, owned_recording_files=True) is binding
    with pytest.raises(ValueError, match="conflicting"):
        bind_session(session, job, manager=manager, owned_recording_files=True, finalization_timeout=1)
    binding.close()
    assert job._on_session_end == original
    session._session_span = None
    assert bind_session(session, job, manager=manager, owned_recording_files=True).status == "correlation_unavailable"
    span.end()
    provider.shutdown()


@pytest.mark.asyncio
async def test_cancellation_preserves_original_and_closes_scope(manager, transport, tmp_path):
    session, job, _, _, provider, calls = owned_session(tmp_path)
    (tmp_path / "audio.ogg").write_bytes(b"synthetic")
    transport.release.clear()
    binding = bind_session(session, job, manager=manager, owned_recording_files=True, finalization_timeout=.2)
    task = asyncio.create_task(job._on_session_end())
    assert await asyncio.to_thread(transport.entered.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert binding.status == "incomplete" and calls == ["original"]
    assert binding._scope is None
    transport.release.set()
    await manager.flush_async(2)
    provider.shutdown()


@pytest.mark.asyncio
async def test_policy_tightening_cancels_admitted_file(manager, transport, tmp_path):
    session, job, _, _, provider, _ = owned_session(tmp_path)
    (tmp_path / "audio.ogg").write_bytes(b"synthetic")
    transport.release.clear()
    binding = bind_session(session, job, manager=manager, owned_recording_files=True)
    task = asyncio.create_task(binding.finalize())
    assert await asyncio.to_thread(transport.entered.wait, 2)
    manager.update_policy(replace(transport.policy, enabled=False))
    transport.release.set()
    assert await task == "policy_denied"
    assert not transport.payloads
    provider.shutdown()


def test_exact_worker_lifecycle_orders_close_hook_before_cleanup():
    # Version-locked source assertions complement (not replace) the synthetic
    # hook exercise. A whole Worker/SFU shutdown is a separate Q02 gate.
    from livekit.agents.ipc.job_proc_lazy_main import _JobProc
    shutdown = inspect.getsource(_JobProc._shutdown_job)
    runner = inspect.getsource(_JobProc._run_job_task)
    assert shutdown.index("session.aclose()") < shutdown.index("await self._job_ctx._on_session_end()")
    assert runner.index("await self._shutdown_job(") < runner.index("await self._job_ctx._on_cleanup()")
    assert version("livekit-agents") in ("1.8.10", "1.8.11")


@pytest.mark.parametrize("state,expected", [(api.EGRESS_STARTING, "waiting_for_completion"),
    (api.EGRESS_ACTIVE, "waiting_for_completion"), (api.EGRESS_ENDING, "waiting_for_completion"),
    (api.EGRESS_COMPLETE, "source_unavailable"), (api.EGRESS_FAILED, "source_unavailable")])
def test_egress_never_marks_start_stop_or_complete_as_playable(manager, transport, state, expected):
    from fi_instrumentation.attachments import CorrelationHandle
    parent = CorrelationHandle("1" * 32, "2" * 16, True)
    manager.update_policy(replace(transport.policy, allowed_sources=frozenset(("ProviderFile",))))
    info = api.EgressInfo(egress_id="synthetic", room_name="room", status=state, ended_at=1)
    info.file_results.add(location="https://private.invalid/CANARY_SIGNED_PATH?token=SECRET")
    handle = import_completed_egress(info, session_context=parent, egress_id="synthetic", room_name="room",
                                     manager=manager, existing_source_authorized=True)
    assert handle.admission == expected
    assert not transport.requests and not transport.payloads
    assert "CANARY" not in repr(handle)


def test_egress_no_authority_does_not_inspect_response(manager):
    class Poison:
        def __getattribute__(self, name):
            raise AssertionError("denied object inspected")
    assert import_completed_egress(Poison(), session_context=None, egress_id="", room_name="", manager=manager).admission == "disabled"


@pytest.mark.asyncio
async def test_adapter_exception_never_logs_source_or_replaces_original(manager, tmp_path, monkeypatch, caplog):
    session, job, _, _, provider, calls = owned_session(tmp_path)
    binding = bind_session(session, job, manager=manager, owned_recording_files=True)
    def fail(*args):
        raise ValueError("CANARY_PRIVATE_RECORDING_PATH")
    monkeypatch.setattr(job, "make_session_report", fail)
    await job._on_session_end()
    assert binding.status == "source_unavailable" and calls == ["original"]
    assert "CANARY" not in caplog.text
    provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["environment", "provider"])
async def test_native_livekit_pii_denial_wins(manager, transport, tmp_path, monkeypatch, mode):
    from livekit.agents.telemetry.pii import _PIIFilteringSpanProcessor
    session, job, _, span, provider, _ = owned_session(tmp_path)
    processor = _PIIFilteringSpanProcessor(allow_pii=False)
    if mode == "provider":
        provider.add_span_processor(processor)
    else:
        monkeypatch.setenv("LIVEKIT_TELEMETRY_ALLOW_PII", "0")
    job._session_directory = object()  # Path conversion must never be reached.
    binding = bind_session(session, job, manager=manager, owned_recording_files=True)
    assert await binding.finalize() == "policy_denied"
    assert not transport.payloads
    span.end()
    provider.shutdown()


@pytest.mark.asyncio
async def test_suppression_and_unsampled_context_never_open_files(manager, tmp_path):
    from opentelemetry import trace
    session, job, _, span, provider, _ = owned_session(tmp_path)
    token = attach(set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        assert bind_session(session, job, manager=manager, owned_recording_files=True).status == "policy_denied"
    finally:
        detach(token)
    session._session_span = trace.NonRecordingSpan(trace.SpanContext(1, 2, False, trace.TraceFlags(0)))
    assert bind_session(session, job, manager=manager, owned_recording_files=True).status == "unsampled"
    span.end()
    provider.shutdown()


def test_cancelled_finalization_does_not_hold_process_for_full_deadline():
    import sys
    from pathlib import Path
    result = subprocess.run([sys.executable, str(Path(__file__).with_name("process_exit_probe.py"))],
                            timeout=10, check=True, capture_output=True, text=True)
    assert "bounded-cancel-exit" in result.stdout
