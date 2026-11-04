"""Shared fixtures.

Two kinds of tests:

* recorder tests replay recorded event sequences (``fixtures/*.jsonl``) into a
  ``SessionRecorder`` with an in-memory exporter. No network, no SDK socket.
* connection tests run the real ``openai`` SDK (sync and async clients) against
  the local stand-in server in ``examples/realtime_stub_server.py`` on 127.0.0.1.

The fixtures were recorded from stub sessions driven by ``examples/voice_agent.py``,
with audio cut to 64 base64 characters per frame and marked with the prefix
``UklGRg`` so a test can prove audio never lands on a span.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import threading
import time
from typing import Any, Dict, Iterator, List, Optional

import pytest
from fi_instrumentation import FITracer, TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "examples"))

import realtime_stub_server as stub  # noqa: E402

from traceai_openai_realtime import OpenAIRealtimeInstrumentor  # noqa: E402
from traceai_openai_realtime._session import SessionRecorder, Settings  # noqa: E402

from websockets.exceptions import ConnectionClosedError  # noqa: E402

try:  # openai 3.x wraps a drop with unsent messages in its own error
    from openai._exceptions import WebSocketConnectionClosedError  # noqa: E402

    DROP_ERRORS: tuple = (ConnectionClosedError, WebSocketConnectionClosedError)
except ImportError:  # pragma: no cover - openai 2.x
    DROP_ERRORS = (ConnectionClosedError,)

FIXTURES = HERE / "fixtures"
MARKER = "FI-MARK-fixture01"
API_KEY = "sk-fixture-0000000000"
AUDIO_PREFIX = "UklGRg"


def load(name: str) -> List[Dict[str, Any]]:
    with open(FIXTURES / f"{name}.jsonl") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def replay(recorder: SessionRecorder, rows: List[Dict[str, Any]], step_ns: int = 1_000_000) -> None:
    start = time.time_ns()
    for i, row in enumerate(rows):
        now = start + i * step_ns
        if row["dir"] == "server":
            recorder.on_server_event(row["event"], now)
        else:
            recorder.on_client_event(row["event"], now)


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def provider(exporter: InMemorySpanExporter) -> TracerProvider:
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    return tp


@pytest.fixture
def make_recorder(provider: TracerProvider):
    def _make(model: Optional[str] = "gpt-realtime-2.1", **settings: Any) -> SessionRecorder:
        tracer = FITracer(provider.get_tracer("test"), config=TraceConfig())
        recorder = SessionRecorder(tracer, Settings(**settings), model=model, secrets=(API_KEY,))
        recorder.start()
        return recorder

    return _make


@pytest.fixture
def instrument(provider: TracerProvider) -> Iterator[Any]:
    """instrument(**kwargs) with this test's provider; uninstrumented afterwards."""
    instrumentor = OpenAIRealtimeInstrumentor()

    def _instrument(**kwargs: Any) -> OpenAIRealtimeInstrumentor:
        instrumentor.instrument(tracer_provider=provider, **kwargs)
        return instrumentor

    yield _instrument
    instrumentor.uninstrument()


class StubServer:
    """The stand-in Realtime server on its own event loop thread."""

    def __init__(self, config: stub.Config) -> None:
        self.config = config
        self.loop = asyncio.new_event_loop()
        self.port = 0
        self.connections = 0
        self._ready = threading.Event()
        self._stop: Optional[asyncio.Future] = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        self._stop = self.loop.create_future()
        async with stub.make_server(self.config, "127.0.0.1", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/v1"

    def __enter__(self) -> "StubServer":
        self._thread.start()
        assert self._ready.wait(10), "stub server did not start"
        return self

    def __exit__(self, *exc: Any) -> None:
        self.loop.call_soon_threadsafe(self._stop.set_result, None)
        self._thread.join(10)


@pytest.fixture
def stub_server() -> Iterator[Any]:
    servers: List[StubServer] = []

    def _start(**config: Any) -> StubServer:
        server = StubServer(stub.Config(pace=config.pop("pace", 0.002), **config))
        servers.append(server.__enter__())
        return server

    yield _start
    for server in servers:
        server.__exit__()


def by_name(spans: Any, name: str) -> list:
    return [s for s in spans if s.name == name]


def root_of(spans: Any) -> Any:
    roots = [s for s in spans if s.parent is None]
    assert len(roots) == 1, [s.name for s in roots]
    return roots[0]
