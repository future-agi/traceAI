"""Recorder tests: recorded event sequences in, spans out. No network."""

from __future__ import annotations

import copy
import json

from opentelemetry.trace import StatusCode

from conftest import AUDIO_PREFIX, by_name, load, replay, root_of
from traceai_openai_realtime._session import ERROR_NAME, RESPONSE_NAME, ROOT_NAME, TOOL_NAME, TURN_NAME
from voice_tab import assert_not_voice_tab, assert_voice_tab


def _run(make_recorder, exporter, rows, reason="client_closed", **settings):
    recorder = make_recorder(**settings)
    replay(recorder, rows)
    recorder.finish(reason)
    return exporter.get_finished_spans()


def test_voice_call_is_one_conversation_trace(make_recorder, exporter):
    """J1 / AC-02: one parentless CONVERSATION root, gen_ai.system=openai, children under it."""
    spans = _run(make_recorder, exporter, load("j1_voice_call"))
    root = root_of(spans)
    assert root.name == ROOT_NAME
    assert {s.context.trace_id for s in spans} == {root.context.trace_id}
    a = root.attributes
    assert a["fi.span.kind"] == "CONVERSATION"
    assert a["gen_ai.system"] == "openai"
    assert a["gen_ai.request.model"] == "gpt-realtime-2.1"
    assert a["session.id"] == a["realtime.openai_session_id"] == "sess_0001"
    assert a["realtime.voice"] == "marin"
    assert root.status.status_code == StatusCode.OK
    assert_voice_tab(spans, omissions=("user.id",))
    assert len(by_name(spans, TURN_NAME)) == 3
    assert len(by_name(spans, RESPONSE_NAME)) == 4
    for span in by_name(spans, TURN_NAME) + by_name(spans, RESPONSE_NAME):
        assert span.parent.span_id == root.context.span_id


def test_voice_tab_fields(make_recorder, exporter):
    """call.* numbers, the transcript in order, and the flattened rows."""
    spans = _run(make_recorder, exporter, load("j1_voice_call"))
    a = root_of(spans).attributes
    assert isinstance(a["call.duration"], float) and a["call.duration"] > 0
    assert a["call.total_turns"] == 7  # 3 user turns + 4 responses
    assert 0 < a["call.talk_ratio"] < 1
    rows = json.loads(a["conversation.transcript"])
    assert a["fi.conversation.transcript"] == a["conversation.transcript"]
    assert [r["role"] for r in rows] == ["user", "assistant", "user", "assistant", "user", "assistant"]
    assert rows[0]["content"].startswith("What's the weather in Paris")
    for i, row in enumerate(rows):
        assert a[f"conversation.transcript.{i}.message.role"] == row["role"]
        assert a[f"conversation.transcript.{i}.message.content"] == row["content"]
    assert a["input.value"].startswith("You are a concise weather assistant")


def test_audio_deltas_are_counted_not_spans(make_recorder, exporter):
    """AC-03: 200 audio deltas on one response give one response span and a count of 200."""
    rows = [r for r in load("text_session") if r["event"].get("type") != "response.output_text.delta"]
    created = next(i for i, r in enumerate(rows) if r["event"].get("type") == "response.created")
    rid = rows[created]["event"]["response"]["id"]
    deltas = [
        {"dir": "server", "event": {"type": "response.output_audio.delta", "event_id": f"event_d{i}", "response_id": rid,
                                    "item_id": "item_x", "output_index": 0, "content_index": 0, "delta": AUDIO_PREFIX + "AAAA"}}
        for i in range(200)
    ]
    spans = _run(make_recorder, exporter, rows[: created + 1] + deltas + rows[created + 1:])
    responses = by_name(spans, RESPONSE_NAME)
    assert len(responses) == 1
    assert responses[0].attributes["realtime.audio_delta_count"] == 200
    assert len(spans) == 2  # root + response, no delta spans


def test_user_turn_output_is_the_transcript(make_recorder, exporter):
    """AC-04: transcription completed sets the turn output."""
    spans = _run(make_recorder, exporter, load("j1_voice_call"))
    first = sorted(by_name(spans, TURN_NAME), key=lambda s: s.start_time)[0]
    assert first.attributes["output.value"].startswith("What's the weather in Paris")
    assert first.attributes["realtime.transcription"] == "completed"
    assert first.attributes["realtime.turn_detection"] == "server_vad"
    assert first.attributes["realtime.audio_duration_ms"] == 1200.0


