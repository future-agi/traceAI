"""Session, turn, flag and tool mapping (J1-J3, J5, J6, J8; AC-03..AC-10, AC-15, AC-16, AC-21, AC-23)."""

from __future__ import annotations

import asyncio
import json

import pytest
from google.genai import errors, types
from opentelemetry.trace import StatusCode

from ._voice_tab import assert_not_voice_tab, assert_voice_tab
from .conftest import AUDIO_CONFIG, load, run_turn, spans_by_name

MODEL = "gemini-3.8-live"


async def test_j1_realtime_audio_is_one_conversation(server, instrument, client, exporter):
    instrument()
    server.scripts.append(load("j1_realtime_audio"))
    async with client.aio.live.connect(model=MODEL, config=AUDIO_CONFIG) as session:
        for _ in range(5):
            await session.send_realtime_input(audio=types.Blob(data=b"\x01\x02" * 1600,
                                                               mime_type="audio/pcm;rate=16000"))
        await run_turn(session)
    spans = exporter.get_finished_spans()
    by = spans_by_name(exporter)
    root = assert_voice_tab(spans, forbidden_substrings=("fixture-resumption-handle",))
    attrs = dict(root.attributes)
    assert root.parent is None
    assert attrs["fi.span.kind"] == "CONVERSATION"
    assert attrs["gen_ai.system"] == "google_genai"
    assert attrs["gen_ai.request.model"] == MODEL
    assert attrs["gemini.session_id_source"] == "local"
    assert attrs["gemini.realtime_input_count"] == 5
    assert attrs["call.total_turns"] == 2
    assert isinstance(attrs["call.duration"], float)
    # 2.0 s of user speech (voiceActivity offsets) against 3 x 960 bytes of 24 kHz PCM.
    assert attrs["call.talk_ratio"] == pytest.approx((3 * 960 / 48000) / 2.0, rel=1e-3)
    rows = json.loads(attrs["conversation.transcript"])
    assert rows == [{"role": "user", "content": "What is the weather in Paris?"},
                    {"role": "assistant", "content": "It is sunny."}]
    assert attrs["fi.conversation.transcript"] == attrs["conversation.transcript"]
    assert attrs["conversation.transcript.1.message.role"] == "assistant"

    (user,) = by["gemini.live.user_turn"]
    assert user.attributes["gemini.turn_start"] == "server_activity"
    assert user.attributes["gemini.turn_boundary"] == "activity_end"
    assert user.attributes["input.value"] == "What is the weather in Paris?"
    (model,) = by["gemini.live.model_turn"]
    assert model.parent.span_id == root.context.span_id
    assert model.attributes["fi.span.kind"] == "LLM"
    assert model.attributes["llm.model_name"] == MODEL
    assert model.attributes["output.value"] == "It is sunny."
    assert model.attributes["input.value"] == "What is the weather in Paris?"
    assert model.attributes["llm.token_count.prompt"] == 700
    assert model.attributes["llm.token_count.completion"] == 40
    assert model.attributes["gemini.generation_complete"] is True
    assert model.attributes["gemini.turn_complete"] is True
    assert model.attributes["gemini.output_audio_bytes"] == 3 * 960
    (setup,) = by["gemini.live.setup"]
    assert setup.attributes["url.path"].startswith("/ws/google.ai.generativelanguage.")
    assert "?" not in setup.attributes["url.path"]
    assert user.end_time <= model.start_time


async def test_j2_tool_span_closes_on_matching_response(server, instrument, client, exporter):
    instrument()
    server.scripts.append(load("j2_tool"))
    async with client.aio.live.connect(model=MODEL, config=AUDIO_CONFIG) as session:
        await session.send_client_content(turns={"role": "user", "parts": [{"text": "Weather in Paris?"}]})
        await run_turn(session, tool_result={"result": "18C and sunny"})
        await run_turn(session)
    by = spans_by_name(exporter)
    (tool,) = by["gemini.live.tool"]
    first, second = sorted(by["gemini.live.model_turn"], key=lambda s: s.start_time)
    root = by["gemini.live.session"][0]
    assert tool.parent.span_id == first.context.span_id
    assert tool.attributes["tool.name"] == "get_weather"
    assert tool.attributes["gen_ai.tool.call.id"] == "call_1"
    assert json.loads(tool.attributes["input.value"]) == {"city": "Paris"}
    assert json.loads(tool.attributes["output.value"]) == {"result": "18C and sunny"}
    assert tool.status.status_code == StatusCode.OK
    # The following model turn is a sibling of the first, not a child of the tool.
    assert second.parent.span_id == root.context.span_id
    assert first.attributes["gemini.turn_start"] == "tool_call"
    assert first.attributes["llm.token_count.prompt"] == 616
    assert second.attributes["llm.token_count.prompt"] == 648
    assert json.loads(first.attributes["output.value"]) == [{"name": "get_weather", "args": {"city": "Paris"}}]
    assert "18C and sunny" in second.attributes["input.value"]
    assert second.attributes["output.value"] == "Paris is 18C and sunny."
    (user,) = by["gemini.live.user_turn"]
    assert user.attributes["gemini.input_mode"] == "client_content"
    assert user.attributes["gemini.turn_boundary"] == "client_turn_complete"


