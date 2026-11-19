"""
Tests for traceai-livekit: enabling the Future AGI attribute mapping.

livekit-agents 1.8 adds a privacy filter to the tracer provider it is handed, through
`add_span_processor`. Older fi-instrumentation-otel releases (1.1.0 and earlier, see its CHANGELOG)
have `fi_instrumentation.register()` mark its own exporter as a default that the next
`add_span_processor` call replaces, so the filter used to shut the exporter down and no trace was
sent. traceAI-livekit still allows those releases, so `enable_*` clears that mark itself. These
tests pin that the exporter stays next to LiveKit's filter, with or without the mark.

Two end-to-end tests send one agent turn through the real mapping and the real HTTP exporter to
a collector on 127.0.0.1, and read back what arrived: LiveKit 1.8's conversation content must reach
it as `input.value`/`output.value`, and `LIVEKIT_TELEMETRY_ALLOW_PII=false` must keep it out.

No real Future AGI keys and nothing leaves the machine. The provider `register()` returns is never
installed as the process-wide OpenTelemetry provider, and LiveKit's own module tracer is put back
on the provider it held before each test.
"""

import asyncio
import gzip
import importlib.metadata
import re
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
)
from fi_instrumentation.otel import HTTPSpanExporter as _FIHTTPSpanExporter  # noqa: E402
from livekit.agents import Agent, AgentSession, llm  # noqa: E402
from livekit.agents.telemetry import pii  # noqa: E402
from livekit.agents.telemetry.traces import _GatedSpanExporter  # noqa: E402
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


def _processors(provider: Any) -> List[Any]:
    return list(provider._active_span_processor._span_processors)


# The last fi-instrumentation-otel release whose register() marks its exporter as a default the next
# add_span_processor() call replaces. Later releases, and this repository's source, have the TH-8727
# fix. Raise this only if a release with the mark is published after 1.1.0.
_LAST_RELEASE_WITH_THE_MARK = (1, 1, 0)


def _installed_core_marks_the_exporter() -> bool:
    """What the installed fi-instrumentation-otel release's register() should do, by its version."""
    installed = importlib.metadata.version("fi-instrumentation-otel")
    release = tuple(int(part) for part in re.findall(r"\d+", installed)[:3])
    return release <= _LAST_RELEASE_WITH_THE_MARK


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


def test_add_span_processor_after_register_alone_matches_the_installed_core(
    make_provider: Any,
) -> None:
    # Expected from the installed release, not read from the provider: a mark reintroduced in
    # register() fails here against this repository's fi_instrumentation.
    expect_the_mark = _installed_core_marks_the_exporter()
    provider = make_provider()
    future_agi_processor = _processors(provider)[0]
    other_processor = SimpleSpanProcessor(InMemorySpanExporter())

    assert bool(getattr(provider, "_default_processor", False)) is expect_the_mark

    provider.add_span_processor(other_processor)

    if expect_the_mark:
        # Without enable_*, the processor replaces the exporter: why enable_* clears the mark.
        assert _processors(provider) == [other_processor]
    else:
        assert _processors(provider) == [future_agi_processor, other_processor]


def test_livekit_cloud_never_receives_our_copies_of_redacted_content(
    monkeypatch: pytest.MonkeyPatch, make_provider: Any
) -> None:
    # Our exporter maps lk.pii.* content into keys with no "pii" segment (input.value, output.value,
    # the metadata JSON, gen_ai.conversation.id, user.id). LiveKit redacts by key name, so those
    # copies must never be written onto the span the other exporters on the provider share.
    # The mapping runs for real; only the network call under it is replaced.
    monkeypatch.delenv("LIVEKIT_TELEMETRY_ALLOW_PII", raising=False)
    reached_the_network: List[Any] = []

    def record_instead_of_send(self: Any, spans: Any) -> SpanExportResult:
        reached_the_network.extend(spans)
        return SpanExportResult.SUCCESS

    monkeypatch.setattr(_FIHTTPSpanExporter, "export", record_instead_of_send)
    provider = make_provider(batch=False)
    assert enable_http_attribute_mapping() is True

    # LiveKit's Cloud exporter comes after ours: it holds the spans of a job that has not decided
    # on redaction yet, and exports the redacted snapshot of them once the job decides.
    cloud = InMemorySpanExporter()
    gate = _GatedSpanExporter(cloud, export_jobs={})
    gate.open_job("job-1")
    provider.add_span_processor(SimpleSpanProcessor(gate))

    span = provider.get_tracer("t").start_span(
        "agent_turn",
        attributes={
            "job_id": "job-1",
            "lk.pii.user_input": "my card is 4111",
            "lk.pii.response.text": "noted",
            "lk.pii.room_name": "room-secret",
            "lk.pii.participant_identity": "caller-secret",
            "lk.pii.instructions": "secret instructions",
        },
    )
    span.end()
    provider.force_flush()
    gate.job_registered("job-1", traces_enabled=True, redacted=True)

    # The mapping ran: our exporter was handed the span with its content under the FI names.
    assert [s.name for s in reached_the_network] == ["agent_turn"]
    ours = reached_the_network[0].attributes
    assert "4111" in ours[SpanAttributes.INPUT_VALUE]
    assert ours[SpanAttributes.GEN_AI_CONVERSATION_ID] == "room-secret"
    assert ours[SpanAttributes.USER_ID] == "caller-secret"
    assert "secret instructions" in ours[SpanAttributes.METADATA]

    # LiveKit Cloud's exporter got exactly that span, redacted, with none of our copies.
    (received,) = cloud.get_finished_spans()
    assert received.name == "agent_turn"
    for key in (
        SpanAttributes.INPUT_VALUE,
        SpanAttributes.OUTPUT_VALUE,
        SpanAttributes.GEN_AI_CONVERSATION_ID,
        SpanAttributes.USER_ID,
    ):
        assert key not in received.attributes
    assert [k for k in received.attributes if "pii" in k.split(".")] == []
    for value in received.attributes.values():
        text = str(value)
        for secret in ("4111", "room-secret", "caller-secret", "secret instructions"):
            assert secret not in text


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


