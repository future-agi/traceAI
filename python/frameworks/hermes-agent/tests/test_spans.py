"""Span tree, attributes and timing for one Hermes turn."""

from opentelemetry.trace import StatusCode

from conftest import by_name, run_tool_turn


def test_one_trace_per_turn_with_llm_and_tool_children(make_hermes, exporter):
    ctx = make_hermes()
    run_tool_turn(ctx)
    spans = exporter.get_finished_spans()
    assert sorted(s.name for s in spans) == ["chat gpt-6-luna", "hermes.turn", "read_file"]
    assert len({s.context.trace_id for s in spans}) == 1
    named = by_name(spans)
    turn = named["hermes.turn"]
    assert turn.parent is None
    assert named["chat gpt-6-luna"].parent.span_id == turn.context.span_id
    assert named["read_file"].parent.span_id == turn.context.span_id


def test_turn_attributes(make_hermes, exporter):
    ctx = make_hermes()
    run_tool_turn(ctx)
    turn = by_name(exporter.get_finished_spans())["hermes.turn"]
    a = dict(turn.attributes)
    assert a["gen_ai.span.kind"] == "AGENT"
    assert a["gen_ai.operation.name"] == "invoke_agent"
    assert a["session.id"] == "20261014_120000_abc123"
    assert a["gen_ai.conversation.id"] == "20261014_120000_abc123"
    assert a["hermes.turn_id"] == "turn-1"
    assert a["hermes.platform"] == "cli"
    assert a["hermes.model"] == "gpt-6-luna"
    assert a["hermes.completed"] is True
    assert a["hermes.turn_exit_reason"] == "final_response"
    # The turn span carries no model/token fields, so cost is not double counted.
    assert "gen_ai.request.model" not in a
    assert turn.status.status_code == StatusCode.UNSET


def test_llm_span_model_usage_and_timing(make_hermes, exporter):
    ctx = make_hermes()
    run_tool_turn(ctx)
    llm = by_name(exporter.get_finished_spans())["chat gpt-6-luna"]
    a = dict(llm.attributes)
    assert a["gen_ai.span.kind"] == "LLM"
    assert a["gen_ai.request.model"] == "gpt-6-luna"
    assert a["gen_ai.response.model"] == "gpt-6-luna-2026"
    assert a["gen_ai.provider.name"] == "custom"
    assert a["gen_ai.usage.input_tokens"] == 900
    assert a["gen_ai.usage.output_tokens"] == 20
    assert a["gen_ai.usage.total_tokens"] == 920
    assert a["gen_ai.usage.cache_read_tokens"] == 100
    assert a["gen_ai.usage.output_tokens.reasoning"] == 5
    assert tuple(a["gen_ai.response.finish_reasons"]) == ("tool_calls",)
    assert a["hermes.time_to_first_chunk_ms"] == 100
    assert a["session.id"] == "20261014_120000_abc123"
    # Only the host of the base URL; the user:pw part never leaves.
    assert a["server.address"] == "127.0.0.1"
    assert abs((llm.end_time - llm.start_time) / 1e9 - 0.5) < 0.01
    assert llm.status.status_code == StatusCode.OK


def test_tool_span(make_hermes, exporter):
    ctx = make_hermes()
    run_tool_turn(ctx)
    tool = by_name(exporter.get_finished_spans())["read_file"]
    a = dict(tool.attributes)
    assert a["gen_ai.span.kind"] == "TOOL"
    assert a["gen_ai.operation.name"] == "execute_tool"
    assert a["gen_ai.tool.name"] == "read_file"
    assert a["gen_ai.tool.call.id"] == "call-1"
    assert a["hermes.tool.status"] == "ok"
    assert a["hermes.tool.duration_ms"] == 12
    assert tool.status.status_code == StatusCode.OK


def test_turns_of_one_session_share_session_id_not_trace(make_hermes, exporter):
    ctx = make_hermes()
    run_tool_turn(ctx, turn_id="turn-1")
    run_tool_turn(ctx, turn_id="turn-2")
    turns = [s for s in exporter.get_finished_spans() if s.name == "hermes.turn"]
    assert len(turns) == 2
    assert turns[0].context.trace_id != turns[1].context.trace_id
    assert {s.attributes["session.id"] for s in turns} == {"20261014_120000_abc123"}


def test_turn_opened_by_api_request_when_pre_llm_call_is_skipped(make_hermes, exporter):
    ctx = make_hermes()
    base = dict(session_id="s", task_id="t", turn_id="turn-x", platform="cron")
    ctx.fire("pre_api_request", **base, api_request_id="r", model="m", provider="p")
    ctx.fire("post_api_request", **base, api_request_id="r", model="m", usage=None)
    ctx.fire("on_session_end", **base, completed=True, failed=False, interrupted=False)
    spans = by_name(exporter.get_finished_spans())
    assert spans["chat m"].parent.span_id == spans["hermes.turn"].context.span_id


def test_cli_exit_without_turn_id_closes_open_turn(make_hermes, exporter):
    ctx = make_hermes()
    base = dict(session_id="s1", task_id="t", turn_id="turn-1", platform="cli")
    ctx.fire("pre_llm_call", **base, model="m")
    ctx.fire("pre_tool_call", **base, tool_name="terminal", args={}, tool_call_id="c1")
    # hermes_cli/cli_shutdown.py shape on Ctrl+C: no turn_id.
    ctx.fire("on_session_end", session_id="s1", completed=False, interrupted=True)
    spans = by_name(exporter.get_finished_spans())
    assert spans["hermes.turn"].attributes["hermes.interrupted"] is True
    assert spans["terminal"].attributes["hermes.unfinished"] is True


def test_subagent_turn_nests_under_parent_turn(make_hermes, exporter):
    ctx = make_hermes()
    parent = dict(session_id="parent", task_id="t", turn_id="p-turn", platform="cli")
    ctx.fire("pre_llm_call", **parent, model="m")
    ctx.fire("pre_tool_call", **parent, tool_name="delegate_task", args={}, tool_call_id="d1")
    ctx.fire("subagent_start", parent_session_id="parent", parent_turn_id="p-turn",
             child_session_id="child", child_role="worker", child_goal="x")
    child = dict(session_id="child", task_id="t2", turn_id="c-turn", platform="cli")
    ctx.fire("pre_llm_call", **child, model="m", parent_session_id="parent")
    ctx.fire("on_session_end", **child, completed=True, failed=False, interrupted=False)
    ctx.fire("subagent_stop", parent_session_id="parent", child_session_id="child")
    ctx.fire("post_tool_call", **parent, tool_name="delegate_task", tool_call_id="d1",
             status="ok", duration_ms=5, result="done")
    ctx.fire("on_session_end", **parent, completed=True, failed=False, interrupted=False)
    turns = {s.attributes["hermes.turn_id"]: s for s in exporter.get_finished_spans()
             if s.name == "hermes.turn"}
    assert turns["c-turn"].parent.span_id == turns["p-turn"].context.span_id
    assert turns["c-turn"].context.trace_id == turns["p-turn"].context.trace_id
