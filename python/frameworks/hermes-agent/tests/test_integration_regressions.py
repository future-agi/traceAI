"""Unified-core parity and native Hermes regression contracts (run in E2B)."""
import json

import pytest
from opentelemetry.trace import StatusCode
from traceai_hermes_agent import FutureAGIHermesPlugin
from traceai_hermes_agent._config import resolve
from traceai_hermes_agent._recorder import HermesSpanRecorder
from test_shared_config import shared, write


@pytest.mark.parametrize("layer", [
    {"transport": None}, {"transport": []}, {"transport": "bad"},
    {"transport": {"endpoint": 7}}, {"transport": {"baseUrl": False}},
    {"transport": {"baseUrl": "https://example.invalid/path"}},
    {"transport": {"endpoint": "http://example.invalid/traces"}},
    {"transport": {"endpoint": ""}}, {"transport": {"extra": "x"}},
    {"tags": None}, {"tags": [2]}, {"projectName": "invalid\nheader"},
])
def test_bad_project_layer_preserves_user_runtime(shared, layer, monkeypatch, provider):
    env, _, host, root = shared
    write(root / ".futureagi/agents.json", {"hermes": {"schemaVersion": 1, **layer}})
    c = resolve(env.get, host=host, project=root)
    assert c["enabled"] and c["issues"] == ["PROJECT_CONFIG_IGNORED"]
    assert c["endpoint"] == "http://127.0.0.1:4318/tracer/v1/traces"
    monkeypatch.setattr("traceai_hermes_agent._setting", env.get)
    monkeypatch.setattr("traceai_hermes_agent.build_tracer_provider", lambda **kw: provider)
    plugin = FutureAGIHermesPlugin(project=root, register_atexit=False)
    plugin.callback("pre_llm_call")(session_id="s", turn_id="t")
    assert plugin._provider is provider and not plugin._disabled


@pytest.mark.parametrize("layer", [{"projectName": "unapproved"}, {"profile": "unapproved"},
                                    {"transport": {"endpoint": "https://unapproved.invalid/traces"}}])
def test_inherited_keys_do_not_authorize_repository_routing(shared, layer):
    env, _, host, root = shared
    env.update(FI_API_KEY="synthetic-override", FI_SECRET_KEY="synthetic-secret")
    write(root / ".futureagi/agents.json", {"hermes": {"schemaVersion": 1, **layer}})
    assert not resolve(env.get, host=host, project=root)["enabled"]


@pytest.mark.parametrize("terminal", ["finalize", "shutdown", "incomplete", "interrupted", "recovered"])
@pytest.mark.parametrize("hide_errors", [True, False])
def test_provider_failure_outcome(provider, exporter, terminal, hide_errors):
    r = HermesSpanRecorder(provider.get_tracer("test"), capture_content=True, hide_errors=hide_errors)
    base = dict(session_id="s", turn_id="t", api_request_id="r", model="m")
    r.on_pre_api_request(**base)
    r.on_api_request_error(**base, status_code=400, retryable=False,
                           error={"type": "InvalidRequest", "message": "PRIVATE_PROVIDER_ERROR"})
    if terminal == "recovered":
        r.on_pre_api_request(**{**base, "api_request_id": "fallback"})
        r.on_post_api_request(**{**base, "api_request_id": "fallback"})
        r.on_session_end(**base, completed=True)
    elif terminal == "shutdown":
        r.end_all()
    elif terminal == "finalize":
        r.on_session_finalize(session_id="s")
    else:
        r.on_session_end(**base, completed=False, interrupted=terminal == "interrupted")
    root = next(s for s in exporter.get_finished_spans() if s.name == "hermes.turn")
    expected = "success" if terminal == "recovered" else "interrupted" if terminal == "interrupted" else "error"
    assert root.attributes["fi.agent.outcome"] == expected
    assert root.status.status_code == (StatusCode.OK if expected == "success" else StatusCode.ERROR)
    if hide_errors:
        assert "PRIVATE_PROVIDER_ERROR" not in str(root.status.description)


def origin(r, turn="original", session="s", delegation="deleg_1"):
    base = dict(session_id=session, turn_id=turn, telemetry_schema_version="hermes.observer.v1")
    r.on_pre_llm_call(**base, user_message="delegate a task")
    r.on_subagent_start(parent_turn_id=turn, parent_session_id=session, child_session_id="child")
    r.on_post_tool_call(**base, tool_name="delegate_task", tool_call_id="call", status="ok",
                       result=json.dumps({"status": "dispatched", "mode": "background", "delegation_id": delegation}))
    r.on_session_end(**base, completed=True)