# Recording rows use real LiveKit telemetry, but never start an RPC/collector.
@pytest.mark.parametrize('allow_pii', [True, False])
def test_recording_metadata_respects_livekit_pii_opt_out_and_cloud_redaction(
    monkeypatch: pytest.MonkeyPatch, make_provider: Any, allow_pii: bool
) -> None:
    from types import SimpleNamespace
    import traceai_livekit
    from opentelemetry.sdk.trace import SpanProcessor

    record = getattr(traceai_livekit, 'record_egress', None)
    assert callable(record)
    monkeypatch.setenv('LIVEKIT_TELEMETRY_ALLOW_PII', 'true' if allow_pii else 'false')
    captured = []

    def capture(self: Any, spans: Any) -> SpanExportResult:
        captured.extend(spans)
        return SpanExportResult.SUCCESS

    monkeypatch.setattr(_FIHTTPSpanExporter, 'export', capture)
    provider = make_provider(batch=False)
    assert enable_http_attribute_mapping()
    cloud = InMemorySpanExporter()
    gate = _GatedSpanExporter(cloud, export_jobs={})
    gate.open_job('recording-job')

    class AddJob(SpanProcessor):
        def on_start(self, span, parent_context=None):
            span.set_attribute('job_id', 'recording-job')

        def on_end(self, span):
            pass

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    provider.add_span_processor(AddJob())
    provider.add_span_processor(SimpleSpanProcessor(gate))
    tracer = provider.get_tracer('recording-test')
    root = tracer.start_span('session-envelope')
    try:
        assert record(SimpleNamespace(
            egress_id='egress-synthetic', room_name='room-synthetic', status=0,
            file_results=[SimpleNamespace(location='https://storage.example/call.mp4?sig=SENTINEL')],
        ), session_span=root, tracer=tracer)
    finally:
        root.end()
    provider.force_flush()
    gate.job_registered('recording-job', traces_enabled=True, redacted=True)
    ours = next(s for s in captured if s.name == 'livekit.recording.snapshot')
    assert ours.attributes['livekit.recording.status'] == '0'
    assert ours.context.trace_id == root.get_span_context().trace_id
    if allow_pii:
        assert ours.attributes['livekit.recording.egress_id'] == 'egress-synthetic'
    else:
        assert not any('pii' in k.split('.') for k in ours.attributes)
        for key in ('egress_id', 'room_name', 'file_locations', 'stream_locations'):
            assert 'livekit.recording.' + key not in ours.attributes
        assert SpanAttributes.GEN_AI_CONVERSATION_ID not in ours.attributes
    seen = next(s for s in cloud.get_finished_spans() if s.name == 'livekit.recording.snapshot')
    assert not any('pii' in k.split('.') for k in seen.attributes)
    assert SpanAttributes.GEN_AI_CONVERSATION_ID not in seen.attributes
    assert not any(k.startswith('livekit.recording.') for k in seen.attributes)
    for value in seen.attributes.values():
        assert not any(s in str(value) for s in ('egress-synthetic', 'room-synthetic', 'storage.example', 'SENTINEL'))


def test_native_agent_session_and_recording_share_the_explicit_session_trace(
    monkeypatch: pytest.MonkeyPatch, make_provider: Any
) -> None:
    from types import SimpleNamespace
    from opentelemetry import trace
    import traceai_livekit

    wrap = getattr(traceai_livekit, 'wrap_egress_client', None)
    assert callable(wrap)
    captured = []

    def capture(self: Any, spans: Any) -> SpanExportResult:
        captured.extend(spans)
        return SpanExportResult.SUCCESS

    monkeypatch.setattr(_FIHTTPSpanExporter, 'export', capture)
    provider = make_provider(batch=False)
    assert enable_http_attribute_mapping()
    tracer = provider.get_tracer('recording-test')
    root = tracer.start_span('session-envelope')

    class Egress:
        async def start_room_composite_egress(self, request):
            return SimpleNamespace(egress_id='egress-synthetic', status=0)

        async def stop_egress(self, request):
            return SimpleNamespace(egress_id='egress-synthetic', status=2)

    service = wrap(Egress(), session_span=root, tracer=tracer)

    async def run():
        with trace.use_span(root, end_on_exit=False):
            await service.start_room_composite_egress(object())
            await _run_one_turn()
        root.end()
        with tracer.start_as_current_span('unrelated'):
            await service.stop_egress(object())

    try:
        asyncio.run(run())
    finally:
        if root.is_recording():
            root.end()
    provider.force_flush()
    native = [s for s in captured if s.name == 'agent_session']
    recordings = [s for s in captured if s.name.startswith('livekit.recording.')]
    assert native, 'native session linkage remains unverified without agent_session'
    assert len(recordings) == 2
    assert all(s.context.trace_id == root.get_span_context().trace_id for s in native + recordings)
    assert all(s.parent.span_id == root.get_span_context().span_id for s in recordings)


