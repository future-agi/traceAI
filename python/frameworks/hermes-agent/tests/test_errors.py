"""Errors become ERROR spans; a broken payload never reaches Hermes."""

from opentelemetry.trace import StatusCode

from conftest import FakeContext, by_name, run_tool_turn
from traceai_hermes_agent import FutureAGIHermesPlugin, register


def test_failed_tool_is_error(make_hermes, exporter):
    ctx = make_hermes()
    run_tool_turn(ctx, tool_status="error")
    tool = by_name(exporter.get_finished_spans())["read_file"]
    assert tool.status.status_code == StatusCode.ERROR
    assert tool.status.description == "FileNotFoundError"
    assert tool.attributes["error.type"] == "FileNotFoundError"
    assert (tool.end_time - tool.start_time) == 12_000_000


def test_blocked_tool_is_error(make_hermes, exporter):
    ctx = make_hermes()
    run_tool_turn(ctx, tool_status="blocked")
    tool = by_name(exporter.get_finished_spans())["read_file"]
    assert tool.status.status_code == StatusCode.ERROR
    assert tool.attributes["hermes.tool.status"] == "blocked"


def test_provider_error_marks_llm_span(make_hermes, exporter):
    ctx = make_hermes()
    run_tool_turn(ctx, fail_api=True)
    llm = by_name(exporter.get_finished_spans())["chat gpt-6-luna"]
    assert llm.status.status_code == StatusCode.ERROR
    assert llm.attributes["http.response.status_code"] == 429
    assert llm.attributes["hermes.retryable"] is True
    assert llm.attributes["error.type"] == "rate_limit"
    assert llm.attributes["hermes.error_class"] == "RateLimitError"


def test_failed_turn_is_error(make_hermes, exporter):
    ctx = make_hermes()
    run_tool_turn(ctx, turn_failed=True)
    turn = by_name(exporter.get_finished_spans())["hermes.turn"]
    assert turn.status.status_code == StatusCode.ERROR
    assert turn.status.description == "repeated_outer_errors"
    assert turn.attributes["hermes.failed"] is True
    assert turn.attributes["hermes.turn_exit_reason"] == "repeated_outer_errors"


def test_retry_reuses_request_id_without_leaking_spans(make_hermes, exporter):
    ctx = make_hermes()
    base = dict(session_id="s", task_id="t", turn_id="u", platform="cli")
    ctx.fire("pre_api_request", **base, api_request_id="r", model="m", retry_count=0)
    ctx.fire("api_request_error", **base, api_request_id="r", model="m", reason="timeout")
    ctx.fire("pre_api_request", **base, api_request_id="r", model="m", retry_count=1)
    ctx.fire("post_api_request", **base, api_request_id="r", model="m")
    ctx.fire("on_session_end", **base, completed=True)
    llm = [s for s in exporter.get_finished_spans() if s.name == "chat m"]
    assert [s.status.status_code for s in llm] == [StatusCode.ERROR, StatusCode.OK]


def test_malformed_payloads_never_raise(make_hermes, exporter):
    ctx = make_hermes()
    for hook in list(ctx.hooks):
        assert ctx.fire(hook) == [None]
        assert ctx.fire(hook, session_id=object(), turn_id=3, usage="bad", started_at="x",
                        args=object(), result=object(), assistant_message=42) == [None]


def test_no_policy_hooks_are_registered(make_hermes):
    # pre_tool_call is a fail-closed directive hook in Hermes: a slow or failing
    # callback there blocks the tool. Tool spans come from post_tool_call.
    ctx = make_hermes()
    assert "pre_tool_call" not in ctx.hooks
    assert "pre_gateway_dispatch" not in ctx.hooks
    assert "pre_verify" not in ctx.hooks


def test_missing_keys_make_the_plugin_a_noop(monkeypatch):
    monkeypatch.delenv("FI_API_KEY", raising=False)
    monkeypatch.delenv("FI_SECRET_KEY", raising=False)
    plugin = FutureAGIHermesPlugin()
    assert plugin.recorder() is None
    ctx = FakeContext()
    register(ctx)
    run_tool_turn(ctx)  # nothing to assert beyond: no exception, no exporter


def test_process_exit_closes_open_spans(make_hermes, exporter, provider):
    plugin = FutureAGIHermesPlugin(provider)
    ctx = FakeContext()
    for hook in ("pre_llm_call", "pre_api_request"):
        ctx.register_hook(hook, plugin.callback(hook))
    ctx.fire("pre_llm_call", session_id="s", turn_id="u", model="m")
    ctx.fire("pre_api_request", session_id="s", turn_id="u", api_request_id="r", model="m")
    plugin.shutdown()
    spans = by_name(exporter.get_finished_spans())
    assert spans["hermes.turn"].attributes["hermes.unfinished"] is True
    assert spans["chat m"].attributes["hermes.unfinished"] is True