async def test_j3_interrupt_then_cancel_and_late_response_ignored(server, instrument, client, exporter):
    instrument()
    server.scripts.append(load("j3_interrupt_cancel"))
    async with client.aio.live.connect(model=MODEL, config=AUDIO_CONFIG) as session:
        msgs = await run_turn(session)
        assert any(m.tool_call_cancellation for m in msgs)
        # The customer answers anyway: not attached to the cancelled span (AC-09).
        await session.send_tool_response(function_responses=[
            types.FunctionResponse(id="call_9", name="get_weather", response={"result": "LATE-RESULT"})])
    by = spans_by_name(exporter)
    (tool,) = by["gemini.live.tool"]
    assert tool.attributes["gemini.cancelled"] is True
    assert tool.status.status_code == StatusCode.OK
    assert "output.value" not in tool.attributes
    (model,) = by["gemini.live.model_turn"]
    assert model.attributes["gemini.interrupted"] is True
    assert model.attributes["gemini.close_reason"] == "interrupted"
    (late,) = by["gemini.live.tool_response"]
    assert late.attributes["gemini.ignored"] is True
    assert late.parent.span_id == by["gemini.live.session"][0].context.span_id
    assert "LATE-RESULT" not in json.dumps(dict(late.attributes))


@pytest.mark.parametrize("fixture,open_after,flag", [
    ("flag_generation_complete", True, "gemini.generation_complete"),
    ("flag_turn_complete", False, "gemini.turn_complete"),
    ("flag_interrupted", False, "gemini.interrupted"),
])
async def test_ac05_three_flags(server, instrument, client, exporter, fixture, open_after, flag):
    instrument()
    server.scripts.append(load(fixture))
    async with client.aio.live.connect(model=MODEL, config=AUDIO_CONFIG) as session:
        it = session.receive()
        await it.__anext__()  # model audio
        await it.__anext__()  # the flag
        await it.aclose()
        names = [s.name for s in exporter.get_finished_spans()]
        assert ("gemini.live.model_turn" not in names) is open_after
    model = spans_by_name(exporter)["gemini.live.model_turn"][0]
    assert model.attributes.get(flag) is True
    if fixture == "flag_generation_complete":
        assert model.attributes["gemini.close_reason"] == "connection_end"


async def test_interrupted_wins_over_later_turn_complete(server, instrument, client, exporter):
    instrument()
    server.scripts.append(load("flag_interrupted"))
    async with client.aio.live.connect(model=MODEL, config=AUDIO_CONFIG) as session:
        await run_turn(session)
    (model,) = spans_by_name(exporter)["gemini.live.model_turn"]
    assert model.attributes["gemini.interrupted"] is True
    assert "gemini.turn_complete" not in model.attributes


async def test_ac06_client_content_interrupts_open_model_turn(server, instrument, client, exporter):
    instrument()
    server.scripts.append(load("flag_generation_complete"))
    async with client.aio.live.connect(model=MODEL, config=AUDIO_CONFIG) as session:
        it = session.receive()
        await it.__anext__()
        await it.aclose()
        await session.send_client_content(turns={"role": "user", "parts": [{"text": "Stop, new question"}]})
    by = spans_by_name(exporter)
    (model,) = by["gemini.live.model_turn"]
    assert model.attributes["gemini.interrupted"] is True
    assert model.attributes["gemini.close_reason"] == "client_content"
    (user,) = by["gemini.live.user_turn"]
    assert user.start_time >= model.start_time


