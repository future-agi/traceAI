"""goAway, resume with the stored handle, resume failure, unknown handle (J4; AC-10..AC-14)."""

from __future__ import annotations

import asyncio

import pytest
from google.genai import errors
from opentelemetry.trace import StatusCode

from .conftest import AUDIO_CONFIG, HANDLE_1, HANDLE_2, load, run_turn, spans_by_name

MODEL = "gemini-3.8-live"


def resume_config(handle=None):
    return dict(AUDIO_CONFIG, session_resumption={"handle": handle} if handle else {})


async def _first_connection(client, *, stop_after_go_away: bool = True):
    handle = None
    async with client.aio.live.connect(model=MODEL, config=resume_config()) as session:
        for msg in await run_turn(session):
            if msg.session_resumption_update and msg.session_resumption_update.new_handle:
                handle = msg.session_resumption_update.new_handle
        async for msg in session.receive():
            if msg.go_away is not None and stop_after_go_away:
                break
    return handle


async def test_ac10_go_away_does_not_close_root(server, instrument, client, exporter):
    instrument()
    server.scripts.append(load("j4_first_connection"))
    async with client.aio.live.connect(model=MODEL, config=resume_config()) as session:
        await run_turn(session)
        it = session.receive()
        msg = await it.__anext__()
        assert msg.go_away is not None
        names = [s.name for s in exporter.get_finished_spans()]
        assert "gemini.live.session" not in names
        await it.aclose()


async def test_ac11_resume_with_stored_handle_is_one_trace(server, instrument, client, exporter, log_text):
    instrument()
    server.scripts += [load("j4_first_connection"), load("j4_second_connection")]
    handle = await _first_connection(client)
    assert handle == HANDLE_1
    # The connection ended after goAway: the root waits for a resume.
    assert "gemini.live.session" not in [s.name for s in exporter.get_finished_spans()]
    async with client.aio.live.connect(model=MODEL, config=resume_config(handle)) as session:
        await run_turn(session)
    spans = exporter.get_finished_spans()
    roots = [s for s in spans if s.parent is None]
    assert len(roots) == 1
    assert len({s.context.trace_id for s in spans}) == 1
    by = spans_by_name(exporter)
    (reconnect,) = by["gemini.live.reconnect"]
    assert reconnect.attributes["gemini.reconnect.after_go_away"] is True
    assert reconnect.attributes["gemini.reconnect.gap_ms"] >= 0
    assert reconnect.status.status_code == StatusCode.OK
    root = roots[0]
    assert root.attributes["gemini.go_away"] is True
    assert root.attributes["gemini.time_left"] == "10s"
    assert root.attributes["gemini.reconnect_count"] == 1
    assert len(by["gemini.live.setup"]) == 2
    assert len(by["gemini.live.model_turn"]) == 2
    # AC-14: neither handle is on any span, event or log line.
    from .conftest import all_text

    exported = all_text(exporter)
    for h in (HANDLE_1, HANDLE_2):
        assert h not in exported
        assert h not in log_text()
    assert server.connects == 2  # both connects were the customer's


async def test_ac12_resume_failure_closes_error_and_does_not_reconnect(server, instrument, client, exporter):
    instrument()
    server.scripts += [load("j4_first_connection"), [["close", 1008, "bad handle"]]]
    handle = await _first_connection(client)
    with pytest.raises(errors.APIError):
        async with client.aio.live.connect(model=MODEL, config=resume_config(handle)):
            pass
    by = spans_by_name(exporter)
    root = by["gemini.live.session"][0]
    assert root.status.status_code == StatusCode.ERROR
    assert root.status.description == "resume_failed"
    (reconnect,) = by["gemini.live.reconnect"]
    assert reconnect.status.status_code == StatusCode.ERROR
    assert server.connects == 2


async def test_ac13_unknown_handle_is_a_new_trace(server, instrument, client, exporter):
    instrument()
    server.scripts.append(load("j4_second_connection"))
    async with client.aio.live.connect(model=MODEL, config=resume_config("handle-from-another-process")) as session:
        await run_turn(session)
    by = spans_by_name(exporter)
    root = by["gemini.live.session"][0]
    assert root.parent is None
    assert root.attributes["gemini.resumed_unknown_handle"] is True
    assert "gemini.live.reconnect" not in by
    assert "handle-from-another-process" not in str(dict(root.attributes))


async def test_detached_root_closes_after_resume_window(server, instrument, client, exporter):
    instrument(resume_window_seconds=0.05)
    server.scripts.append(load("j4_first_connection"))
    await _first_connection(client)
    assert "gemini.live.session" not in [s.name for s in exporter.get_finished_spans()]
    await asyncio.sleep(0.15)
    root = spans_by_name(exporter)["gemini.live.session"][0]
    assert root.status.status_code == StatusCode.OK  # goAway then a clean exit is not an error
    assert root.attributes["call.total_turns"] == 1


async def test_clean_exit_without_go_away_closes_immediately(server, instrument, client, exporter):
    instrument()
    server.scripts.append(load("j4_second_connection"))
    async with client.aio.live.connect(model=MODEL, config=resume_config()) as session:
        await run_turn(session)
    assert "gemini.live.session" in [s.name for s in exporter.get_finished_spans()]


async def test_uninstrument_closes_detached_sessions(server, instrument, client, exporter):
    inst = instrument()
    server.scripts.append(load("j4_first_connection"))
    await _first_connection(client)
    inst.uninstrument()
    assert "gemini.live.session" in [s.name for s in exporter.get_finished_spans()]