def test_turn_without_transcription_still_closes(make_recorder, exporter):
    """AC-04: transcription off: the turn closes on committed, with no text."""
    spans = _run(make_recorder, exporter, load("push_to_talk"))
    (turn,) = by_name(spans, TURN_NAME)
    assert "output.value" not in turn.attributes
    assert turn.attributes["realtime.transcription"] == "not_enabled"
    assert turn.attributes["realtime.turn_detection"] == "manual"
    assert turn.status.status_code == StatusCode.OK


def test_usage_copied_verbatim_or_omitted(make_recorder, exporter):
    """AC-05: usage only from response.done, verbatim; no usage means no token keys."""
    rows = load("text_session")
    done = next(r for r in rows if r["event"].get("type") == "response.done")
    usage = done["event"]["response"]["usage"]
    spans = _run(make_recorder, exporter, rows)
    (resp,) = by_name(spans, RESPONSE_NAME)
    assert resp.attributes["gen_ai.usage.input_tokens"] == usage["input_tokens"]
    assert resp.attributes["gen_ai.usage.output_tokens"] == usage["output_tokens"]
    assert resp.attributes["gen_ai.usage.total_tokens"] == usage["total_tokens"]

    exporter.clear()
    stripped = copy.deepcopy(rows)
    for r in stripped:
        if r["event"].get("type") == "response.done":
            del r["event"]["response"]["usage"]
    spans = _run(make_recorder, exporter, stripped)
    (resp,) = by_name(spans, RESPONSE_NAME)
    assert not [k for k in resp.attributes if k.startswith("gen_ai.usage.")]


def _tool_rows(call_ids, output_call_ids):
    rows = [r for r in load("no_turns")]
    rid = "resp_tools"
    rows.append({"dir": "server", "event": {"type": "response.created", "event_id": "e1", "response": {"id": rid, "status": "in_progress"}}})
    for i, cid in enumerate(call_ids):
        rows.append({"dir": "server", "event": {"type": "response.function_call_arguments.done", "event_id": f"ea{i}",
                                                "response_id": rid, "item_id": f"item_f{i}", "output_index": i,
                                                "call_id": cid, "name": "get_weather", "arguments": json.dumps({"city": f"C{i}"})}})
    rows.append({"dir": "server", "event": {"type": "response.done", "event_id": "e2", "response": {"id": rid, "status": "completed"}}})
    for cid in output_call_ids:
        rows.append({"dir": "client", "event": {"type": "conversation.item.create",
                                                "item": {"type": "function_call_output", "call_id": cid, "output": "{\"ok\": true}"}}})
    return rows


def test_parallel_tool_calls_are_siblings_and_unmatched_is_error(make_recorder, exporter):
    """J2 / AC-06: two parallel calls are sibling tool spans under the response; an
    unknown call_id is an ERROR tool span under the root."""
    spans = _run(make_recorder, exporter, _tool_rows(["call_a", "call_b"], ["call_a", "call_b", "call_zzz"]))
    root = root_of(spans)
    (resp,) = by_name(spans, RESPONSE_NAME)
    tools = by_name(spans, TOOL_NAME)
    matched = [t for t in tools if not t.attributes.get("realtime.unmatched")]
    unmatched = [t for t in tools if t.attributes.get("realtime.unmatched")]
    assert len(matched) == 2 and len(unmatched) == 1
    assert {t.parent.span_id for t in matched} == {resp.context.span_id}
    assert all(t.status.status_code == StatusCode.OK for t in matched)
    assert unmatched[0].parent.span_id == root.context.span_id
    assert unmatched[0].status.status_code == StatusCode.ERROR
    assert json.loads(matched[0].attributes["input.value"])["city"] in ("C0", "C1")
    assert matched[0].attributes["output.value"] == "{\"ok\": true}"


def test_tool_arguments_that_do_not_parse_stay_raw(make_recorder, exporter):
    rows = _tool_rows(["call_a"], ["call_a"])
    for r in rows:
        if r["event"].get("type") == "response.function_call_arguments.done":
            r["event"]["arguments"] = "{\"city\": \"Par"  # truncated JSON
    spans = _run(make_recorder, exporter, rows)
    (tool,) = by_name(spans, TOOL_NAME)
    assert tool.attributes["input.value"] == "{\"city\": \"Par"