async def test_ac07_audio_frames_are_counted_not_spanned(server, instrument, client, exporter):
    instrument()
    server.scripts.append([{"setupComplete": {}}])
    async with client.aio.live.connect(model=MODEL, config=AUDIO_CONFIG) as session:
        for _ in range(100):
            await session.send_realtime_input(audio=types.Blob(data=b"\x00" * 320, mime_type="audio/pcm;rate=16000"))
    names = [s.name for s in exporter.get_finished_spans()]
    assert names.count("gemini.live.user_turn") == 0
    root = spans_by_name(exporter)["gemini.live.session"][0]
    assert root.attributes["gemini.realtime_input_count"] == 100
    assert root.attributes["gemini.realtime_audio_bytes"] == 32000
    assert len(names) == 2  # root + setup


async def test_ac08_unknown_tool_response_is_error_on_root(server, instrument, client, exporter):
    instrument()
    server.scripts.append([{"setupComplete": {}}])
    async with client.aio.live.connect(model=MODEL, config=AUDIO_CONFIG) as session:
        await session.send_tool_response(function_responses=[
            types.FunctionResponse(id="nope", name="get_weather", response={"x": 1})])
    by = spans_by_name(exporter)
    (late,) = by["gemini.live.tool_response"]
    assert late.attributes["gemini.unmatched"] is True
    assert late.status.status_code == StatusCode.ERROR
    assert late.parent.span_id == by["gemini.live.session"][0].context.span_id


async def test_duplicate_tool_call_id_opens_one_span(server, instrument, client, exporter):
    instrument()
    call = {"toolCall": {"functionCalls": [{"name": "f", "args": {}, "id": "dup"}]}}
    server.scripts.append([{"setupComplete": {}}, call, call, {"serverContent": {"turnComplete": True}}])
    async with client.aio.live.connect(model=MODEL, config=AUDIO_CONFIG) as session:
        await run_turn(session, tool_result={"ok": True})
    assert len(spans_by_name(exporter)["gemini.live.tool"]) == 1


async def test_ac03_text_session_is_chain_and_not_listed(server, instrument, client, exporter):
    instrument()
    server.scripts.append(load("text_session"))
    async with client.aio.live.connect(model=MODEL, config={"response_modalities": ["TEXT"]}) as session:
        await session.send_client_content(turns={"role": "user", "parts": [{"text": "hello"}]})
        await run_turn(session)
    spans = exporter.get_finished_spans()
    root = assert_not_voice_tab(spans)
    assert root.attributes["fi.span.kind"] == "CHAIN"
    assert "call.duration" not in root.attributes
    assert "conversation.transcript" not in root.attributes
    (model,) = spans_by_name(exporter)["gemini.live.model_turn"]
    assert model.attributes["output.value"] == "Hi there."


async def test_default_modality_is_audio(server, instrument, client, exporter):
    instrument()
    server.scripts.append([{"setupComplete": {}}])
    async with client.aio.live.connect(model=MODEL):
        pass
    assert_voice_tab(exporter.get_finished_spans())


async def test_ac04_ac23_setup_rejected_is_one_error_root(server, instrument, client, exporter):
    instrument()
    server.scripts.append(load("j8_setup_rejected"))
    with pytest.raises(errors.APIError):
        async with client.aio.live.connect(model="models/x", config=AUDIO_CONFIG):
            pass
    spans = exporter.get_finished_spans()
    by = spans_by_name(exporter)
    assert set(by) == {"gemini.live.session", "gemini.live.setup"}
    root = assert_voice_tab(spans, omissions=("call.total_turns", "call.talk_ratio", "conversation.transcript",
                                              "fi.conversation.transcript", "user.id"))
    assert root.status.status_code == StatusCode.ERROR
    assert root.status.description == "setup_rejected"
    assert by["gemini.live.setup"][0].status.status_code == StatusCode.ERROR
    assert by["gemini.live.setup"][0].attributes["gemini.close_code"] == 1008