@pytest.mark.parametrize("allow_pii", [True, False], ids=["pii-allowed", "pii-disabled"])
@pytest.mark.parametrize("withhold_conversation", [True, False], ids=["stronger-policy", "default-policy"])
def test_recording_ambient_context_does_not_bypass_livekit_privacy(
    monkeypatch: pytest.MonkeyPatch, make_provider: Any, allow_pii: bool,
    withhold_conversation: bool,
) -> None:
    from types import SimpleNamespace
    from fi_instrumentation import FITracer, TraceConfig, using_attributes, using_session
    from opentelemetry import context, trace
    from opentelemetry.sdk.trace import SpanProcessor
    from traceai_livekit import record_egress

    class RecordingPolicy(TraceConfig):
        def mask(self, key, value):
            if withhold_conversation and key == SpanAttributes.GEN_AI_CONVERSATION_ID:
                return None
            return super().mask(key, value)

    monkeypatch.setenv("LIVEKIT_TELEMETRY_ALLOW_PII", "true" if allow_pii else "false")
    captured = []

    def capture(self, spans):
        captured.extend(spans)
        return SpanExportResult.SUCCESS

    monkeypatch.setattr(_FIHTTPSpanExporter, "export", capture)
    provider = make_provider(batch=False)
    assert enable_http_attribute_mapping()
    raw, cloud = InMemorySpanExporter(), InMemorySpanExporter()
    gate = _GatedSpanExporter(cloud, export_jobs={})
    gate.open_job("recording-job-synthetic")

    class AddJob(SpanProcessor):
        def on_start(self, span, parent_context=None):
            span.set_attribute("job_id", "recording-job-synthetic")

        def on_end(self, span):
            pass

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    provider.add_span_processor(AddJob())
    provider.add_span_processor(SimpleSpanProcessor(raw))
    provider.add_span_processor(SimpleSpanProcessor(gate))
    tracer = provider.get_tracer("caller-supplied-scope")
    root = tracer.start_span("session-envelope")
    saved = trace.NonRecordingSpan(root.get_span_context())
    root.end()
    existing = FITracer(tracer, config=TraceConfig())
    with tracer.start_as_current_span("unrelated"):
        with using_session("ambient-person@example.invalid"), using_attributes(
            user_id="ambient-user@example.invalid", metadata={"source": "ambient-synthetic"},
        ):
            before = context.get_current()
            assert record_egress(
                SimpleNamespace(egress_id="egress-synthetic", room_name="room-synthetic"),
                session_span=saved, tracer=existing, trace_config=RecordingPolicy(),
            )
            assert context.get_current() is before
    provider.force_flush()
    gate.job_registered("recording-job-synthetic", traces_enabled=True, redacted=True)
    ours = next(s for s in captured if s.name == "livekit.recording.snapshot")
    other = next(s for s in raw.get_finished_spans() if s.name == "livekit.recording.snapshot")
    seen = next(s for s in cloud.get_finished_spans() if s.name == "livekit.recording.snapshot")
    for span in (ours, other, seen):
        assert span.context.trace_id == saved.get_span_context().trace_id
        assert span.parent.span_id == saved.get_span_context().span_id
        assert "ambient-" not in str(dict(span.attributes))
        assert SpanAttributes.SESSION_ID not in span.attributes
        assert SpanAttributes.USER_ID not in span.attributes
    assert SpanAttributes.GEN_AI_CONVERSATION_ID not in other.attributes
    assert not any(k.startswith("livekit.recording.") for k in other.attributes)
    assert SpanAttributes.GEN_AI_CONVERSATION_ID not in seen.attributes
    assert not any("pii" in k.split(".") for k in seen.attributes)
    assert not any(k.startswith("livekit.recording.") for k in seen.attributes)
    if allow_pii:
        assert ours.attributes["livekit.recording.egress_id"] == "egress-synthetic"
    else:
        assert not any("pii" in k.split(".") for k in ours.attributes)
        assert "livekit.recording.egress_id" not in ours.attributes
    if allow_pii and not withhold_conversation:
        assert ours.attributes[SpanAttributes.GEN_AI_CONVERSATION_ID] == "room-synthetic"
    else:
        assert SpanAttributes.GEN_AI_CONVERSATION_ID not in ours.attributes
