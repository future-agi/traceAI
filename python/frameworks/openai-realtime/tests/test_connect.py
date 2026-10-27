"""The real openai SDK, sync and async, against the local stand-in server."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time

import pytest
from fi_instrumentation import TraceConfig
from openai import AsyncOpenAI, OpenAI
from websockets.exceptions import InvalidStatus
from openai.resources.realtime import realtime as rt
from opentelemetry.trace import StatusCode

import realtime_stub_server as stub
import voice_agent
from conftest import API_KEY, DROP_ERRORS, MARKER, by_name, root_of
from traceai_openai_realtime._session import RESPONSE_NAME, ROOT_NAME, TOOL_NAME, TURN_NAME
from voice_tab import assert_voice_tab

# Published core 1.1 retains the legacy descriptions; current core owns error privacy.
NATIVE_ERROR_POLICY = hasattr(TraceConfig(), "hide_error_details")

SDK3 = hasattr(rt.AsyncRealtimeConnectionManager, "send")  # 3.x: pre-open queue, handlers, reconnect


def _args(**kw):
    base = dict(model="gpt-realtime-2.1", marker=MARKER, user_id=None, text=False,
                typed=None, error_case=False, playback_speed=6.0, mic_pace=0.002)
    base.update(kw)
    return argparse.Namespace(**base)


def _client(server, key=API_KEY, cls=AsyncOpenAI):
    return cls(api_key=key, websocket_base_url=server.url)


def test_async_voice_call_end_to_end(instrument, exporter, stub_server):
    """J1-J3 through the real SDK: tool call, barge-in with truncate, four responses."""
    instrument(user_id="caller-1")
    # The stub generates audio at 10x real time and the example "plays" it at 3x, so
    # unplayed audio is still buffered when the scripted barge-in arrives (about
    # 330 ms of wall time into the long answer) and the example sends a truncate,
    # even when a loaded runner slows generation down.
    server = stub_server(pace=0.01)
    asyncio.run(voice_agent.voice_call(_client(server), _args(playback_speed=3.0)))
    spans = exporter.get_finished_spans()
    assert_voice_tab(spans, forbidden_substrings=(API_KEY, "UklGRg"))
    root = root_of(spans)
    assert root.attributes["user.id"] == "caller-1"
    assert root.attributes["realtime.end_reason"] == "client_closed"
    assert len(by_name(spans, TURN_NAME)) == 3
    assert len(by_name(spans, RESPONSE_NAME)) == 4
    assert len(by_name(spans, TOOL_NAME)) == 1
    (cancelled,) = [s for s in by_name(spans, RESPONSE_NAME) if s.attributes.get("realtime.cancelled")]
    assert cancelled.attributes["realtime.truncation"] == "server_confirmed"
    assert root.attributes["call.total_turns"] == 7


def test_typed_question_spoken_answer(instrument, exporter, stub_server):
    """The example's --typed mode: no microphone, one tool call, a spoken answer."""
    instrument()
    server = stub_server()
    asyncio.run(voice_agent.typed_call(_client(server), _args(typed="What's the weather in Paris?")))
    spans = exporter.get_finished_spans()
    assert_voice_tab(spans, omissions=("call.talk_ratio", "user.id"))
    root = root_of(spans)
    assert root.attributes["call.total_turns"] == 2
    rows = json.loads(root.attributes["conversation.transcript"])
    assert rows[0] == {"role": "user", "content": "What's the weather in Paris?"}
    assert rows[1]["role"] == "assistant"
    assert len(by_name(spans, TOOL_NAME)) == 1
    assert by_name(spans, TURN_NAME) == []


