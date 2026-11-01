"""What this package must not touch: chat calls, beta Realtime, GPT-Live, LiveKit, Pipecat."""

from __future__ import annotations

import ast
import importlib
import inspect
import pathlib
import logging
import sys
import types

import pytest
import wrapt
from openai import AsyncOpenAI, OpenAI
from openai.resources.realtime import realtime as rt

try:  # openai 3.x moved to httpx2; 2.x uses httpx
    import httpx2 as httpx
except ImportError:  # pragma: no cover - depends on the installed openai
    import httpx

import traceai_openai_realtime
from traceai_openai_realtime import PATCHED
from traceai_openai_realtime._connect import AsyncManagerProxy, SyncManagerProxy


def _is_wrapper(obj) -> bool:
    return isinstance(obj, (wrapt.FunctionWrapper, wrapt.BoundFunctionWrapper))


def test_only_realtime_connect_is_patched(instrument):
    """AC-01: the patch list is the two connect methods; OpenAI.request is untouched."""
    import openai

    assert PATCHED == ("Realtime.connect", "AsyncRealtime.connect")
    request_before = (openai.OpenAI.request, openai.AsyncOpenAI.request)
    instrument()
    assert _is_wrapper(inspect.getattr_static(rt.Realtime, "connect"))
    assert _is_wrapper(inspect.getattr_static(rt.AsyncRealtime, "connect"))
    assert (openai.OpenAI.request, openai.AsyncOpenAI.request) == request_before
    assert not _is_wrapper(inspect.getattr_static(openai.OpenAI, "request"))


def test_chat_completion_produces_no_realtime_span(instrument, exporter):
    """AC-01: a chat completion in the same process is not traced by this package."""
    instrument()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "gpt-5",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    client = OpenAI(api_key="sk-test-000000", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    out = client.chat.completions.create(model="gpt-5", messages=[{"role": "user", "content": "hi"}])
    assert out.choices[0].message.content == "hi"
    assert exporter.get_finished_spans() == ()


def test_chat_instrumentor_keeps_its_span_when_both_are_installed(instrument, exporter, provider):
    """AC-01: with traceAI-openai also instrumented, the chat call still gets its own span."""
    traceai_openai = pytest.importorskip("traceai_openai")
    instrument()
    chat = traceai_openai.OpenAIInstrumentor()
    chat.instrument(tracer_provider=provider)
    try:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "gpt-5",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })

        client = OpenAI(api_key="sk-test-000000", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        client.chat.completions.create(model="gpt-5", messages=[{"role": "user", "content": "hi"}])
        names = [s.name for s in exporter.get_finished_spans()]
        assert names and not [n for n in names if n.startswith("openai.realtime")]
    finally:
        chat.uninstrument()


def test_beta_realtime_and_gpt_live_are_not_wrapped(instrument):
    instrument()
    beta = importlib.import_module("openai.resources.beta.realtime.realtime")
    assert not _is_wrapper(inspect.getattr_static(beta.Realtime, "connect"))
    assert not _is_wrapper(inspect.getattr_static(beta.AsyncRealtime, "connect"))
    try:
        live = importlib.import_module("openai.resources.live.live")
    except ImportError:
        return  # openai 2.x has no client.live
    assert not _is_wrapper(inspect.getattr_static(live.Live, "connect"))
    assert not _is_wrapper(inspect.getattr_static(live.AsyncLive, "connect"))


def test_connect_aimed_at_gpt_live_is_not_instrumented(instrument, exporter, caplog):
    """AC-17: a websocket base URL on /live/... produces no span and one warning."""
    instrument()
    caplog.set_level(logging.WARNING, logger="traceai_openai_realtime")
    client = AsyncOpenAI(api_key="sk-test-000000", websocket_base_url="wss://api.openai.com/v1/live/sessions")
    manager = client.realtime.connect(model="gpt-realtime-2.1")
    assert type(manager) is rt.AsyncRealtimeConnectionManager
    assert not isinstance(manager, wrapt.ObjectProxy)
    warnings = [r for r in caplog.records if "GPT-Live" in r.getMessage()]
    assert len(warnings) == 1
    assert exporter.get_finished_spans() == ()

    normal = AsyncOpenAI(api_key="sk-test-000000").realtime.connect(model="gpt-realtime-2.1")
    assert isinstance(normal, AsyncManagerProxy)
    assert isinstance(normal, rt.AsyncRealtimeConnectionManager)  # the proxy keeps isinstance
    assert isinstance(OpenAI(api_key="sk-test-000000").realtime.connect(model="gpt-realtime-2.1"), SyncManagerProxy)


def test_livekit_and_pipecat_classes_are_not_hooked(instrument, monkeypatch):
    """AC-16: Realtime classes in LiveKit and Pipecat keep their own methods."""

    class RealtimeModel:
        def session(self):
            return "livekit"

    class OpenAIRealtimeLLMService:
        async def _connect(self):
            return "pipecat"

    fakes = {
        "livekit": types.ModuleType("livekit"),
        "livekit.plugins": types.ModuleType("livekit.plugins"),
        "livekit.plugins.openai": types.ModuleType("livekit.plugins.openai"),
        "livekit.plugins.openai.realtime": types.ModuleType("livekit.plugins.openai.realtime"),
        "pipecat": types.ModuleType("pipecat"),
        "pipecat.services": types.ModuleType("pipecat.services"),
        "pipecat.services.openai": types.ModuleType("pipecat.services.openai"),
        "pipecat.services.openai.realtime": types.ModuleType("pipecat.services.openai.realtime"),
    }
    fakes["livekit.plugins.openai.realtime"].RealtimeModel = RealtimeModel
    fakes["pipecat.services.openai.realtime"].OpenAIRealtimeLLMService = OpenAIRealtimeLLMService
    for name, module in fakes.items():
        monkeypatch.setitem(sys.modules, name, module)
    before = (RealtimeModel.__dict__["session"], OpenAIRealtimeLLMService.__dict__["_connect"])
    instrument()
    after = (RealtimeModel.__dict__["session"], OpenAIRealtimeLLMService.__dict__["_connect"])
    assert before == after
    package_dir = pathlib.Path(traceai_openai_realtime.__file__).parent
    imported = set()
    for path in package_dir.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
    assert not [m for m in imported if m.split(".")[0] in ("livekit", "pipecat")]


def test_uninstrument_restores_connect(instrument):
    original = (rt.Realtime.connect, rt.AsyncRealtime.connect)
    instrumentor = instrument()
    assert _is_wrapper(inspect.getattr_static(rt.Realtime, "connect"))
    instrumentor.uninstrument()
    assert (rt.Realtime.connect, rt.AsyncRealtime.connect) == original
    instrumentor.instrument()  # the fixture uninstruments at teardown
