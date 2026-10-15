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


def test_failed_turn_is_error(make_hermes, exporter):
    ctx = make_hermes()
    run_tool_turn(ctx, turn_failed=True)
    turn = by_name(exporter.get_finished_spans())["hermes.turn"]
    assert turn.status.status_code == StatusCode.ERROR
    assert turn.attributes["hermes.failed"] is True


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


def test_pre_tool_call_never_returns_a_directive(make_hermes):
    ctx = make_hermes()
    assert ctx.fire("pre_tool_call", tool_name="terminal", args={"command": "rm -rf /"},
                    session_id="s", turn_id="t", tool_call_id="c") == [None]


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
    for hook in ("pre_llm_call", "pre_tool_call"):
        ctx.register_hook(hook, plugin.callback(hook))
    ctx.fire("pre_llm_call", session_id="s", turn_id="u", model="m")
    ctx.fire("pre_tool_call", session_id="s", turn_id="u", tool_name="terminal", tool_call_id="c")
    plugin.shutdown()
    spans = by_name(exporter.get_finished_spans())
    assert spans["hermes.turn"].attributes["hermes.unfinished"] is True
    assert spans["terminal"].attributes["hermes.unfinished"] is True