def complete(r, turn="notification", session="s", delegation="deleg_1", typed=True):
    row = {"role": "user", "content": "[ASYNC DELEGATION BATCH COMPLETE]"}
    if typed:
        row.update(display_kind="async_delegation_complete", display_metadata={"delegation_id": delegation})
    r.on_pre_llm_call(session_id=session, turn_id=turn, telemetry_schema_version="hermes.observer.v1",
                      user_message=row["content"], conversation_history=[row])
    r.on_pre_api_request(session_id=session, turn_id=turn, api_request_id=turn, model="m")
    r.on_post_api_request(session_id=session, turn_id=turn, api_request_id=turn, model="m",
                          usage={"prompt_tokens": 10, "output_tokens": 2, "total_tokens": 12})
    r.on_session_end(session_id=session, turn_id=turn, completed=True)


def test_background_context_survives_ended_origin_without_new_root_or_usage(provider, exporter):
    r = HermesSpanRecorder(provider.get_tracer("test"))
    origin(r)
    r.on_pre_llm_call(session_id="child", turn_id="child-t")  # delayed until after parent ended
    r.on_session_end(session_id="child", turn_id="child-t", completed=True)
    r.on_subagent_stop(child_session_id="child")
    # A genuine subsequent user prompt is independent, even while a child is pending.
    complete(r, turn="human", typed=False)
    complete(r)
    spans = exporter.get_finished_spans()
    roots = [s for s in spans if s.parent is None]
    assert len(roots) == 2
    first = next(s for s in roots if s.attributes["hermes.turn_id"] == "original")
    continuation = next(s for s in spans if s.name == "hermes.continuation")
    child = next(s for s in spans if s.attributes.get("hermes.turn_id") == "child-t")
    assert continuation.parent.span_id == first.context.span_id == child.parent.span_id
    assert continuation.context.trace_id == first.context.trace_id == child.context.trace_id
    assert sum(s.context.span_id == first.context.span_id for s in spans) == 1
    assert sum(s.attributes.get("gen_ai.usage.total_tokens", 0) for s in spans) == 24
    models = [s for s in spans if s.attributes.get("gen_ai.span.kind") == "LLM"]
    assert all(s.attributes["fi.usage.scope"] == "request" and s.attributes["fi.usage.contribution"] == "additive" for s in models)
    tool = next(s for s in spans if s.name == "delegate_task")
    assert tool.attributes["tool.name"] == "delegate_task" and tool.attributes["tool_call.id"] == "call"


@pytest.mark.parametrize("case", ["other-session", "unknown-delegation", "prompt-only", "expired"])
def test_untrusted_or_expired_completion_never_adopts_trace(provider, exporter, case, monkeypatch):
    r = HermesSpanRecorder(provider.get_tracer("test"))
    origin(r)
    if case == "expired":
        monkeypatch.setattr("traceai_hermes_agent._recorder.time.monotonic", lambda: 1e20)
    complete(r, session="other" if case == "other-session" else "s",
             delegation="unknown" if case == "unknown-delegation" else "deleg_1", typed=case != "prompt-only")
    assert len([s for s in exporter.get_finished_spans() if s.parent is None]) == 2
    assert not any(s.name == "hermes.continuation" for s in exporter.get_finished_spans())


@pytest.mark.parametrize("typed", [True, False])
def test_cli_timeline_header_requires_native_display_type(provider, exporter, typed):
    r = HermesSpanRecorder(provider.get_tracer("test"))
    origin(r)
    row = {"role": "user", "content": "[ASYNC DELEGATION BATCH COMPLETE — deleg_1]\\nResult"}
    if typed:
        row.update(display_kind="async_delegation_complete", display_metadata={"display_text": "Task completed"})
    r.on_pre_llm_call(session_id="s", turn_id="next", telemetry_schema_version="hermes.observer.v1",
                      user_message=row["content"], conversation_history=[row])
    r.on_session_end(session_id="s", turn_id="next", completed=True)
    spans = exporter.get_finished_spans()
    assert len([s for s in spans if s.parent is None]) == (1 if typed else 2)
    assert any(s.name == "hermes.continuation" for s in spans) is typed


def test_retained_context_is_bounded_and_contains_no_prompt(provider):
    r = HermesSpanRecorder(provider.get_tracer("test"), capture_content=True)
    for i in range(270):
        origin(r, turn=str(i), delegation=str(i))
    assert len(r._delegations) == 256
    assert all(set(vars(v)) == {"context", "expires_at"} for v in r._delegations.values())
