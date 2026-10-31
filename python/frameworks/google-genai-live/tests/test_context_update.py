"""In-session updates through the real SDK and a scripted websocket, without Gemini."""

from __future__ import annotations

import asyncio
import json

import pytest
from google.genai import live, types
from fi_instrumentation import TraceConfig
from opentelemetry.trace import StatusCode

from .conftest import AUDIO_CONFIG, all_text, run_turn, spans_by_name

HAS_CONTEXT_UPDATE = hasattr(live.AsyncSession, "send_context_update")
needs_context_update = pytest.mark.skipif(not HAS_CONTEXT_UPDATE, reason="SDK predates context updates")
UPDATE = "gemini.live.context_update"
MARK = "instruction-private-71ec"


def tools(name):
    return [{"function_declarations": [{"name": name, "description": "private-schema-71ec"}]}]


def reply():
    return {"serverContent": {"modelTurn": {"parts": [{"text": "ok"}]}, "turnComplete": True}}


@needs_context_update
async def test_ordered_updates_and_tool_clearing(server, instrument, client, exporter):
    instrument()
    server.scripts.append([{"setupComplete": {}}, reply(), reply(), reply()])
    async with client.aio.live.connect(model="gemini-live", config=dict(AUDIO_CONFIG, tools=tools("initial"))) as session:
        await session.send_context_update(system_instruction=MARK, tools=tools("updated"))
        await run_turn(session)
        await session.send_context_update(system_instruction="next instruction")
        await run_turn(session)
        await session.send_context_update(tools=[])
        await run_turn(session)
    # Upstream writes directly to the websocket, not via the other instrumented sends.
    wire = json.dumps(server.sockets[0].sent)
    assert MARK in wire and "contextUpdate" in wire
    by = spans_by_name(exporter)
    updates = by.get(UPDATE, [])
    assert len(updates) == 3
    assert [s.attributes["gemini.context_update.sequence"] for s in updates] == [1, 2, 3]
    assert updates[0].attributes["input.value"] == MARK
    assert "input.value" not in updates[2].attributes
    assert updates[2].attributes["gemini.tool_names"] == ()
    assert all(s.status.status_code == StatusCode.OK for s in updates)
    assert all(s.attributes["gemini.context_update.sent"] for s in updates)
    root = by["gemini.live.session"][0]
    assert all(s.parent.span_id == root.context.span_id for s in updates)
    turns = by["gemini.live.model_turn"]
    assert [s.attributes["gemini.context.system_instruction_revision"] for s in turns] == [1, 2, 2]
    assert [s.attributes["gemini.context.tools_revision"] for s in turns] == [1, 1, 3]
    assert [s.attributes["gemini.tool_names"] for s in turns] == [("updated",), ("updated",), ()]
    assert root.attributes["gemini.tool_names"] == ()
    assert not by.get("gemini.live.user_turn")  # Instructions are not a conversation turn.
    assert "private-schema-71ec" not in all_text(exporter)


@needs_context_update
@pytest.mark.parametrize("failure", [ValueError, asyncio.CancelledError])
async def test_failed_update_does_not_replace_context(server, instrument, client, exporter, monkeypatch, failure):
    instrument(config=TraceConfig(hide_inputs=True))
    server.scripts.append([{"setupComplete": {}}, reply()])
    async with client.aio.live.connect(model="gemini-live", config=AUDIO_CONFIG) as session:
        await session.send_context_update(tools=tools("kept"))
        sock = server.sockets[0]
        send = sock.send
        error = failure(MARK)

        async def fail(message):
            raise error

        monkeypatch.setattr(sock, "send", fail)
        with pytest.raises(failure) as caught:
            await session.send_context_update(system_instruction=MARK, tools=[])
        assert caught.value is error
        monkeypatch.setattr(sock, "send", send)
        await run_turn(session)
    by = spans_by_name(exporter)
    updates = by.get(UPDATE, [])
    assert len(updates) == 2
    assert updates[1].status.status_code == StatusCode.ERROR
    assert updates[1].attributes["gemini.context_update.sent"] is False
    assert updates[1].attributes["exception.type"] == failure.__name__
    turn = by["gemini.live.model_turn"][0]
    assert turn.attributes["gemini.tool_names"] == ("kept",)
    assert turn.attributes["gemini.context.system_instruction_revision"] == 0
    assert turn.attributes["gemini.context.tools_revision"] == 1
    assert MARK not in all_text(exporter)


