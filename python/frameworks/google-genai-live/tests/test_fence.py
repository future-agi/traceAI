"""What this package patches and what it leaves alone (AC-01, AC-02, AC-22, AC-24, V-B, D-V11, AC-17)."""

from __future__ import annotations

import inspect
import logging
import pathlib
import sys
import time

import pytest
import wrapt
from google import genai
from google.genai import live, models
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

from traceai_google_genai_live import PATCHED, GoogleGenAILiveInstrumentor

from .conftest import AUDIO_CONFIG

MODEL = "gemini-3.8-live"


def test_vb_sdk_surface_exists():
    """V-B: the hook points exist in the installed google-genai."""
    assert inspect.isfunction(live.AsyncLive.connect) or callable(live.AsyncLive.connect)
    for name in ("send_client_content", "send_realtime_input", "send_tool_response", "receive"):
        assert hasattr(live.AsyncSession, name), name
    params = inspect.signature(live.AsyncLive.connect).parameters
    assert "model" in params and "config" in params


def test_ac01_ac02_patch_list(instrument):
    instrument()
    assert set(PATCHED) == {("AsyncLive", "connect"), ("AsyncSession", "send_client_content"),
                            ("AsyncSession", "send_realtime_input"), ("AsyncSession", "send_tool_response"),
                            ("AsyncSession", "receive"), ("AsyncSession", "send_context_update")}
    for cls, name in PATCHED:
        if name == "send_context_update" and not hasattr(live.AsyncSession, name):
            continue
        assert isinstance(getattr(live, cls).__dict__[name], wrapt.FunctionWrapper)
    # Deprecated upstream: not wrapped.
    assert not isinstance(live.AsyncSession.__dict__["send"], wrapt.FunctionWrapper)
    assert not isinstance(live.AsyncSession.__dict__["start_stream"], wrapt.FunctionWrapper)
    # The chat path belongs to traceAI-google-genai.
    for name in ("generate_content", "generate_content_stream"):
        assert not isinstance(models.Models.__dict__[name], wrapt.FunctionWrapper)
        assert not isinstance(models.AsyncModels.__dict__[name], wrapt.FunctionWrapper)


def test_uninstrument_restores_originals(instrument):
    originals = {(c, n): getattr(live, c).__dict__[n] for c, n in PATCHED if hasattr(getattr(live, c), n)}
    inst = instrument()
    inst.uninstrument()
    for (c, n), fn in originals.items():
        assert getattr(live, c).__dict__[n] is fn


def test_ac22_livekit_and_pipecat_untouched(instrument):
    before = {m for m in sys.modules if m.split(".")[0] in ("livekit", "pipecat", "traceai_livekit", "traceai_pipecat")}
    instrument()
    after = {m for m in sys.modules if m.split(".")[0] in ("livekit", "pipecat", "traceai_livekit", "traceai_pipecat")}
    assert after == before
    assert all(cls in ("AsyncLive", "AsyncSession") for cls, _ in PATCHED)


def test_ac24_chat_package_floor_untouched():
    pyproject = pathlib.Path(__file__).resolve().parents[2] / "google-genai" / "pyproject.toml"
    if not pyproject.exists():
        pytest.skip("not run from the traceAI monorepo")
    assert 'google-genai = ">=1.8.0"' in pyproject.read_text()


async def test_vertex_client_warns_once_and_is_not_traced(server, instrument, exporter, caplog):
    from traceai_google_genai_live import _connect

    _connect._warned.discard("vertex")
    instrument()
    client = genai.Client(api_key="AIzaVertexExpress", vertexai=True)
    server.scripts.append([{"setupComplete": {}}])
    caplog.set_level(logging.WARNING)
    async with client.aio.live.connect(model="gemini-live", config=AUDIO_CONFIG):
        pass
    assert exporter.get_finished_spans() == ()
    assert sum("Vertex AI Live" in r.getMessage() for r in caplog.records) == 1


async def test_ephemeral_token_is_not_traced(server, instrument, exporter, caplog):
    from traceai_google_genai_live import _connect

    _connect._warned.discard("ephemeral")
    instrument()
    client = genai.Client(api_key="auth_tokens/fixture", http_options={"api_version": "v1alpha"})
    server.scripts.append([{"setupComplete": {}}])
    caplog.set_level(logging.WARNING)
    async with client.aio.live.connect(model=MODEL, config=AUDIO_CONFIG):
        pass
    assert exporter.get_finished_spans() == ()
    assert any("ephemeral-token" in r.getMessage() for r in caplog.records)


class _SlowExporter(SpanExporter):
    def __init__(self):
        self.exported = 0

    def export(self, spans):
        time.sleep(0.5)
        self.exported += len(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


async def test_ac17_full_queue_drops_and_never_blocks_the_reader(server, client):
    slow = _SlowExporter()
    tp = TracerProvider()
    tp.add_span_processor(BatchSpanProcessor(slow, max_queue_size=1, max_export_batch_size=1,
                                             schedule_delay_millis=1))
    inst = GoogleGenAILiveInstrumentor()
    inst.instrument(tracer_provider=tp)
    try:
        frames = [{"setupComplete": {}}]
        for i in range(30):
            frames.append({"toolCall": {"functionCalls": [{"name": "f", "args": {}, "id": f"c{i}"}]}})
        frames.append({"serverContent": {"turnComplete": True}})
        server.scripts.append(frames)
        worst = 0.0
        async with client.aio.live.connect(model=MODEL, config=AUDIO_CONFIG) as session:
            it = session.receive()
            while True:
                t0 = time.perf_counter()
                try:
                    msg = await it.__anext__()
                except StopAsyncIteration:
                    break
                worst = max(worst, time.perf_counter() - t0)
                if msg.tool_call:
                    from google.genai import types

                    await session.send_tool_response(function_responses=[
                        types.FunctionResponse(id=fc.id, name=fc.name, response={})
                        for fc in msg.tool_call.function_calls])
        # One export takes 0.5 s, so a reader that waited on the exporter would stall that long.
        # Half of it leaves room for a loaded CI machine without hiding a real block.
        assert worst < 0.25, f"receive blocked for {worst:.3f}s"
    finally:
        inst.uninstrument()
        tp.shutdown()
    assert slow.exported < 33  # some spans were dropped instead of blocking


def test_ac01_generate_content_keeps_its_own_span_and_gets_no_live_span(instrument, provider, exporter, monkeypatch):
    """AC-01: with both packages instrumented, generate_content is traced by traceAI-google-genai only."""
    chat = pytest.importorskip("traceai_google_genai")
    import json as _json

    from google.genai import _api_client

    payload = {"candidates": [{"content": {"role": "model", "parts": [{"text": "pong"}]}, "finishReason": "STOP"}],
               "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1, "totalTokenCount": 4}}
    monkeypatch.setattr(_api_client.BaseApiClient, "_request",
                        lambda self, *a, **k: _api_client.HttpResponse(headers={}, response_stream=[_json.dumps(payload)]))
    instrument()
    chat_inst = chat.GoogleGenAIInstrumentor()
    chat_inst.instrument(tracer_provider=provider)
    try:
        client = genai.Client(api_key="AIzaFixtureKey-0000-not-real-7f2e")
        out = client.models.generate_content(model="gemini-3.7-flash", contents="ping")
        assert out.text == "pong"
    finally:
        chat_inst.uninstrument()
    names = [s.name for s in exporter.get_finished_spans()]
    assert names, "traceAI-google-genai produced no span"
    assert not any(n.startswith("gemini.live.") for n in names), names
