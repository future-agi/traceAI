"""A scripted BidiGenerateContent socket under the real google-genai SDK.

``google.genai.live.ws_connect`` is replaced by a fake that replays a fixture of server
frames (wire JSON, camelCase), so the real ``AsyncLive.connect``, converters and
``AsyncSession`` methods run unchanged. No network, no Gemini call.
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any, Dict, List

import pytest
from google import genai
from google.genai import live as live_mod
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from traceai_google_genai_live import GoogleGenAILiveInstrumentor

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
API_KEY = "AIzaFixtureKey-0000-not-real-7f2e"
AUDIO_MARKER = "FI-AUDIO-BYTES-MARKER-9c41"
HANDLE_1 = "fixture-resumption-handle-1-d93e"
HANDLE_2 = "fixture-resumption-handle-2-a17b"


def load(name: str) -> List[Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeSocket:
    def __init__(self, frames: List[Any]) -> None:
        self.frames = list(frames)
        self.sent: List[Dict[str, Any]] = []
        self.closed = False

    async def send(self, message: str) -> None:
        if self.closed:
            raise ConnectionClosedError(Close(1000, ""), None)
        self.sent.append(json.loads(message))

    async def recv(self, decode: Any = None) -> bytes:
        if not self.frames:
            raise AssertionError("fixture exhausted: the test read past the script")
        frame = self.frames.pop(0)
        if isinstance(frame, list) and frame and frame[0] == "close":
            self.closed = True
            raise ConnectionClosedError(Close(frame[1], frame[2]) if frame[1] != 1006 else None, None)
        return json.dumps(frame).encode()

    async def close(self) -> None:
        self.closed = True


class FakeServer:
    """Each ``ws_connect`` call takes the next script from ``scripts``."""

    def __init__(self) -> None:
        self.scripts: List[List[Any]] = []
        self.sockets: List[FakeSocket] = []
        self.connects = 0
        self.uris: List[str] = []

    def __call__(self, uri: str, **kwargs: Any):
        server = self

        class _CM:
            async def __aenter__(self_inner):
                server.connects += 1
                server.uris.append(uri)
                sock = FakeSocket(server.scripts.pop(0))
                server.sockets.append(sock)
                return sock

            async def __aexit__(self_inner, *exc):
                return False

        return _CM()


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    srv = FakeServer()
    monkeypatch.setattr(live_mod, "ws_connect", srv)
    return srv


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def provider(exporter: InMemorySpanExporter) -> TracerProvider:
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    return tp


@pytest.fixture
def instrument(provider: TracerProvider, monkeypatch: pytest.MonkeyPatch):
    for name in ("FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_INPUT_MESSAGES",
                 "FI_HIDE_OUTPUT_MESSAGES", "FI_HIDE_INPUT_TEXT", "FI_HIDE_OUTPUT_TEXT"):
        monkeypatch.delenv(name, raising=False)
    created = []

    def _instrument(**kwargs: Any) -> GoogleGenAILiveInstrumentor:
        inst = GoogleGenAILiveInstrumentor()
        inst.instrument(tracer_provider=provider, **kwargs)
        created.append(inst)
        return inst

    yield _instrument
    for inst in created:
        inst.uninstrument()


@pytest.fixture
def client() -> genai.Client:
    return genai.Client(api_key=API_KEY)


AUDIO_CONFIG = {"response_modalities": ["AUDIO"], "output_audio_transcription": {},
                "input_audio_transcription": {}}


async def run_turn(session: Any, *, tool_result: Any = None) -> List[Any]:
    """One ``receive()`` pass. Answers each tool call with ``tool_result`` when given."""
    from google.genai import types

    seen = []
    async for msg in session.receive():
        seen.append(msg)
        if msg.tool_call and tool_result is not None:
            await session.send_tool_response(function_responses=[
                types.FunctionResponse(id=fc.id, name=fc.name, response=tool_result)
                for fc in msg.tool_call.function_calls])
    return seen


def spans_by_name(exporter: InMemorySpanExporter) -> Dict[str, List[Any]]:
    out: Dict[str, List[Any]] = {}
    for span in exporter.get_finished_spans():
        out.setdefault(span.name, []).append(span)
    return out


def all_text(exporter: InMemorySpanExporter) -> str:
    """Every attribute value, event attribute, status text and resource attribute exported."""
    chunks = []
    for span in exporter.get_finished_spans():
        chunks.append(span.name)
        chunks.append(json.dumps(dict(span.attributes or {}), default=str))
        chunks.append(json.dumps(dict(span.resource.attributes or {}), default=str))
        chunks.append(str(span.status.description))
        for ev in span.events:
            chunks.append(ev.name)
            chunks.append(json.dumps(dict(ev.attributes or {}), default=str))
    return "\n".join(chunks)


@pytest.fixture
def log_text(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)

    def _text() -> str:
        return "\n".join(r.getMessage() + " " + str(r.args) for r in caplog.records)

    return _text
