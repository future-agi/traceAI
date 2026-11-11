"""V01: real Pipecat AudioBufferProcessor -> core RecordingWriter (segments + manifest).

The pipeline tests run a real PipelineWorker with Pipecat's own tracing, so the
recording binds to the conversation span Pipecat itself started. Audio is a
synthetic tone; the transport is the core's synthetic F01 double. This is not a
real SFU call or a product-playback claim (Q02).
"""

import asyncio
import io
import math
import struct
import wave
from dataclasses import replace

import pytest
from opentelemetry import trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, attach, detach, set_value
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from conftest import TransportDouble, make_manager
from fi_instrumentation.attachments import AttachmentConfig, AttachmentManager, CorrelationHandle
from fi_instrumentation.attachments._disabled import DisabledAttachmentManager
from pipecat.frames.frames import EndFrame, InputAudioRawFrame, OutputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.workers.runner import WorkerRunner
from traceai_pipecat import bind_audio_processor, create_audio_capture
from traceai_pipecat.attachments import AudioSessionContext, mark_conversation_root

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
