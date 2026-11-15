"""V01: real Pipecat AudioBufferProcessor -> core RecordingWriter (segments + manifest).

The pipeline tests run a real PipelineWorker with Pipecat's own tracing, so the
recording binds to the conversation span Pipecat itself started. Audio is a
synthetic tone; the transport is the core's synthetic F01 double. This is not a
real SFU call or a product-playback claim (Q02).
"""

import asyncio
import io
import json
import math
import struct
import threading
import wave
from dataclasses import replace

import pytest
from opentelemetry import trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, attach, detach, set_value
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from conftest import TransportDouble, make_manager
from fi_instrumentation.attachments import AttachmentConfig, AttachmentManager, CapturePolicy, CorrelationHandle
from fi_instrumentation.attachments._disabled import DisabledAttachmentManager
from pipecat.frames.frames import (
    BotStartedSpeakingFrame, BotStoppedSpeakingFrame, EndFrame, InputAudioRawFrame, OutputAudioRawFrame,
    UserStartedSpeakingFrame, UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor, TurnAudioData
from pipecat.workers.runner import WorkerRunner
from traceai_pipecat import bind_audio_processor, create_audio_capture
from traceai_pipecat.attachments import AudioSessionContext, TurnAudioCaps, mark_conversation_root

# Pipecat's TurnTraceObserver uses the GLOBAL tracer provider.
EXPORTER = InMemorySpanExporter()
PROVIDER = TracerProvider()
PROVIDER.add_span_processor(SimpleSpanProcessor(EXPORTER))
trace.set_tracer_provider(PROVIDER)
TRACER = PROVIDER.get_tracer("synthetic")
RATE = 16000


def tone(freq, ms, rate=RATE, amplitude=8000):
    count = rate * ms // 1000
    return b"".join(struct.pack("<h", int(amplitude * math.sin(2 * math.pi * freq * i / rate)))
                    for i in range(count))


def handler_counts(processor):
    return {name: len(handler.handlers) for name, handler in processor._event_handlers.items()}


def pcm(payload):
    with wave.open(io.BytesIO(payload), "rb") as decoded:
        return decoded.getnchannels(), decoded.getframerate(), decoded.readframes(decoded.getnframes())


def segment_map(transport):
    """occurrence_id -> uploaded payload (requests and payloads are in lane order)."""
    uploads = [r for r in transport.requests if "recording_id" in r]
    assert len(uploads) == len(transport.payloads)
    return {r["occurrence_id"]: (r, p) for r, p in zip(uploads, transport.payloads)}


def final_manifest(transport):
    assert transport.manifests, "no manifest published"
    return transport.manifests[-1][1]


async def run_pipeline(processor, frames, *, conversation_id="conv-synthetic", parent=None):
    worker = PipelineWorker(Pipeline([processor]),
                            params=PipelineParams(audio_in_sample_rate=RATE, audio_out_sample_rate=RATE),
                            enable_tracing=True, enable_turn_tracking=True, conversation_id=conversation_id,
                            idle_timeout_secs=None, enable_rtvi=False)
    return worker


async def drive(worker, frames, parent=None):
    await worker.queue_frames(frames + [EndFrame()])
    runner = WorkerRunner(handle_sigint=False)
    # run(worker) is the form that ends with the worker on 1.15.0 (it warns as
    # deprecated); add_workers() + run() kept running after EndFrame here.
    if parent is None:
        await asyncio.wait_for(runner.run(worker), 60)
    else:
        with trace.use_span(parent, end_on_exit=False):
            await asyncio.wait_for(runner.run(worker), 60)


def call_frames(turns=6, ms=500):
    frames = []
    for i in range(turns):
        for _ in range(ms // 20):
            frames.append(InputAudioRawFrame(audio=tone(440 + i, 20), sample_rate=RATE, num_channels=1))
        for _ in range(ms // 20):
            frames.append(OutputAudioRawFrame(audio=tone(880 + i, 20), sample_rate=RATE, num_channels=1))
    return frames


def conversation_span():
    spans = [s for s in EXPORTER.get_finished_spans() if s.name == "conversation"]
    assert len(spans) == 1
    return spans[0]


@pytest.fixture(autouse=True)
def reset_spans():
    EXPORTER.clear()
    yield


# ------------------------------------------------------------------ real pipeline
@pytest.mark.asyncio
@pytest.mark.parametrize("channels,layout", [(1, "mixed"), (2, "stereo")])
async def test_real_pipeline_whole_call_recording(manager, transport, channels, layout):
    processor = AudioBufferProcessor(sample_rate=RATE, num_channels=channels, buffer_size=16000,
                                     auto_start_recording=True)
    emitted = []
    processor.add_event_handler("on_audio_data", lambda _p, audio, rate, nch: emitted.append((audio, rate, nch)))
    worker = await run_pipeline(processor, None)
    binding = bind_audio_processor(processor, AudioSessionContext.from_pipeline(worker), manager=manager)
    assert binding.status == "session_bound"
    await drive(worker, call_frames())
    result = await binding.finish(10)
    assert result is not None and result.state == "published" and result.capture_complete, result
    assert binding.status == "published"
    assert {(r, n) for _, r, n in emitted} == {(RATE, channels)}
    manifest = final_manifest(transport)
    assert manifest["layout"] == layout and manifest["sample_rate_hz"] == RATE
    assert manifest["channels"] == channels and not manifest["gaps"] and manifest["capture_complete"]
    if layout == "stereo":
        assert [(t["channels"], t["direction"], t["role"]) for t in manifest["tracks"]] == [
            ([0], "input", "user"), ([1], "output", "assistant")]
    else:
        assert [(t["direction"], t["role"]) for t in manifest["tracks"]] == [("mixed", "mixed")]
    segments = segment_map(transport)
    uploaded = b""
    for segment in sorted(manifest["segments"], key=lambda s: s["start_ms"]):
        request, payload = segments[segment["occurrence_id"]]
        assert request["direction"] == "mixed" and request["purpose"] == "conversation_recording"
        nch, rate, frames = pcm(payload)
        assert (nch, rate) == (channels, RATE)
        uploaded += frames
    # Byte identity: exactly what Pipecat emitted, in order, nothing more.
    assert uploaded == b"".join(audio for audio, _, _ in emitted)
    assert manifest["duration_ms"] == math.ceil(len(uploaded) * 1000 / (2 * channels * RATE))
    # Bound to the conversation span Pipecat started, with its conversation id.
    root = conversation_span()
    parent = transport.requests[0]["association"]["parent"]
    assert parent["span_id"] == format(root.context.span_id, "016x")
    assert all(r["conversation_id"] == "conv-synthetic" for r in transport.requests if "recording_id" in r)
    assert root.parent is None and binding.root_marked
    assert root.attributes["fi.span.kind"] == "CONVERSATION"
    assert root.attributes["fi.attachment.schema_version"] == 1
    for span in EXPORTER.get_finished_spans():
        if span is not root:
            assert span.attributes.get("fi.span.kind") != "CONVERSATION"


@pytest.mark.asyncio
async def test_real_pipeline_per_track_manifest(manager, transport):
    processor = AudioBufferProcessor(sample_rate=RATE, num_channels=2, buffer_size=16000,
                                     auto_start_recording=True)
    emitted = []
    processor.add_event_handler("on_track_audio_data",
                                lambda _p, user, bot, rate, nch: emitted.append((user, bot)))
    worker = await run_pipeline(processor, None)
    binding = bind_audio_processor(processor, AudioSessionContext.from_pipeline(worker), manager=manager,
                                   layout="separate_tracks")
    await drive(worker, call_frames())
    result = await binding.finish(10)
    assert result.state == "published" and result.capture_complete
    manifest = final_manifest(transport)
    assert manifest["layout"] == "separate_tracks" and manifest["channels"] == 2
    user_id, bot_id = (t["track_id"] for t in manifest["tracks"])
    segments = segment_map(transport)
    per_track = {user_id: b"", bot_id: b""}
    for segment in sorted(manifest["segments"], key=lambda s: (s["start_ms"], s["track_id"])):
        request, payload = segments[segment["occurrence_id"]]
        nch, rate, frames = pcm(payload)
        assert (nch, rate) == (1, RATE)
        assert request["direction"] == ("input" if segment["track_id"] == user_id else "output")
        per_track[segment["track_id"]] += frames
    assert per_track[user_id] == b"".join(u for u, _ in emitted)
    assert per_track[bot_id] == b"".join(b for _, b in emitted)
    assert conversation_span().attributes["fi.span.kind"] == "CONVERSATION"


@pytest.mark.asyncio
async def test_conversation_with_a_parent_is_never_marked_root(manager, transport):
    processor = AudioBufferProcessor(sample_rate=RATE, num_channels=1, buffer_size=16000,
                                     auto_start_recording=True)
    worker = await run_pipeline(processor, None)
    binding = bind_audio_processor(processor, AudioSessionContext.from_pipeline(worker), manager=manager)
    app = TRACER.start_span("app-request")
    try:
        await drive(worker, call_frames(turns=2), parent=app)
    finally:
        app.end()
    result = await binding.finish(10)
    assert result.state == "published"
    root = conversation_span()
    assert root.parent is not None and root.parent.span_id == app.get_span_context().span_id
    assert not binding.root_marked and "fi.span.kind" not in root.attributes


@pytest.mark.asyncio
async def test_pipeline_without_tracing_never_uses_ambient_span(manager, transport):
    processor = AudioBufferProcessor(sample_rate=RATE, num_channels=1, buffer_size=16000,
                                     auto_start_recording=True)
    worker = PipelineWorker(Pipeline([processor]), params=PipelineParams(audio_in_sample_rate=RATE,
                            audio_out_sample_rate=RATE), enable_tracing=False, idle_timeout_secs=None,
                            enable_rtvi=False)
    binding = bind_audio_processor(processor, AudioSessionContext.from_pipeline(worker), manager=manager)
    ambient = TRACER.start_span("ambient")
    with trace.use_span(ambient, end_on_exit=True):
        await drive(worker, call_frames(turns=2))
    assert await binding.finish(5) is None
    assert binding.status == "correlation_unavailable"
    assert not transport.requests and not transport.payloads and manager.owned_bytes == 0


# ------------------------------------------------------- direct event dispatch
async def dispatch(processor, event, *args):
    """Pipecat's own dispatch: one task per handler, run to completion."""
    await processor._call_event_handler(event, *args)
    for _ in range(3):
        await asyncio.sleep(0)


def explicit_context():
    span = TRACER.start_span("explicit-conversation")
    return span, AudioSessionContext.from_span(span, conversation_id="conv-explicit")


@pytest.mark.asyncio
async def test_stereo_one_side_hidden_denies_whole_recording(manager, transport, monkeypatch):
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    processor = AudioBufferProcessor(sample_rate=RATE, num_channels=2, buffer_size=16000)
    before = handler_counts(processor)
    span, context = explicit_context()
    binding = bind_audio_processor(processor, context, manager=manager)
    assert binding.status == "policy_denied" and handler_counts(processor) == before
    span.end()
    assert not transport.requests


@pytest.mark.asyncio
async def test_separate_tracks_hidden_side_becomes_gap_other_side_uploads(manager, transport, monkeypatch):
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    processor = AudioBufferProcessor(sample_rate=RATE, num_channels=1, buffer_size=16000)
    span, context = explicit_context()
    binding = bind_audio_processor(processor, context, manager=manager, layout="separate_tracks")
    assert binding.status == "session_bound"
    for i in range(4):
        await dispatch(processor, "on_track_audio_data", tone(440, 500), tone(880, 500), RATE, 1)
    await dispatch(processor, "on_recording_stopped")
    result = await binding.finish(10)
    span.end()
    assert result.state == "partial" and not result.capture_complete
    manifest = final_manifest(transport)
    user_id, bot_id = (t["track_id"] for t in manifest["tracks"])
    assert {s["track_id"] for s in manifest["segments"]} == {bot_id}
    assert {(g["track_id"], g["reason"]) for g in manifest["gaps"]} == {(user_id, "policy_denied")}
    assert all(r["direction"] == "output" for r in transport.requests if "recording_id" in r)
    assert b"".join(pcm(p)[2] for p in transport.payloads) == tone(880, 500) * 4


@pytest.mark.parametrize("buffer_size,turns", [(0, False), (16000, True), (128 * 1024 + 1, False)])
def test_unbounded_upstream_is_refused_and_untouched(manager, transport, buffer_size, turns):
    processor = AudioBufferProcessor(sample_rate=RATE, buffer_size=buffer_size, enable_turn_audio=turns)
    before = handler_counts(processor)
    span, context = explicit_context()
    assert bind_audio_processor(processor, context, manager=manager).status == "unbounded_source"
    assert handler_counts(processor) == before and not processor._recording
    span.end()


@pytest.mark.parametrize("flags", [("FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS")])
def test_policy_denial_precedes_processor_access(manager, monkeypatch, flags):
    class Poison:
        def __getattribute__(self, name):
            raise AssertionError("denied processor accessed")
    for flag in flags:
        monkeypatch.setenv(flag, "true")
    span, context = explicit_context()
    assert bind_audio_processor(Poison(), context, manager=manager).status == "policy_denied"
    span.end()


def test_suppressed_instrumentation_binds_nothing(manager):
    processor = AudioBufferProcessor(sample_rate=RATE, buffer_size=16000)
    before = handler_counts(processor)
    span, context = explicit_context()
    token = attach(set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        assert bind_audio_processor(processor, context, manager=manager).status == "policy_denied"
    finally:
        detach(token)
    assert handler_counts(processor) == before
    span.end()


def test_off_by_default_registers_nothing_and_writes_no_attributes():
    class Poison:
        def __getattribute__(self, name):
            raise AssertionError("disabled adapter touched its arguments")
    for manager in (AttachmentManager(config=AttachmentConfig()), DisabledAttachmentManager()):
        try:
            assert bind_audio_processor(Poison(), Poison(), manager=manager).status == "disabled"
            assert create_audio_capture(Poison(), manager=manager).status == "disabled"
            assert manager.owned_bytes == 0
        finally:
            manager.shutdown(0)


@pytest.mark.asyncio
async def test_unsampled_conversation_retains_nothing(manager, transport):
    processor = AudioBufferProcessor(sample_rate=RATE, buffer_size=16000)
    context = AudioSessionContext(correlation=CorrelationHandle("1" * 32, "2" * 16, False))
    binding = bind_audio_processor(processor, context, manager=manager)
    await dispatch(processor, "on_audio_data", tone(440, 500), RATE, 1)
    assert binding.status == "unsampled" and manager.owned_bytes == 0 and not transport.requests


def test_create_capture_needs_local_and_server_recording_permission(transport):
    span, context = explicit_context()
    manager = make_manager(transport)
    try:
        assert create_audio_capture(context, manager=manager).status == "recording_disabled"
    finally:
        manager.shutdown(1)
    transport.policy = replace(transport.policy, record_audio=False)
    manager = make_manager(transport, record_audio=True)
    try:
        assert create_audio_capture(context, manager=manager).status == "recording_disabled"
    finally:
        manager.shutdown(1)
    transport.policy = replace(transport.policy, record_audio=True)
    manager = make_manager(transport, record_audio=True)
    try:
        binding = create_audio_capture(context, manager=manager, num_channels=2)
        assert binding.status == "session_bound"
        processor = binding.processor
        assert isinstance(processor, AudioBufferProcessor) and processor.num_channels == 2
        assert not processor._recording and not processor._auto_start_recording
        binding.close()
    finally:
        manager.shutdown(1)
    span.end()


@pytest.mark.asyncio
async def test_blocked_uploader_turns_overflow_into_gaps_not_memory(transport):
    manager = make_manager(transport)
    transport.release.clear()
    processor = AudioBufferProcessor(sample_rate=RATE, buffer_size=32000)
    span, context = explicit_context()
    binding = bind_audio_processor(processor, context, manager=manager)
    peak = 0
    try:
        for _ in range(150):  # 150 s of mono audio while the uploader is stuck
            await dispatch(processor, "on_audio_data", tone(440, 1000), RATE, 1)
            peak = max(peak, manager.owned_bytes)
        assert peak <= manager.config.max_owned_bytes
        # Two 30 s segments may wait (window_segments=2); the rest is a gap.
        assert peak <= 2 * (30 * RATE * 2) + 3 * RATE * 2
        transport.release.set()
        await dispatch(processor, "on_recording_stopped")
        result = await binding.finish(20)
    finally:
        transport.release.set()
        manager.shutdown(2)
        span.end()
    assert result.state == "partial" and not result.capture_complete
    manifest = final_manifest(transport)
    assert any(g["reason"] == "queue_full" for g in manifest["gaps"])
    assert manifest["duration_ms"] == 150000


@pytest.mark.asyncio
async def test_policy_tightening_mid_recording_stops_retention(manager, transport):
    processor = AudioBufferProcessor(sample_rate=RATE, buffer_size=16000)
    span, context = explicit_context()
    binding = bind_audio_processor(processor, context, manager=manager)
    for _ in range(35):
        await dispatch(processor, "on_audio_data", tone(440, 1000), RATE, 1)
    manager.update_policy(replace(transport.policy, enabled=False))
    for _ in range(5):
        await dispatch(processor, "on_audio_data", tone(440, 1000), RATE, 1)
    await dispatch(processor, "on_recording_stopped")
    result = await binding.finish(10)
    span.end()
    assert result.state in ("partial", "unpublished") and not result.capture_complete
    assert sum(len(pcm(p)[2]) for p in transport.payloads) <= 35 * RATE * 2


@pytest.mark.asyncio
async def test_format_drift_keeps_clock_with_explicit_gap(manager, transport):
    processor = AudioBufferProcessor(sample_rate=RATE, buffer_size=16000)
    span, context = explicit_context()
    binding = bind_audio_processor(processor, context, manager=manager)
    await dispatch(processor, "on_audio_data", tone(440, 500), RATE, 1)
    await dispatch(processor, "on_audio_data", tone(440, 500, rate=8000), 8000, 1)
    await dispatch(processor, "on_audio_data", tone(440, 500), RATE, 1)
    await dispatch(processor, "on_recording_stopped")
    result = await binding.finish(10)
    span.end()
    manifest = final_manifest(transport)
    assert result.state == "partial"
    assert [(g["start_ms"], g["duration_ms"], g["reason"]) for g in manifest["gaps"]] == [
        (500, 500, "source_unavailable")]
    assert manifest["duration_ms"] == 1500


@pytest.mark.asyncio
async def test_each_start_stop_cycle_is_its_own_recording(manager, transport):
    processor = AudioBufferProcessor(sample_rate=RATE, buffer_size=16000)
    span, context = explicit_context()
    binding = bind_audio_processor(processor, context, manager=manager)
    await dispatch(processor, "on_audio_data", tone(440, 500), RATE, 1)
    await dispatch(processor, "on_recording_stopped")
    # The stop closes the cycle without the app calling finish(); its upload
    # runs in the background and the binding is ready for the next start.
    for _ in range(200):
        if binding.results:
            break
        await asyncio.sleep(0.01)
    assert [r.state for r in binding.results] == ["published"]
    await dispatch(processor, "on_audio_data", tone(660, 700), RATE, 1)
    await dispatch(processor, "on_recording_stopped")
    await binding.finish(10)
    span.end()
    assert [r.state for r in binding.results] == ["published", "published"]
    first, second = binding.results
    assert first.recording_id != second.recording_id
    durations = {m["recording_id"]: m["duration_ms"] for _, m, _, _ in transport.manifests}
    assert durations == {first.recording_id: 500, second.recording_id: 700}


@pytest.mark.asyncio
async def test_handler_failure_is_isolated_and_content_free(manager, transport, caplog):
    class Exploding:
        @property
        def turn_trace_observer(self):
            raise RuntimeError("CANARY_PRIVATE_DETAIL")
    processor = AudioBufferProcessor(sample_rate=RATE, buffer_size=16000)
    binding = bind_audio_processor(processor, AudioSessionContext.from_pipeline(Exploding()), manager=manager)
    await dispatch(processor, "on_audio_data", tone(440, 500), RATE, 1)
    assert binding.status == "source_unavailable"
    assert "CANARY" not in caplog.text and not transport.requests and manager.owned_bytes == 0


def test_duplicate_conflicting_and_detach(manager):
    processor = AudioBufferProcessor(sample_rate=RATE, buffer_size=16000)
    before = handler_counts(processor)
    span, context = explicit_context()
    binding = bind_audio_processor(processor, context, manager=manager)
    assert bind_audio_processor(processor, context, manager=manager) is binding
    with pytest.raises(ValueError, match="conflicting"):
        bind_audio_processor(processor, context, manager=manager, layout="separate_tracks")
    assert handler_counts(processor) != before
    binding.close()
    assert handler_counts(processor) == before and not processor._recording
    assert bind_audio_processor(processor, context, manager=manager) is not binding
    span.end()


def test_unknown_upstream_fails_closed(manager, monkeypatch):
    monkeypatch.setattr("traceai_pipecat.attachments.version", lambda _: "1.14.0")
    span, context = explicit_context()
    processor = AudioBufferProcessor(sample_rate=RATE, buffer_size=16000)
    assert bind_audio_processor(processor, context, manager=manager).status == "unsupported_version"
    span.end()


def test_root_marker_only_on_true_parentless_live_span():
    root = TRACER.start_span("root")
    child = TRACER.start_span("child", context=trace.set_span_in_context(root))
    remote = TRACER.start_span("remote-child", context=trace.set_span_in_context(trace.NonRecordingSpan(
        trace.SpanContext(int("1" * 32, 16), int("2" * 16, 16), True, trace.TraceFlags(1)))))
    assert mark_conversation_root(root) and root.attributes["fi.span.kind"] == "CONVERSATION"
    assert root.attributes["gen_ai.span.kind"] == "CONVERSATION"
    assert not mark_conversation_root(child) and "fi.span.kind" not in child.attributes
    assert not mark_conversation_root(remote) and "fi.span.kind" not in remote.attributes
    assert not mark_conversation_root(trace.NonRecordingSpan(root.get_span_context()))
    for span in (remote, child, root):
        span.end()
    ended = TRACER.start_span("ended")
    ended.end()
    assert not mark_conversation_root(ended)


def test_no_private_manager_internals_in_voice_adapters():
    import inspect

    import traceai_pipecat.attachments as pipecat
    for module in (pipecat,):
        source = inspect.getsource(module)
        for private in ("manager._session", "manager.policy", "._fd", "manager._", "_manager._"):
            assert private not in source, (module.__name__, private)


# ------------------------------------------------------------- short calls
@pytest.mark.asyncio
async def test_short_call_binds_to_snapshot_after_worker_ended_conversation(manager, transport):
    """Pipecat runs on_audio_data as a task: a call shorter than buffer_size
    delivers its only chunk on stop, which can run after the worker's cleanup
    has ended and cleared the conversation span."""
    processor = AudioBufferProcessor(sample_rate=RATE, num_channels=1, buffer_size=128 * 1024)
    worker = await run_pipeline(processor, None)
    binding = bind_audio_processor(processor, AudioSessionContext.from_pipeline(worker), manager=manager)
    observer = worker.turn_trace_observer
    observer.start_conversation_tracing("conv-short")
    await dispatch(processor, "on_recording_started")
    live_id = format(observer._conversation_span.get_span_context().span_id, "016x")
    observer.end_conversation_tracing()
    assert observer._conversation_span is None
    await dispatch(processor, "on_audio_data", tone(440, 300), RATE, 1)
    await dispatch(processor, "on_recording_stopped")
    result = await binding.finish(10)
    assert result is not None and result.state == "published", (binding.status, result)
    uploads = [r for r in transport.requests if "recording_id" in r]
    assert uploads and all(r["association"]["parent"]["span_id"] == live_id for r in uploads)
    assert all(r["conversation_id"] == "conv-short" for r in uploads)
    root = conversation_span()
    assert format(root.context.span_id, "016x") == live_id
    # Marked while it was still live, because the audio came after it ended.
    assert binding.root_marked and root.attributes["fi.span.kind"] == "CONVERSATION"
    assert pcm(transport.payloads[0])[2] == tone(440, 300)


@pytest.mark.asyncio
async def test_no_snapshot_and_no_span_stays_correlation_unavailable(manager, transport):
    processor = AudioBufferProcessor(sample_rate=RATE, num_channels=1, buffer_size=16000)
    worker = await run_pipeline(processor, None)
    binding = bind_audio_processor(processor, AudioSessionContext.from_pipeline(worker), manager=manager)
    ambient = TRACER.start_span("ambient")
    with trace.use_span(ambient, end_on_exit=True):
        await dispatch(processor, "on_audio_data", tone(440, 300), RATE, 1)
    assert binding.status == "correlation_unavailable"
    assert not transport.requests and manager.owned_bytes == 0


@pytest.mark.asyncio
async def test_real_pipeline_short_call_under_buffer_size_is_recorded(manager, transport):
    processor = AudioBufferProcessor(sample_rate=RATE, num_channels=1, buffer_size=128 * 1024,
                                     auto_start_recording=True)
    worker = await run_pipeline(processor, None)
    binding = bind_audio_processor(processor, AudioSessionContext.from_pipeline(worker), manager=manager)
    await drive(worker, call_frames(turns=1, ms=200))
    result = await binding.finish(10)
    assert result is not None and result.state == "published", (binding.status, result)
    root = conversation_span()
    parent = transport.requests[0]["association"]["parent"]
    assert parent["span_id"] == format(root.context.span_id, "016x")
    assert binding.root_marked and root.attributes["fi.span.kind"] == "CONVERSATION"


# ------------------------------------------------------------ per-turn audio
CAPS = TurnAudioCaps(max_turns=100, max_bytes=8 * 1024 * 1024, max_duration_ms=600000)


def turn_frames(turns=3, ms=200):
    frames = []
    for i in range(turns):
        frames.append(UserStartedSpeakingFrame())
        frames += [InputAudioRawFrame(audio=tone(300 + i, 20), sample_rate=RATE, num_channels=1)
                   for _ in range(ms // 20)]
        frames.append(UserStoppedSpeakingFrame())
        frames.append(BotStartedSpeakingFrame())
        frames += [OutputAudioRawFrame(audio=tone(900 + i, 20), sample_rate=RATE, num_channels=1)
                   for _ in range(ms // 20)]
        frames.append(BotStoppedSpeakingFrame())
    return frames


def pair_uploads(transport):
    """Record (UploadRequest, payload) per upload, paired on the worker thread that ran both.

    The core runs two upload workers, so the double's flat request and payload
    lists can interleave; one job's initialize and content PUT share a thread.
    """
    pairs, local = [], threading.local()
    initialize = transport.initialize

    def paired_initialize(session, body, **kwargs):
        upload = initialize(session, body, **kwargs)
        local.request = json.loads(body)
        return upload

    class Payloads(list):
        def append(self, data):
            super().append(data)
            pairs.append((getattr(local, "request", None), data))

    transport.initialize = paired_initialize
    transport.payloads = Payloads(transport.payloads)
    return pairs


def turn_pairs(pairs):
    return [(r, p) for r, p in pairs if r is not None and r.get("purpose") == "turn_recording"]


@pytest.mark.asyncio
async def test_turn_audio_is_off_by_default_and_needs_explicit_caps(manager):
    span, context = explicit_context()
    turns = AudioBufferProcessor(sample_rate=RATE, buffer_size=16000, enable_turn_audio=True)
    before = handler_counts(turns)
    assert bind_audio_processor(turns, context, manager=manager).status == "unbounded_source"
    assert handler_counts(turns) == before
    plain = AudioBufferProcessor(sample_rate=RATE, buffer_size=16000)
    with pytest.raises(ValueError, match="enable_turn_audio"):
        bind_audio_processor(plain, context, manager=manager, turn_audio=CAPS)
    with pytest.raises(ValueError):
        bind_audio_processor(turns, context, manager=manager, turn_audio={"max_turns": 1})
    for bad in ((0, 1, 1), (1, 0, 1), (1, 1, 0), (1001, 1, 1), (1, 64 * 1024 * 1024 + 1, 1),
                (1, 1, 2 * 60 * 60 * 1000 + 1), (1.0, 1, 1), (True, 1, 1)):
        with pytest.raises(ValueError):
            TurnAudioCaps(*bad)
    binding = bind_audio_processor(plain, context, manager=manager)
    assert binding.turn_status == "off" and "on_user_turn_audio" not in [e for e, _ in binding._handlers]
    binding.close()
    span.end()


@pytest.mark.asyncio
async def test_real_pipeline_uploads_each_turn_bound_to_its_turn_span(manager, transport):
    processor = AudioBufferProcessor(sample_rate=RATE, num_channels=1, buffer_size=16000,
                                     auto_start_recording=True, enable_turn_audio=True)
    emitted = []
    processor.add_event_handler("on_user_turn_audio", lambda _p, d: emitted.append(("input", d.turn_number, d.audio)))
    processor.add_event_handler("on_bot_turn_audio", lambda _p, d: emitted.append(("output", d.turn_number, d.audio)))
    worker = await run_pipeline(processor, None)
    binding = bind_audio_processor(processor, AudioSessionContext.from_pipeline(worker), manager=manager,
                                   turn_audio=CAPS)
    assert binding.turn_status == "session_bound"
    pairs = pair_uploads(transport)
    await drive(worker, turn_frames(turns=3))
    result = await binding.finish(10)
    assert result is not None and result.state == "published"
    assert emitted, "Pipecat emitted no turn audio"
    turn_spans = {s.attributes["turn.number"]: s for s in EXPORTER.get_finished_spans() if s.name == "turn"}
    uploads = turn_pairs(pairs)
    assert len(uploads) == len(emitted) == len(binding.turn_handles)
    got = {}
    for request, payload in uploads:
        number = int(request["turn_id"])
        assert request["association"]["parent"]["span_id"] == format(turn_spans[number].context.span_id, "016x")
        assert request["conversation_id"] == "conv-synthetic" and request["mime_type"] == "audio/wav"
        nch, rate, frames = pcm(payload)
        assert (nch, rate) == (1, RATE)
        got[(request["direction"], number)] = frames
    assert got == {(direction, number): audio for direction, number, audio in emitted}
    # Queued synthetic frames: Bot/User*SpeakingFrame are system frames and can
    # overtake queued audio, so which turns Pipecat emits here depends on its
    # scheduling. Every emitted turn must upload, byte-identical, on its own
    # turn span; the bot side is covered by the dispatch tests below.
    assert all(direction in ("input", "output") for direction, _ in got)
    assert all(h.result(0).status != "unfinished" for _, _, h in binding.turn_handles)
    # The whole-call recording is unchanged and still binds to the conversation.
    root = conversation_span()
    recording = [r for r in transport.requests if "recording_id" in r]
    assert recording and recording[0]["association"]["parent"]["span_id"] == format(root.context.span_id, "016x")
    assert manager.owned_bytes == 0


async def turn_binding(manager, caps=CAPS, *, context=None, layout="auto", capture=None):
    processor = AudioBufferProcessor(sample_rate=RATE, num_channels=1, buffer_size=16000, enable_turn_audio=True)
    worker = await run_pipeline(processor, None)
    observer = worker.turn_trace_observer
    observer.start_conversation_tracing("conv-turns")
    binding = bind_audio_processor(processor, context or AudioSessionContext.from_pipeline(worker),
                                   manager=manager, turn_audio=caps, layout=layout, capture_policy=capture)
    return processor, observer, binding


async def run_turn(processor, observer, number, user_ms=500, bot_ms=500):
    await observer._turn_tracker._call_event_handler("on_turn_started", number)
    for _ in range(3):
        await asyncio.sleep(0)
    await observer._turn_tracker._call_event_handler("on_turn_ended", number, 1.0, False)
    for _ in range(3):
        await asyncio.sleep(0)
    if user_ms:
        await dispatch(processor, "on_user_turn_audio", TurnAudioData(number, tone(300, user_ms), RATE, 1))
    if bot_ms:
        await dispatch(processor, "on_bot_turn_audio", TurnAudioData(number, tone(900, bot_ms), RATE, 1))


@pytest.mark.asyncio
async def test_turn_count_cap_skips_whole_turns(manager, transport):
    processor, observer, binding = await turn_binding(
        manager, TurnAudioCaps(max_turns=2, max_bytes=8 * 1024 * 1024, max_duration_ms=600000))
    for number in (1, 2, 3):
        await run_turn(processor, observer, number)
    await binding.finish(10)
    observer.end_conversation_tracing()
    uploads = [r for r in transport.requests if r.get("purpose") == "turn_recording"]
    assert sorted((r["turn_id"], r["direction"]) for r in uploads) == [("1", "input"), ("1", "output")]
    assert binding.turn_denials == {"queue_full": 4} and binding.turn_status == "queue_full"
    assert len(binding.turn_handles) == 2


@pytest.mark.asyncio
async def test_turn_byte_and_duration_caps_never_truncate(manager, transport):
    one_turn = 44 + len(tone(300, 500))
    pairs = pair_uploads(transport)
    processor, observer, binding = await turn_binding(
        manager, TurnAudioCaps(max_turns=100, max_bytes=one_turn + 10, max_duration_ms=600000))
    await run_turn(processor, observer, 1)
    await binding.finish(10)
    uploads = turn_pairs(pairs)
    assert [(r["direction"], r["declared_bytes"]) for r, _ in uploads] == [("input", one_turn)]
    assert pcm(uploads[0][1])[2] == tone(300, 500)
    assert binding.turn_denials == {"too_large": 1}
    binding.close()
    observer.end_conversation_tracing()
    # Duration cap: 500 ms fits under 700 ms, the bot's 500 ms more does not.
    processor, observer, binding = await turn_binding(
        manager, TurnAudioCaps(max_turns=100, max_bytes=8 * 1024 * 1024, max_duration_ms=700))
    await run_turn(processor, observer, 1)
    await binding.finish(10)
    uploads = turn_pairs(pairs)[1:]
    assert [r["direction"] for r, _ in uploads] == ["input"]
    assert binding.turn_denials == {"too_large": 1}
    observer.end_conversation_tracing()


@pytest.mark.asyncio
async def test_hidden_side_turns_are_denied_other_side_uploads(manager, transport, monkeypatch):
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    # A mono mixed recording is denied whole under a hide flag; per-side tracks are not.
    processor, observer, binding = await turn_binding(manager, layout="separate_tracks")
    assert binding.status == "session_bound" and binding.turn_status == "session_bound"
    await run_turn(processor, observer, 1)
    await binding.finish(10)
    observer.end_conversation_tracing()
    uploads = [r for r in transport.requests if r.get("purpose") == "turn_recording"]
    assert [r["direction"] for r in uploads] == ["output"]
    assert binding.turn_denials == {"policy_denied": 1}


@pytest.mark.asyncio
async def test_turn_never_falls_back_to_conversation_or_explicit_span(manager, transport):
    span, context = explicit_context()
    processor, observer, binding = await turn_binding(manager, context=context)
    await run_turn(processor, observer, 1)
    assert binding.turn_denials == {"correlation_unavailable": 2}
    # Pipeline context, but a turn number Pipecat never traced.
    binding.close()
    observer.end_conversation_tracing()
    processor, observer, binding = await turn_binding(manager)
    await dispatch(processor, "on_user_turn_audio", TurnAudioData(7, tone(300, 200), RATE, 1))
    assert binding.turn_status == "correlation_unavailable"
    observer.end_conversation_tracing()
    span.end()
    assert not [r for r in transport.requests if r.get("purpose") == "turn_recording"]
    assert manager.owned_bytes == 0


@pytest.mark.asyncio
async def test_unsampled_turn_retains_nothing(manager, transport):
    processor, observer, binding = await turn_binding(manager)
    unsampled = trace.SpanContext(int("3" * 32, 16), int("4" * 16, 16), False, trace.TraceFlags(0))
    observer._trace_context_map[1] = unsampled
    await dispatch(processor, "on_user_turn_audio", TurnAudioData(1, tone(300, 200), RATE, 1))
    observer.end_conversation_tracing()
    assert binding.turn_status == "unsampled" and manager.owned_bytes == 0
    assert not [r for r in transport.requests if r.get("purpose") == "turn_recording"]


def test_turns_denied_on_both_sides_register_no_turn_handler(transport):
    # The server allows only mixed recordings: the call records, turns cannot.
    transport.policy = replace(transport.policy, allowed_directions=frozenset(("mixed",)))
    manager = make_manager(transport)
    span, context = explicit_context()
    try:
        processor = AudioBufferProcessor(sample_rate=RATE, buffer_size=16000, enable_turn_audio=True)
        binding = bind_audio_processor(processor, context, manager=manager, turn_audio=CAPS)
        assert binding.status == "session_bound" and binding.turn_status == "policy_denied"
        events = {e for e, _ in binding._handlers}
        assert "on_audio_data" in events and not events & {"on_user_turn_audio", "on_bot_turn_audio"}
        binding.close()
    finally:
        manager.shutdown(1)
        span.end()


def test_turn_caps_are_part_of_binding_identity(manager):
    processor = AudioBufferProcessor(sample_rate=RATE, buffer_size=16000, enable_turn_audio=True)
    span, context = explicit_context()
    binding = bind_audio_processor(processor, context, manager=manager, turn_audio=CAPS)
    assert bind_audio_processor(processor, context, manager=manager, turn_audio=CAPS) is binding
    with pytest.raises(ValueError, match="conflicting"):
        bind_audio_processor(processor, context, manager=manager,
                             turn_audio=TurnAudioCaps(max_turns=1, max_bytes=1000, max_duration_ms=1000))
    binding.close()
    span.end()


def test_disabled_turn_audio_touches_nothing():
    class Poison:
        def __getattribute__(self, name):
            raise AssertionError("disabled adapter touched its arguments")
    manager = DisabledAttachmentManager()
    assert bind_audio_processor(Poison(), Poison(), manager=manager, turn_audio=CAPS).status == "disabled"
    assert create_audio_capture(Poison(), manager=manager, turn_audio=CAPS).status == "disabled"


def test_create_capture_with_turn_audio_enables_pipecat_turns(transport):
    span, context = explicit_context()
    transport.policy = replace(transport.policy, record_audio=True)
    manager = make_manager(transport, record_audio=True)
    try:
        binding = create_audio_capture(context, manager=manager, turn_audio=CAPS)
        assert binding.status == "session_bound" and binding.turn_status == "session_bound"
        assert binding.processor._enable_turn_audio is True
        assert {"on_user_turn_audio", "on_bot_turn_audio"} <= {e for e, _ in binding._handlers}
        binding.close()
        plain = create_audio_capture(context, manager=manager)
        assert plain.processor._enable_turn_audio is False and plain.turn_status == "off"
        plain.close()
    finally:
        manager.shutdown(1)
    span.end()


# ------------------------------------------- per-turn audio: review follow-ups
@pytest.mark.asyncio
async def test_local_capture_policy_hiding_inputs_drops_only_user_turns(manager, transport):
    # A LOCAL CapturePolicy (no env flag) reaches the per-turn preflight.
    processor, observer, binding = await turn_binding(manager, layout="separate_tracks",
                                                      capture=CapturePolicy(record_inputs=False))
    assert binding.status == "session_bound" and binding.turn_status == "session_bound"
    await run_turn(processor, observer, 1)
    await binding.finish(10)
    observer.end_conversation_tracing()
    uploads = [r for r in transport.requests if r.get("purpose") == "turn_recording"]
    assert [(r["direction"], r["turn_id"]) for r in uploads] == [("output", "1")]
    assert binding.turn_denials == {"policy_denied": 1}
    assert [direction for _, direction, _ in binding.turn_handles] == ["output"]


@pytest.mark.asyncio
async def test_created_recorder_turns_stop_when_server_record_audio_is_revoked(transport):
    # create_audio_capture's turns preflight as operation="create_recording":
    # once the server turns record_audio off, the next turn is denied.
    transport.policy = replace(transport.policy, record_audio=True)
    manager = make_manager(transport, record_audio=True)
    try:
        worker = await run_pipeline(AudioBufferProcessor(sample_rate=RATE, buffer_size=16000), None)
        observer = worker.turn_trace_observer
        observer.start_conversation_tracing("conv-created")
        binding = create_audio_capture(AudioSessionContext.from_pipeline(worker), manager=manager,
                                       sample_rate=RATE, turn_audio=CAPS)
        assert binding.status == "session_bound" and binding.turn_status == "session_bound"
        processor = binding.processor
        await run_turn(processor, observer, 1)
        assert binding.turn_denials == {} and len(binding.turn_handles) == 2
        # Let turn 1 finish first: a tightened policy also stops uploads that are still queued.
        for _, _, handle in binding.turn_handles:
            assert (await handle.wait(10)).status != "unfinished"
        manager.update_policy(replace(transport.policy, record_audio=False))
        await run_turn(processor, observer, 2)
        await binding.finish(10)
        observer.end_conversation_tracing()
        uploads = [r for r in transport.requests if r.get("purpose") == "turn_recording"]
        assert sorted((r["turn_id"], r["direction"]) for r in uploads) == [("1", "input"), ("1", "output")]
        assert binding.turn_denials == {"recording_disabled": 2}
        assert binding.turn_status == "recording_disabled" and len(binding.turn_handles) == 2
        binding.close()
    finally:
        manager.shutdown(2)


def test_created_recorder_has_no_pipecat_turn_buffer_when_turns_are_denied(transport):
    # The server records whole calls (mixed) but admits no per-side turn: the
    # recorder is built WITHOUT enable_turn_audio and no turn handler exists.
    transport.policy = replace(transport.policy, record_audio=True, allowed_directions=frozenset(("mixed",)))
    manager = make_manager(transport, record_audio=True)
    span, context = explicit_context()
    try:
        binding = create_audio_capture(context, manager=manager, turn_audio=CAPS)
        assert binding.status == "session_bound" and binding.turn_status == "policy_denied"
        assert binding.processor._enable_turn_audio is False
        events = {e for e, _ in binding._handlers}
        assert "on_audio_data" in events and not events & {"on_user_turn_audio", "on_bot_turn_audio"}
        binding.close()
    finally:
        manager.shutdown(1)
        span.end()


@pytest.mark.asyncio
async def test_turn_upload_binds_on_parent_and_never_projects_refs(manager, transport):
    # Even when the turn span is still live at upload time, the turn span gets
    # no fi.attachment.* attribute; the upload names it in association.parent.
    pairs = pair_uploads(transport)
    processor, observer, binding = await turn_binding(manager)
    await observer._turn_tracker._call_event_handler("on_turn_started", 1)
    for _ in range(3):
        await asyncio.sleep(0)
    await dispatch(processor, "on_user_turn_audio", TurnAudioData(1, tone(300, 200), RATE, 1))
    for _, _, handle in binding.turn_handles:
        assert (await handle.wait(10)).status != "unfinished"
    await observer._turn_tracker._call_event_handler("on_turn_ended", 1, 1.0, False)
    for _ in range(3):
        await asyncio.sleep(0)
    await binding.finish(10)
    observer.end_conversation_tracing()
    turn = [s for s in EXPORTER.get_finished_spans() if s.name == "turn"]
    assert len(turn) == 1
    assert not [k for k in turn[0].attributes if k.startswith("fi.attachment")]
    uploads = turn_pairs(pairs)
    assert len(uploads) == 1
    assert uploads[0][0]["association"]["parent"]["span_id"] == format(turn[0].context.span_id, "016x")