def _speak_sync(conn, ms_loud=600, ms_quiet=600):
    for loud, total in ((True, ms_loud), (False, ms_quiet)):
        for _ in range(total // 100):
            conn.input_audio_buffer.append(audio=base64.b64encode(voice_agent.pcm(100, loud)).decode())


def test_sync_client_voice_turn(instrument, exporter, stub_server):
    instrument()
    server = stub_server()
    client = _client(server, cls=OpenAI)
    with client.realtime.connect(model="gpt-realtime-2.1") as conn:
        conn.session.update(session={"type": "realtime", "audio": {"input": {"transcription": {"model": "gpt-4o-mini-transcribe"}}}})
        _speak_sync(conn)
        for event in conn:
            if event.type == "response.done":
                break
    spans = exporter.get_finished_spans()
    assert_voice_tab(spans)
    assert len(by_name(spans, TURN_NAME)) == 1
    (resp,) = by_name(spans, RESPONSE_NAME)
    assert resp.attributes["realtime.audio_delta_count"] > 0
    assert root_of(spans).status.status_code == StatusCode.OK


def test_socket_drop_mid_response_flushes_once_and_never_reconnects(instrument, exporter, stub_server, provider, monkeypatch):
    """J4 / AC-09: the open response and the root close ERROR socket_closed; one flush;
    the stand-in sees exactly one connection."""
    flushes = []
    real_flush = provider.force_flush
    monkeypatch.setattr(provider, "force_flush", lambda *a, **k: flushes.append(1) or real_flush(*a, **k))
    connections = []
    real_init = stub.Handler.__init__
    monkeypatch.setattr(stub.Handler, "__init__", lambda self, *a: connections.append(1) or real_init(self, *a))
    instrument()
    server = stub_server(drop_after_deltas=3)
    with pytest.raises(DROP_ERRORS):
        asyncio.run(voice_agent.voice_call(_client(server), _args()))
    time.sleep(0.2)  # the async flush runs on the default executor
    spans = exporter.get_finished_spans()
    root = root_of(spans)
    assert root.status.status_code == StatusCode.ERROR
    assert root.attributes["realtime.end_reason"] == "socket_closed"
    assert root.attributes["realtime.close_code"] == 1011
    dropped = [s for s in by_name(spans, RESPONSE_NAME) if s.attributes.get("realtime.end_reason") == "socket_closed"]
    assert len(dropped) == 1 and dropped[0].status.status_code == StatusCode.ERROR
    assert len(flushes) == 1
    assert len(connections) == 1


def test_exception_in_caller_code_is_reraised_and_recorded(instrument, exporter, stub_server):
    """AC-10: the caller sees its own exception object; the root records it as ERROR."""
    instrument()
    server = stub_server()
    boom = ValueError("tool crashed")

    async def session():
        async with _client(server).realtime.connect(model="gpt-realtime-2.1") as conn:
            async for event in conn:
                if event.type == "session.created":
                    raise boom

    with pytest.raises(ValueError) as raised:
        asyncio.run(session())
    assert raised.value is boom
    root = root_of(exporter.get_finished_spans())
    assert root.status.status_code == StatusCode.ERROR
    assert root.attributes["realtime.end_reason"] == "exception"
    (event,) = [e for e in root.events if e.name == "exception"]
    assert event.attributes["exception.type"] == "builtins.ValueError"
    assert "exception.stacktrace" not in event.attributes


def test_rejected_key_is_a_failed_connect(instrument, exporter, stub_server):
    """The upgrade is refused (HTTP 401): one CHAIN root with status ERROR, the error re-raised."""
    instrument()
    server = stub_server()
    bad_key = "sk-bad-0123456789abcdef"

    async def session():
        async with _client(server, key=bad_key).realtime.connect(model="gpt-realtime-2.1"):
            pass

    with pytest.raises(InvalidStatus) as raised:
        asyncio.run(session())
    spans = exporter.get_finished_spans()
    root = root_of(spans)
    assert len(spans) == 1
    assert root.name == ROOT_NAME
    assert root.status.status_code == StatusCode.ERROR
    assert root.attributes["realtime.end_reason"] == "connect_failed"
    assert root.attributes["fi.span.kind"] == "CHAIN"
    expected = str(raised.value) if NATIVE_ERROR_POLICY else "InvalidStatus: " + str(raised.value)
    assert root.status.description == expected
    assert [e.attributes["exception.type"] for e in root.events if e.name == "exception"] == [
        "websockets.exceptions.InvalidStatus"
    ]
    assert bad_key not in json.dumps({k: str(v) for k, v in root.attributes.items()})
    assert bad_key not in str([e.attributes for e in root.events]) + str(root.status.description)


def test_task_cancellation_closes_spans_as_task_cancelled(instrument, exporter, stub_server):
    instrument()
    server = stub_server()

    async def session(started):
        async with _client(server).realtime.connect(model="gpt-realtime-2.1") as conn:
            async for _event in conn:
                started.set()

    async def main():
        started = asyncio.Event()
        task = asyncio.create_task(session(started))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    root = root_of(exporter.get_finished_spans())
    assert root.status.status_code == StatusCode.ERROR
    assert root.attributes["realtime.end_reason"] == "task_cancelled"


def test_enter_and_close_without_context_manager(instrument, exporter, stub_server):
    instrument()
    server = stub_server()

    async def session():
        conn = await _client(server).realtime.connect(model="gpt-realtime-2.1").enter()
        await conn.session.update(session={"type": "realtime", "instructions": "hello"})
        event = await conn.recv()
        assert event.type == "session.created"
        await conn.close()

    asyncio.run(session())
    root = root_of(exporter.get_finished_spans())
    assert root.attributes["realtime.end_reason"] == "client_closed"
    assert root.status.status_code == StatusCode.OK


def test_raw_bytes_reader_is_traced(instrument, exporter, stub_server):
    """Callers that read recv_bytes() and parse themselves are traced the same way."""
    instrument()
    server = stub_server()

    async def session():
        async with _client(server).realtime.connect(model="gpt-realtime-2.1") as conn:
            await conn.session.update(session={"type": "realtime", "output_modalities": ["text"]})
            await conn.conversation.item.create(item={"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]})
            await conn.response.create()
            while True:
                event = json.loads(await conn.recv_bytes())
                if event["type"] == "response.done":
                    break

    asyncio.run(session())
    spans = exporter.get_finished_spans()
    (resp,) = by_name(spans, RESPONSE_NAME)
    assert resp.attributes["output.value"]
    assert root_of(spans).attributes["fi.span.kind"] == "CHAIN"


@pytest.mark.skipif(not SDK3, reason="handler registration and dispatch_events are openai 3.x")
def test_chained_handlers_and_dispatch_events_stay_traced(instrument, exporter, stub_server):
    instrument()
    server = stub_server()

    async def session():
        done = asyncio.Event()
        manager = _client(server).realtime.connect(model="gpt-realtime-2.1").on("response.done", lambda e: done.set())
        async with manager as conn:
            await conn.session.update(session={"type": "realtime", "output_modalities": ["text"]})
            await conn.conversation.item.create(item={"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]})
            await conn.response.create()
            dispatcher = asyncio.create_task(conn.dispatch_events())
            await asyncio.wait_for(done.wait(), 10)
            dispatcher.cancel()

    asyncio.run(session())
    spans = exporter.get_finished_spans()
    assert len(by_name(spans, RESPONSE_NAME)) == 1
    assert root_of(spans).attributes["realtime.end_reason"] in ("client_closed", "task_cancelled")


@pytest.mark.skipif(not SDK3, reason="queueing before the socket opens is openai 3.x")
def test_events_queued_before_open_are_observed(instrument, exporter, stub_server):
    instrument()
    server = stub_server()

    async def session():
        manager = _client(server).realtime.connect(model="gpt-realtime-2.1")
        manager.send({"type": "session.update", "session": {"type": "realtime", "instructions": f"queued {MARKER}"}})
        async with manager as conn:
            async for event in conn:
                if event.type == "session.updated":
                    break

    asyncio.run(session())
    assert root_of(exporter.get_finished_spans()).attributes["input.value"] == f"queued {MARKER}"


@pytest.mark.skipif(not SDK3, reason="SDK reconnect (on_reconnecting) is openai 3.x")
def test_reconnect_capable_drop_keeps_one_root(instrument, exporter, stub_server):
    """With on_reconnecting the SDK may reopen the socket on the same connection object:
    in-flight spans close, the root stays open. Here the callback aborts the retry,
    so the session then ends ERROR, still as one trace."""
    instrument()
    server = stub_server(drop_after_deltas=3)
    calls = []

    async def session():
        client = _client(server)
        async with client.realtime.connect(model="gpt-realtime-2.1", on_reconnecting=lambda e: calls.append(e) or {"abort": True}) as conn:
            await conn.session.update(session={"type": "realtime", "output_modalities": ["audio"]})
            for _ in range(2):
                await conn.conversation.item.create(item={"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]})
                await conn.response.create()
            async for _event in conn:
                pass

    with pytest.raises(DROP_ERRORS):
        asyncio.run(session())
    spans = exporter.get_finished_spans()
    root = root_of(spans)
    assert len(calls) == 1
    assert root.attributes["realtime.socket_drop_count"] == 1
    assert root.status.status_code == StatusCode.ERROR
    assert {s.context.trace_id for s in spans} == {root.context.trace_id}
