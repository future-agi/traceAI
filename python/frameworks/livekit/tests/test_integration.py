"""
Tests for traceai-livekit: enabling the Future AGI attribute mapping.

livekit-agents 1.8 adds a privacy filter to the tracer provider it is handed, through
`add_span_processor`. `fi_instrumentation.register()` marks its own exporter as a default that the
next `add_span_processor` call replaces, so the filter used to shut the exporter down and no trace
was sent. These tests pin that the exporter stays next to LiveKit's filter.

Two end-to-end tests send one agent turn through the real mapping and the real HTTP exporter to
a collector on 127.0.0.1, and read back what arrived: LiveKit 1.8's conversation content must reach
it as `input.value`/`output.value`, and `LIVEKIT_TELEMETRY_ALLOW_PII=false` must keep it out.

No real Future AGI keys and nothing leaves the machine. The provider `register()` returns is never
installed as the process-wide OpenTelemetry provider, and LiveKit's own module tracer is put back
on the provider it held before each test.
"""

import asyncio
import gzip
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator, List

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

pytest.importorskip("livekit.agents", minversion="1.8")

from fi_instrumentation.fi_types import SpanAttributes  # noqa: E402
from fi_instrumentation.otel import (  # noqa: E402
    BatchSpanProcessor,
    Transport,
    register,
)
from livekit.agents import Agent, AgentSession, llm  # noqa: E402
from livekit.agents.telemetry import pii, traces  # noqa: E402
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS  # noqa: E402
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (  # noqa: E402
    ExportTraceServiceRequest,
)

from traceai_livekit import (  # noqa: E402
    MappedGRPCSpanExporter,
    MappedHTTPSpanExporter,
    enable_grpc_attribute_mapping,
    enable_http_attribute_mapping,
)