@needs_context_update
async def test_invalid_update_records_failure_without_changing_sdk_error(server, instrument, client, exporter):
    instrument()
    server.scripts.append([{"setupComplete": {}}])
    async with client.aio.live.connect(model="gemini-live", config=AUDIO_CONFIG) as session:
        with pytest.raises(ValueError, match="At least one"):
            await session.send_context_update()
    updates = spans_by_name(exporter).get(UPDATE, [])
    assert len(updates) == 1
    assert updates[0].status.description == "context_update_failed"
    assert len(server.sockets[0].sent) == 1  # setup only


@needs_context_update
@pytest.mark.parametrize("instruction", [
    types.Content(parts=[types.Part(text=MARK)]),
    types.Part(text=MARK),
    [types.Part(text=MARK), types.Part(inline_data=types.Blob(data=b"private-media-71ec", mime_type="image/png"))],
    {"parts": [{"text": MARK}]},
])
async def test_instruction_forms_capture_only_text(server, instrument, client, exporter, instruction):
    instrument()
    server.scripts.append([{"setupComplete": {}}])
    async with client.aio.live.connect(model="gemini-live", config=AUDIO_CONFIG) as session:
        await session.send_context_update(system_instruction=instruction)
    update = spans_by_name(exporter)[UPDATE][0]
    assert update.attributes["input.value"] == MARK
    assert "private-media-71ec" not in all_text(exporter)
    assert "cHJpdmF0ZS1tZWRpYS03MWVj" not in all_text(exporter)


@needs_context_update
async def test_callable_tool_names_without_descriptions(server, instrument, client, exporter):
    def lookup(query: str) -> str:
        """private-schema-71ec"""
        return query

    instrument()
    server.scripts.append([{"setupComplete": {}}])
    async with client.aio.live.connect(model="gemini-live", config=AUDIO_CONFIG) as session:
        await session.send_context_update(tools=[lookup])
    assert spans_by_name(exporter)[UPDATE][0].attributes["gemini.tool_names"] == ("lookup",)
    assert "private-schema-71ec" not in all_text(exporter)


@needs_context_update
@pytest.mark.parametrize("newer", [{"tools": []}, {"system_instruction": "newer"}])
async def test_slow_earlier_send_cannot_restore_stale_context(server, instrument, client, exporter, monkeypatch, newer):
    instrument()
    server.scripts.append([{"setupComplete": {}}, reply()])
    async with client.aio.live.connect(model="gemini-live", config=AUDIO_CONFIG) as session:
        sock = server.sockets[0]
        send = sock.send
        sent = asyncio.Event()
        release = asyncio.Event()

        async def slow_first(message):
            first = not sent.is_set()
            await send(message)
            if first:
                sent.set()
                await release.wait()

        monkeypatch.setattr(sock, "send", slow_first)
        first = asyncio.create_task(session.send_context_update(system_instruction="older", tools=tools("older")))
        try:
            await asyncio.wait_for(sent.wait(), timeout=2)
            await session.send_context_update(**newer)
        finally:
            release.set()
            await first
        await run_turn(session)
    by = spans_by_name(exporter)
    assert [s.attributes["gemini.context_update.sequence"] for s in by[UPDATE]] == [2, 1]
    attrs = by["gemini.live.model_turn"][0].attributes
    if "tools" in newer:
        assert attrs["gemini.tool_names"] == ()
        assert attrs["gemini.context.tools_revision"] == 2
        assert attrs["gemini.context.system_instruction_revision"] == 1
    else:
        assert attrs["gemini.tool_names"] == ("older",)
        assert attrs["gemini.context.tools_revision"] == 1
        assert attrs["gemini.context.system_instruction_revision"] == 2


def test_optional_method_missing_does_not_break_instrumentation(instrument, monkeypatch):
    from traceai_google_genai_live import PATCHED

    monkeypatch.delattr(live.AsyncSession, "send_context_update", raising=False)
    originals = {(cls, name): getattr(getattr(live, cls), name) for cls, name in PATCHED
                 if hasattr(getattr(live, cls), name)}
    inst = instrument()
    assert not hasattr(live.AsyncSession, "send_context_update")
    inst.uninstrument()
    assert not hasattr(live.AsyncSession, "send_context_update")
    for (cls, name), original in originals.items():
        assert getattr(getattr(live, cls), name) is original


@needs_context_update
async def test_instrumentation_failure_does_not_prevent_send(server, instrument, client, exporter, monkeypatch):
    from traceai_google_genai_live._session import SessionTrace

    instrument()
    server.scripts.append([{"setupComplete": {}}])
    async with client.aio.live.connect(model="gemini-live", config=AUDIO_CONFIG) as session:
        def fail(*args, **kwargs):
            raise RuntimeError("instrumentation failure")

        monkeypatch.setattr(SessionTrace, "start_context_update", fail)
        assert await session.send_context_update(system_instruction=MARK) is None
    assert MARK in json.dumps(server.sockets[0].sent)


