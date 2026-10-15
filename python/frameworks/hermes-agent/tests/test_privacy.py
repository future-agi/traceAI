"""Content is off by default; FI_HERMES_CAPTURE_CONTENT=true turns it on."""

import json

from opentelemetry.trace import StatusCode

from conftest import SECRET, by_name, run_tool_turn


def _dump(spans) -> str:
    return json.dumps([{"name": s.name, "attributes": dict(s.attributes),
                        "status": s.status.description, "events": [
                            {"name": e.name, "attributes": dict(e.attributes)} for e in s.events]}
                       for s in spans], default=str)


def test_default_exports_no_prompt_response_or_tool_text(make_hermes, exporter):
    ctx = make_hermes(capture=False)
    run_tool_turn(ctx)
    run_tool_turn(ctx, turn_id="turn-2", tool_status="error")
    run_tool_turn(ctx, turn_id="turn-3", fail_api=True)
    dump = _dump(exporter.get_finished_spans())
    assert SECRET not in dump
    for span in exporter.get_finished_spans():
        assert "input.value" not in span.attributes
        assert "output.value" not in span.attributes


def test_default_error_status_is_a_category_not_the_provider_text(make_hermes, exporter):
    ctx = make_hermes(capture=False)
    run_tool_turn(ctx, fail_api=True)
    llm = by_name(exporter.get_finished_spans())["chat gpt-6-luna"]
    assert llm.status.status_code == StatusCode.ERROR
    assert llm.status.description == "rate_limit"


def test_opt_in_exports_content(make_hermes, exporter):
    ctx = make_hermes(capture=True)
    run_tool_turn(ctx)
    spans = by_name(exporter.get_finished_spans())
    assert SECRET in spans["hermes.turn"].attributes["input.value"]
    assert SECRET in spans["hermes.turn"].attributes["output.value"]
    assert SECRET in spans["chat gpt-6-luna"].attributes["input.value"]
    assert spans["chat gpt-6-luna"].attributes["input.mime_type"] == "application/json"
    assert "read_file" in spans["chat gpt-6-luna"].attributes["output.value"]
    assert SECRET in spans["read_file"].attributes["input.value"]
    assert SECRET in spans["read_file"].attributes["output.value"]


def test_opt_in_uses_sanitized_request_not_raw_history(make_hermes, exporter):
    ctx = make_hermes(capture=True)
    base = dict(session_id="s", task_id="t", turn_id="u", platform="cli")
    ctx.fire("pre_api_request", **base, api_request_id="r", model="m",
             request_messages=[{"role": "user", "content": "raw"}],
             request={"messages": [{"role": "user", "content": "sanitized"}]})
    ctx.fire("post_api_request", **base, api_request_id="r", model="m")
    ctx.fire("on_session_end", **base, completed=True)
    value = by_name(exporter.get_finished_spans())["chat m"].attributes["input.value"]
    assert "sanitized" in value and "raw" not in value


def test_captured_text_is_truncated(make_hermes, exporter, monkeypatch):
    monkeypatch.setenv("FI_HERMES_MAX_CHARS", "300")
    ctx = make_hermes(capture=True)
    base = dict(session_id="s", task_id="t", turn_id="u", platform="cli")
    ctx.fire("pre_llm_call", **base, user_message="x" * 5000)
    ctx.fire("on_session_end", **base, completed=True)
    value = exporter.get_finished_spans()[0].attributes["input.value"]
    assert len(value) < 400 and value.endswith("[truncated 4700 chars]")