@pytest.fixture()
def exported_spans(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """Spans handed to the Future AGI exporters, captured instead of sent."""
    captured: List[Any] = []

    def fake_export(self: Any, spans: Any) -> SpanExportResult:
        captured.extend(spans)
        return SpanExportResult.SUCCESS

    monkeypatch.setattr(MappedHTTPSpanExporter, "export", fake_export)
    monkeypatch.setattr(MappedGRPCSpanExporter, "export", fake_export)
    return captured


@pytest.fixture()
def make_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Build a provider with `register()` and make the integration pick it up.

    The provider is returned to the integration through a patched
    `trace_api.get_tracer_provider`, so the process-wide provider is never set. `register()`
    replaces the SIGINT/SIGTERM handlers; they are put back, and every provider is shut down.
    `enable_*` points LiveKit's module tracer at the provider; it is put back on the provider it
    held before, so no later test finds it on a shut-down one.
    """
    monkeypatch.setenv("FI_API_KEY", "test-api-key")
    monkeypatch.setenv("FI_SECRET_KEY", "test-secret-key")
    saved_handlers = {
        sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    saved_tracer_provider = traces.tracer._tracer_provider
    providers: List[Any] = []

    def _make(transport: Transport = Transport.HTTP) -> Any:
        provider = register(
            project_name="livekit-test",
            set_global_tracer_provider=False,
            verbose=False,
            transport=transport,
        )
        providers.append(provider)
        monkeypatch.setattr(
            "traceai_livekit.integration.trace_api.get_tracer_provider",
            lambda: provider,
        )
        return provider

    yield _make

    traces.tracer.set_provider(saved_tracer_provider)
    for provider in providers:
        provider.shutdown()
    for sig, handler in saved_handlers.items():
        signal.signal(sig, handler)


def _processors(provider: Any) -> List[Any]:
    return list(provider._active_span_processor._span_processors)


def _future_agi_processors(provider: Any, exporter_type: type) -> List[Any]:
    return [
        p
        for p in _processors(provider)
        if isinstance(getattr(p, "span_exporter", None), exporter_type)
    ]


def test_enabling_http_mapping_keeps_the_exporter_alongside_livekits_privacy_filter(
    make_provider: Any,
) -> None:
    provider = make_provider()

    assert enable_http_attribute_mapping() is True

    processors = _processors(provider)
    assert len(processors) == 2
    assert isinstance(processors[0], pii._PIIFilteringSpanProcessor)
    assert isinstance(processors[1], BatchSpanProcessor)
    assert isinstance(processors[1].span_exporter, MappedHTTPSpanExporter)


def test_a_span_ended_after_enabling_reaches_the_future_agi_exporter(
    make_provider: Any, exported_spans: List[Any]
) -> None:
    provider = make_provider()
    enable_http_attribute_mapping()

    span = provider.get_tracer("t").start_span("livekit-turn")
    span.end()
    provider.force_flush()

    assert [s.name for s in exported_spans] == ["livekit-turn"]


def test_enabling_grpc_mapping_keeps_the_exporter(
    make_provider: Any, exported_spans: List[Any]
) -> None:
    pytest.importorskip("grpc")
    provider = make_provider(Transport.GRPC)

    assert enable_grpc_attribute_mapping() is True

    processors = _processors(provider)
    assert any(isinstance(p, pii._PIIFilteringSpanProcessor) for p in processors)
    assert len(_future_agi_processors(provider, MappedGRPCSpanExporter)) == 1


def test_the_exporter_is_kept_even_when_no_exporter_was_swapped(
    make_provider: Any, exported_spans: List[Any]
) -> None:
    # An HTTP provider with the gRPC mapping: nothing is swapped, and the exporter still stays.
    provider = make_provider(Transport.HTTP)

    assert enable_grpc_attribute_mapping() is False

    processors = _processors(provider)
    assert isinstance(processors[0], pii._PIIFilteringSpanProcessor)
    assert any(isinstance(p, BatchSpanProcessor) for p in processors)


def test_a_processor_added_after_enabling_runs_alongside_the_exporter(
    make_provider: Any, exported_spans: List[Any]
) -> None:
    provider = make_provider()
    enable_http_attribute_mapping()
    other = InMemorySpanExporter()
    other_processor = SimpleSpanProcessor(other)

    provider.add_span_processor(other_processor)

    processors = _processors(provider)
    assert other_processor in processors
    assert len(_future_agi_processors(provider, MappedHTTPSpanExporter)) == 1

    provider.get_tracer("t").start_span("after-add").end()
    provider.force_flush()

    assert [s.name for s in exported_spans] == ["after-add"]
    assert [s.name for s in other.get_finished_spans()] == ["after-add"]


def test_register_alone_still_lets_add_span_processor_replace_its_default(
    make_provider: Any,
) -> None:
    provider = make_provider()
    other_processor = SimpleSpanProcessor(InMemorySpanExporter())

    provider.add_span_processor(other_processor)

    assert _processors(provider) == [other_processor]


# --- One agent turn, end to end -------------------------------------------------------------
#
# No patched `export()`: the spans go through `_map_attributes_to_fi_conventions` and the Future
# AGI HTTP exporter to a collector on 127.0.0.1 that records what it is sent.

USER_TEXT = "Book a table for two at 7pm"
REPLY_TEXT = "Your table for two is booked at 7pm."
MODEL_NAME = "probe-model-1"


class _FakeStream(llm.LLMStream):
    async def _run(self) -> None:
        self._event_ch.send_nowait(
            llm.ChatChunk(
                id="c1",
                delta=llm.ChoiceDelta(role="assistant", content=REPLY_TEXT),
            )
        )
        self._event_ch.send_nowait(
            llm.ChatChunk(
                id="c1",
                usage=llm.CompletionUsage(
                    completion_tokens=9, prompt_tokens=21, total_tokens=30
                ),
            )
        )


class _FakeLLM(llm.LLM):
    @property
    def model(self) -> str:
        return MODEL_NAME

    @property
    def provider(self) -> str:
        return "probe"

    def chat(  # type: ignore[override]
        self,
        *,
        chat_ctx: Any,
        tools: Any = None,
        conn_options: Any = DEFAULT_API_CONNECT_OPTIONS,
        **kwargs: Any,
    ) -> _FakeStream:
        return _FakeStream(
            self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options
        )


def _any_value(value: Any) -> Any:
    kind = value.WhichOneof("value")
    return getattr(value, kind) if kind else None


class _Collector:
    """A local OTLP/HTTP endpoint that records every POST body it receives."""

    def __init__(self) -> None:
        self.bodies: List[bytes] = []
        bodies = self.bodies

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                bodies.append(body)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def spans(self) -> Dict[str, Dict[str, Any]]:
        """Attributes of every span received, by span name."""
        by_name: Dict[str, Dict[str, Any]] = {}
        for body in self.bodies:
            request = ExportTraceServiceRequest()
            request.ParseFromString(body)
            for resource_spans in request.resource_spans:
                for scope_spans in resource_spans.scope_spans:
                    for span in scope_spans.spans:
                        by_name[span.name] = {
                            a.key: _any_value(a.value) for a in span.attributes
                        }
        return by_name


@pytest.fixture()
def collector(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Collector]:
    """A running collector, with `FI_BASE_URL` pointing at it.

    Ask for it before `make_provider`: `register()` reads `FI_BASE_URL` when it builds the exporter.
    """
    server = _Collector()
    server.start()
    try:
        monkeypatch.setenv("FI_BASE_URL", server.url)
        yield server
    finally:
        server.stop()


async def _run_one_turn() -> None:
    async with AgentSession(llm=_FakeLLM()) as session:
        await session.start(Agent(instructions="You book restaurant tables."))
        await session.run(user_input=USER_TEXT)


def test_one_agent_turn_reaches_the_collector_with_its_content(
    monkeypatch: pytest.MonkeyPatch, collector: _Collector, make_provider: Any
) -> None:
    monkeypatch.delenv("LIVEKIT_TELEMETRY_ALLOW_PII", raising=False)
    provider = make_provider()
    assert enable_http_attribute_mapping() is True

    asyncio.run(_run_one_turn())
    provider.force_flush()

    spans = collector.spans()
    llm_node = spans["llm_node"]
    # input.value / output.value are JSON-encoded, so the texts are matched inside them.
    assert USER_TEXT in llm_node[SpanAttributes.INPUT_VALUE]
    assert REPLY_TEXT in llm_node[SpanAttributes.OUTPUT_VALUE]
    assert llm_node[SpanAttributes.GEN_AI_REQUEST_MODEL] == MODEL_NAME
    assert llm_node[SpanAttributes.GEN_AI_SPAN_KIND] == "LLM"
    agent_turn = spans["agent_turn"]
    assert USER_TEXT in agent_turn[SpanAttributes.INPUT_VALUE]
    assert REPLY_TEXT in agent_turn[SpanAttributes.OUTPUT_VALUE]


def test_the_allow_pii_opt_out_keeps_content_out_of_the_export(
    monkeypatch: pytest.MonkeyPatch, collector: _Collector, make_provider: Any
) -> None:
    # LiveKit reads the variable when it installs its privacy filter, inside `enable_*`.
    monkeypatch.setenv("LIVEKIT_TELEMETRY_ALLOW_PII", "false")
    provider = make_provider()
    assert enable_http_attribute_mapping() is True

    asyncio.run(_run_one_turn())
    provider.force_flush()

    spans = collector.spans()
    assert "llm_node" in spans  # the span is still exported ...
    for name in ("llm_node", "agent_turn"):  # ... without the conversation
        attributes = spans[name]
        assert SpanAttributes.INPUT_VALUE not in attributes
        assert SpanAttributes.OUTPUT_VALUE not in attributes
        assert [k for k in attributes if k.startswith("lk.pii.")] == []
