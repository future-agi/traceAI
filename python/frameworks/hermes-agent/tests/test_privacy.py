"""Content is off by default; FI_HERMES_CAPTURE_CONTENT=true turns it on."""

import json

from opentelemetry.trace import StatusCode

from conftest import LOCAL_PATH, SECRET, by_name, run_tool_turn


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
    run_tool_turn(ctx, turn_id="turn-4", turn_failed=True)
    dump = _dump(exporter.get_finished_spans())
    assert SECRET not in dump
    assert LOCAL_PATH not in dump and "alice" not in dump
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
             request={"method": "POST",
                      "body": {"messages": [{"role": "user", "content": "sanitized"}]}})
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


def test_exit_reason_is_reduced_to_its_category_by_default(make_hermes, exporter):
    # The three reasons agent/turn_loop_errors.py builds from str(e).
    ctx = make_hermes(capture=False)
    for i, prefix in enumerate(("repeated_outer_errors", "local_processing_error",
                                "error_near_max_iterations")):
        base = dict(session_id="s", task_id="t", turn_id="u{0}".format(i), platform="cli")
        ctx.fire("pre_llm_call", **base, model="m")
        ctx.fire("on_session_end", **base, completed=False, failed=True, interrupted=False,
                 turn_exit_reason="{0}(Error during API call #3: [Errno 2] No such file or "
                                  "directory: '{1}' {2})".format(prefix, LOCAL_PATH, SECRET))
    turns = exporter.get_finished_spans()
    assert [t.attributes["hermes.turn_exit_reason"] for t in turns] == [
        "repeated_outer_errors", "local_processing_error", "error_near_max_iterations"]
    assert [t.status.description for t in turns] == [
        "repeated_outer_errors", "local_processing_error", "error_near_max_iterations"]
    dump = _dump(turns)
    assert LOCAL_PATH not in dump and SECRET not in dump


def test_cli_exit_reason_is_reduced_too(make_hermes, exporter):
    ctx = make_hermes(capture=False)
    ctx.fire("pre_llm_call", session_id="s", task_id="t", turn_id="u", platform="cli", model="m")
    ctx.fire("on_session_end", session_id="s", completed=False, interrupted=True,
             reason="keyboard_interrupt(" + LOCAL_PATH + ")")
    assert LOCAL_PATH not in _dump(exporter.get_finished_spans())


def test_opt_in_keeps_error_text(make_hermes, exporter):
    ctx = make_hermes(capture=True)
    run_tool_turn(ctx, turn_failed=True)
    run_tool_turn(ctx, turn_id="turn-2", fail_api=True)
    run_tool_turn(ctx, turn_id="turn-3", tool_status="error")
    spans = exporter.get_finished_spans()
    failed_turn = [s for s in spans if s.name == "hermes.turn"
                   and s.attributes["hermes.turn_id"] == "turn-1"][0]
    assert LOCAL_PATH in failed_turn.attributes["hermes.turn_exit_reason"]
    llm_errors = [s for s in spans if s.name.startswith("chat") and s.status.description
                  and "429" in s.status.description]
    assert llm_errors and LOCAL_PATH in llm_errors[0].status.description
    tool = [s for s in spans if s.name == "read_file" and s.attributes.get("error.type")][0]
    assert LOCAL_PATH in tool.status.description


def test_opt_in_llm_input_is_the_latest_messages_not_the_system_prompt(make_hermes, exporter,
                                                                        monkeypatch):
    monkeypatch.setenv("FI_HERMES_MAX_CHARS", "2000")
    ctx = make_hermes(capture=True)
    base = dict(session_id="s", task_id="t", turn_id="u", platform="cli")
    request = {"method": "POST", "body": {"model": "m", "messages": [
        {"role": "system", "content": "You are Hermes. " * 3000},
        {"role": "user", "content": "old question " * 300},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "read the readme " + SECRET},
    ]}}
    ctx.fire("pre_api_request", **base, api_request_id="r", model="m", request=request)
    ctx.fire("post_api_request", **base, api_request_id="r", model="m")
    ctx.fire("on_session_end", **base, completed=True)
    value = json.loads(by_name(exporter.get_finished_spans())["chat m"].attributes["input.value"])
    assert value["system_prompt_chars"] == len("You are Hermes. " * 3000)
    assert value["message_count"] == 4
    assert value["messages"][-1]["content"].endswith(SECRET)
    assert all(m["role"] != "system" for m in value["messages"])