def test_follow_up_answer_is_a_new_response_not_a_tool_child(make_recorder, exporter):
    """J2: the spoken answer after the tool result is its own response under the root."""
    spans = _run(make_recorder, exporter, load("j1_voice_call"))
    root = root_of(spans)
    responses = sorted(by_name(spans, RESPONSE_NAME), key=lambda s: s.start_time)
    (tool,) = by_name(spans, TOOL_NAME)
    assert tool.parent.span_id == responses[0].context.span_id
    assert responses[1].parent.span_id == root.context.span_id
    inputs = json.loads(responses[1].attributes["input.value"])
    assert inputs[-1]["role"] == "tool"


def test_barge_in_closes_only_on_cancel(make_recorder, exporter):
    """J3 / AC-07: speech_started alone leaves the response open; response.done with
    status cancelled closes it with realtime.cancelled=true and status OK."""
    rows = load("j1_voice_call")
    recorder = make_recorder()
    cut = next(
        i for i, r in enumerate(rows)
        if r["event"].get("type") == "input_audio_buffer.speech_started" and i > 20
    )
    replay(recorder, rows[: cut + 1])
    open_responses = [s for s in exporter.get_finished_spans() if s.name == RESPONSE_NAME]
    assert len(open_responses) == 1  # only the tool-call response has ended
    replay(recorder, rows[cut + 1:])
    recorder.finish("client_closed")
    cancelled = [s for s in by_name(exporter.get_finished_spans(), RESPONSE_NAME) if s.attributes.get("realtime.cancelled")]
    assert len(cancelled) == 1
    span = cancelled[0]
    assert span.status.status_code == StatusCode.OK
    assert span.attributes["realtime.response.status"] == "cancelled"
    assert span.attributes["realtime.response.status_reason"] == "turn_detected"


def test_truncate_recorded_not_invented(make_recorder, exporter):
    """AC-08: the client's truncate sets audio_end_ms (server-confirmed here); without
    a truncate the cancelled response says truncation=not_sent and keeps its transcript."""
    rows = load("j1_voice_call")
    truncate = next(r for r in rows if r["event"].get("type") == "conversation.item.truncate")
    spans = _run(make_recorder, exporter, rows)
    (cancelled,) = [s for s in by_name(spans, RESPONSE_NAME) if s.attributes.get("realtime.cancelled")]
    assert cancelled.attributes["realtime.audio_end_ms"] == truncate["event"]["audio_end_ms"]
    assert cancelled.attributes["realtime.truncation"] == "server_confirmed"
    assert cancelled.attributes["realtime.audio_duration_ms"] <= truncate["event"]["audio_end_ms"]

    exporter.clear()
    no_truncate = [r for r in rows if r["event"].get("type") not in ("conversation.item.truncate", "conversation.item.truncated")]
    spans = _run(make_recorder, exporter, no_truncate)
    (cancelled,) = [s for s in by_name(spans, RESPONSE_NAME) if s.attributes.get("realtime.cancelled")]
    assert cancelled.attributes["realtime.truncation"] == "not_sent"
    assert "realtime.audio_end_ms" not in cancelled.attributes
    assert cancelled.attributes["output.value"].startswith("It is eighteen degrees")

    exporter.clear()
    unconfirmed = [r for r in rows if r["event"].get("type") != "conversation.item.truncated"]
    spans = _run(make_recorder, exporter, unconfirmed)
    (cancelled,) = [s for s in by_name(spans, RESPONSE_NAME) if s.attributes.get("realtime.cancelled")]
    assert cancelled.attributes["realtime.truncation"] == "client_sent_unconfirmed"


def test_error_event_is_a_child_span_and_session_stays_open(make_recorder, exporter):
    """J5: an error event is a realtime.error child with type and code; the root stays OK."""
    spans = _run(make_recorder, exporter, load("j5_errors"))
    root = root_of(spans)
    errors = by_name(spans, ERROR_NAME)
    assert len(errors) == 3  # two client mistakes + one key-bearing message; the replay is deduped
    codes = sorted(e.attributes["error.code"] for e in errors)
    assert codes == ["invalid_api_key", "invalid_event", "response_cancel_not_active"]
    assert all(e.attributes["error.type"] == "invalid_request_error" for e in errors)
    assert all(e.status.status_code == StatusCode.ERROR for e in errors)
    assert all(e.parent.span_id == root.context.span_id for e in errors)
    assert root.status.status_code == StatusCode.OK


def test_duplicate_event_id_does_not_double_span(make_recorder, exporter):
    """AC-12: a replayed server event yields one span."""
    rows = load("text_session")
    created = next(r for r in rows if r["event"].get("type") == "response.created")
    spans = _run(make_recorder, exporter, rows[:3] + [created, created] + rows[3:])
    assert len(by_name(spans, RESPONSE_NAME)) == 1