async def test_ac15_ac16_socket_death_reraises_and_flushes_once(server, instrument, client, exporter, provider):
    flushes = []
    provider.force_flush = lambda *a, **k: flushes.append(1) or True  # type: ignore[method-assign]
    inst = instrument()
    server.scripts.append(load("j6_socket_death"))
    with pytest.raises(errors.APIError) as raised:
        async with client.aio.live.connect(model=MODEL, config=AUDIO_CONFIG) as session:
            await run_turn(session)
    assert raised.value.code == 1006  # the exception the caller sees is the SDK's own
    by = spans_by_name(exporter)
    root = by["gemini.live.session"][0]
    assert root.status.status_code == StatusCode.ERROR
    assert root.status.description == "socket_closed"
    (model,) = by["gemini.live.model_turn"]
    assert model.status.status_code == StatusCode.ERROR
    assert model.attributes["gemini.close_reason"] == "socket_closed"
    events = [e.name for e in root.events]
    assert "gemini.receive_error" in events
    assert server.connects == 1  # no reconnect from the instrumentor
    # The flush runs on its own thread; give a loaded machine time to start it.
    for _ in range(200):
        if flushes:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)  # and room for a second flush to show up if there were one
    assert inst._inst.flush_count == 1
    assert len(flushes) == 1


async def test_task_cancellation_closes_error_task_cancelled(server, instrument, client, exporter):
    instrument()
    gate = asyncio.Event()
    server.scripts.append([{"setupComplete": {}}])

    async def body():
        async with client.aio.live.connect(model=MODEL, config=AUDIO_CONFIG):
            await gate.wait()

    task = asyncio.create_task(body())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    root = spans_by_name(exporter)["gemini.live.session"][0]
    assert root.status.description == "task_cancelled"


async def test_ac21_usage_is_copied_not_summed(server, instrument, client, exporter):
    instrument()
    server.scripts.append(load("j2_tool"))
    async with client.aio.live.connect(model=MODEL, config=AUDIO_CONFIG) as session:
        await run_turn(session, tool_result={"r": 1})
        await run_turn(session)
    values = []
    for span in exporter.get_finished_spans():
        for key, value in (span.attributes or {}).items():
            if "token" in key:
                values.append(value)
        for ev in span.events:
            values.extend(v for k, v in ev.attributes.items() if "token" in k)
    assert 628 in values and 729 in values
    assert 628 + 729 not in values
    assert 616 + 648 not in values


async def test_customer_session_and_user_ids(server, instrument, client, exporter):
    from fi_instrumentation import using_session, using_user

    instrument()
    server.scripts.append([{"setupComplete": {}}])
    with using_session("call-42"), using_user("user-7"):
        async with client.aio.live.connect(model=MODEL, config=AUDIO_CONFIG):
            pass
    root = spans_by_name(exporter)["gemini.live.session"][0]
    assert root.attributes["session.id"] == "call-42"
    assert root.attributes["gemini.session_id_source"] == "customer"
    assert root.attributes["user.id"] == "user-7"


async def test_real_recorded_audio_session(server, instrument, client, exporter):
    """Frames recorded from gemini-3.8-live on 2026-10-15 (audio replaced by silence)."""
    instrument()
    server.scripts.append(load("real_20261015_audio_tool"))
    async with client.aio.live.connect(model=MODEL, config=AUDIO_CONFIG) as session:
        await run_turn(session, tool_result={"result": "18C and sunny"})
        await run_turn(session)
    by = spans_by_name(exporter)
    root = assert_voice_tab(exporter.get_finished_spans(), forbidden_substrings=("fixture-resumption-handle",))
    (user,) = by["gemini.live.user_turn"]
    assert user.attributes["gemini.turn_boundary"] == "activity_end"
    assert "Paris" in user.attributes["input.value"]
    assert len(by["gemini.live.model_turn"]) == 2
    (tool,) = by["gemini.live.tool"]
    assert tool.attributes["tool.name"] == "get_weather"
    assert root.attributes["call.total_turns"] == 3
    assert "call.talk_ratio" in root.attributes


async def test_real_recorded_text_input_session(server, instrument, client, exporter):
    instrument()
    server.scripts.append(load("real_20261015_text_tool"))
    async with client.aio.live.connect(model=MODEL, config=AUDIO_CONFIG) as session:
        await session.send_client_content(turns={"role": "user", "parts": [{"text": "What's the weather in Paris?"}]})
        await run_turn(session, tool_result={"result": "18C and sunny"})
        await run_turn(session)
    by = spans_by_name(exporter)
    assert len(by["gemini.live.model_turn"]) == 2
    assert by["gemini.live.tool"][0].status.status_code == StatusCode.OK
    root = by["gemini.live.session"][0]
    assert "call.talk_ratio" not in root.attributes  # no measured user speech