@needs_context_update
async def test_updates_survive_resume(server, instrument, client, exporter):
    from .conftest import HANDLE_1, load

    instrument()
    server.scripts += [load("j4_first_connection"), load("j4_second_connection")]
    async with client.aio.live.connect(model="gemini-live", config=dict(AUDIO_CONFIG, session_resumption={})) as session:
        await session.send_context_update(system_instruction=MARK, tools=tools("retained"))
        await run_turn(session)
        messages = session.receive()
        assert (await anext(messages)).go_away is not None
        await messages.aclose()
    async with client.aio.live.connect(model="gemini-live", config=dict(AUDIO_CONFIG, session_resumption={"handle": HANDLE_1})) as session:
        await session.send_context_update(system_instruction="after resume")
        await run_turn(session)
    by = spans_by_name(exporter)
    assert len(by["gemini.live.session"]) == 1
    assert len({s.context.trace_id for s in exporter.get_finished_spans()}) == 1
    assert [s.attributes["gemini.context_update.sequence"] for s in by[UPDATE]] == [1, 2]
    attrs = by["gemini.live.model_turn"][-1].attributes
    assert attrs["gemini.context.system_instruction_revision"] == 2
    assert attrs["gemini.context.tools_revision"] == 1
    assert attrs["gemini.tool_names"] == ("retained",)
    assert HANDLE_1 not in all_text(exporter)


@needs_context_update
async def test_existing_turn_keeps_opening_context(server, instrument, client, exporter):
    instrument()
    server.scripts.append([{"setupComplete": {}},
                           {"serverContent": {"modelTurn": {"parts": [{"text": "first"}]}}},
                           {"serverContent": {"turnComplete": True}}, reply()])
    async with client.aio.live.connect(model="gemini-live", config=AUDIO_CONFIG) as session:
        messages = session.receive()
        await anext(messages)
        await session.send_context_update(system_instruction=MARK)
        async for _ in messages:
            pass
        await run_turn(session)
    turns = spans_by_name(exporter)["gemini.live.model_turn"]
    assert [s.attributes["gemini.context.system_instruction_revision"] for s in turns] == [0, 1]


@needs_context_update
@pytest.mark.parametrize("hide", [None, "hide_inputs", "hide_input_messages", "hide_input_text",
                                 "hide_outputs", "hide_output_messages", "hide_output_text"])
async def test_privacy_and_shape_on_otlp_wire(server, instrument, client, exporter, monkeypatch, hide):
    """Serialize through the production HTTP exporter into a loopback OTLP receiver."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    from opentelemetry.sdk.trace.export import SpanExportResult

    if hide:
        monkeypatch.setenv("FI_" + hide.upper(), "true")
    instrument()
    server.scripts.append([{"setupComplete": {}}])
    async with client.aio.live.connect(model="gemini-live", config=AUDIO_CONFIG) as session:
        await session.send_context_update(system_instruction=MARK, tools=tools("visible_name"))
        sock = server.sockets[0]

        async def fail(message):
            raise ValueError("private-error-71ec")

        monkeypatch.setattr(sock, "send", fail)
        with pytest.raises(ValueError):
            await session.send_context_update(system_instruction=MARK)
    bodies = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/traces"
            assert self.headers["Content-Type"] == "application/x-protobuf"
            bodies.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    wire_exporter = OTLPSpanExporter(endpoint=f"http://127.0.0.1:{receiver.server_port}/v1/traces", headers={}, timeout=2)
    try:
        assert wire_exporter.export(exporter.get_finished_spans()) == SpanExportResult.SUCCESS
    finally:
        wire_exporter.shutdown()
        receiver.shutdown()
        receiver.server_close()
        thread.join(timeout=2)
    assert len(bodies) == 1
    payload = bodies[0]
    captures_inputs = hide is None or hide.startswith("hide_output")
    assert (MARK.encode() in payload) == captures_inputs
    assert (b"private-error-71ec" in payload) == (hide is None)
    assert b"private-schema-71ec" not in payload
    assert b"visible_name" in payload
    assert b"exception.stacktrace" not in payload
    request = ExportTraceServiceRequest.FromString(payload)
    spans = [s for r in request.resource_spans for scope in r.scope_spans for s in scope.spans]
    root = next(s for s in spans if s.name == "gemini.live.session")
    updates = [s for s in spans if s.name == UPDATE]
    assert len(updates) == 2
    assert all(s.parent_span_id == root.span_id and s.trace_id == root.trace_id for s in updates)
    assert [s.status.code for s in updates] == [1, 2]  # OTLP OK, ERROR