def test_session_without_turns_omits_turn_fields(make_recorder, exporter):
    """AC-18: no closed turn means no call.total_turns and no call.talk_ratio (absent, not 0)."""
    spans = _run(make_recorder, exporter, load("no_turns"))
    assert_voice_tab(
        spans,
        omissions=("call.total_turns", "call.talk_ratio", "conversation.transcript", "fi.conversation.transcript", "user.id"),
    )
    assert isinstance(root_of(spans).attributes["call.duration"], float)


def test_text_session_is_chain_and_not_a_call(make_recorder, exporter):
    """A text-only session is traced, kind CHAIN, with no call.* fields."""
    spans = _run(make_recorder, exporter, load("text_session"))
    assert_not_voice_tab(spans)
    a = root_of(spans).attributes
    assert a["fi.span.kind"] == "CHAIN"
    assert not [k for k in a if k.startswith("call.")]
    (resp,) = by_name(spans, RESPONSE_NAME)
    assert resp.attributes["output.value"].startswith("It is eighteen degrees")
    assert json.loads(resp.attributes["input.value"])[0]["role"] == "user"


def test_customer_session_and_user_ids_win(make_recorder, exporter):
    spans = _run(make_recorder, exporter, load("no_turns"), session_id="call-42", user_id="cust-7")
    a = root_of(spans).attributes
    assert a["session.id"] == "call-42"
    assert a["user.id"] == "cust-7"
    assert a["realtime.openai_session_id"] == "sess_0001"


def test_session_end_closes_open_spans(make_recorder, exporter):
    """Spans still open when the session ends are closed, with the reason recorded."""
    rows = load("j1_voice_call")
    cut = next(i for i, r in enumerate(rows) if r["event"].get("type") == "response.output_audio.delta")
    spans = _run(make_recorder, exporter, rows[: cut + 3], reason="socket_closed")
    root = root_of(spans)
    assert root.status.status_code == StatusCode.ERROR
    assert root.attributes["realtime.end_reason"] == "socket_closed"
    dropped = [s for s in by_name(spans, RESPONSE_NAME) if s.attributes.get("realtime.end_reason")]
    assert dropped and all(s.status.status_code == StatusCode.ERROR for s in dropped)
    assert all(s.end_time is not None for s in spans)


def test_finish_is_idempotent(make_recorder, exporter):
    recorder = make_recorder()
    replay(recorder, load("no_turns"))
    recorder.finish("client_closed")
    recorder.finish("socket_closed")
    assert len(exporter.get_finished_spans()) == 1
    assert root_of(exporter.get_finished_spans()).attributes["realtime.end_reason"] == "client_closed"


def test_events_after_finish_are_ignored(make_recorder, exporter):
    recorder = make_recorder()
    recorder.finish("client_closed")
    replay(recorder, load("text_session"))
    assert len(exporter.get_finished_spans()) == 1


def test_beta_event_names_are_read(make_recorder, exporter):
    """Servers that still send the beta spellings (response.audio.delta, ...) are traced the same."""
    rows = copy.deepcopy(load("j1_voice_call"))
    rename = {"response.output_audio.delta": "response.audio.delta",
              "response.output_audio_transcript.done": "response.audio_transcript.done"}
    for r in rows:
        if r["event"].get("type") in rename:
            r["event"]["type"] = rename[r["event"]["type"]]
    spans = _run(make_recorder, exporter, rows)
    assert sum(s.attributes.get("realtime.audio_delta_count", 0) for s in by_name(spans, RESPONSE_NAME)) > 0
    assert json.loads(root_of(spans).attributes["conversation.transcript"])[1]["role"] == "assistant"


def test_malformed_events_never_raise(make_recorder, exporter):
    recorder = make_recorder()
    for event in ({}, {"type": None}, {"type": "response.done", "response": "x"}, {"type": "error", "error": 3},
                  {"type": "response.function_call_arguments.done", "call_id": 5},
                  {"type": "session.updated", "session": []}, {"type": "conversation.item.truncated", "item_id": None}):
        recorder.on_server_event(event)
    recorder.on_server_frame(b"not json")
    recorder.on_server_frame(b"[1, 2]")
    for event in ("{bad", 7, None, {"type": "conversation.item.create", "item": "x"}):
        recorder.on_client_event(event)
    recorder.finish("client_closed")
    assert root_of(exporter.get_finished_spans()).name == ROOT_NAME
