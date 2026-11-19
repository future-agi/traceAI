"""Privacy regressions found by the shared native-adapter conformance kit."""
import copy
import json

import pytest
from opentelemetry.trace import StatusCode

from traceai_hermes_agent._config import resolve
from traceai_hermes_agent._privacy import redact
from traceai_hermes_agent._recorder import HermesSpanRecorder
from test_shared_config import shared

TOKEN = "sk-abcdefghijklmnopqrstuvwxyz1234567890"
BASE = dict(session_id="s", turn_id="t", model="m")


def dump(exporter):
    return json.dumps([{"attributes": dict(s.attributes), "status": s.status.description}
                       for s in exporter.get_finished_spans()])


def test_recognizable_credentials_redacted_from_every_content_boundary(provider, exporter):
    r = HermesSpanRecorder(provider.get_tracer("test"), capture_content=True)
    r.on_pre_llm_call(**BASE, user_message="hello " + TOKEN)
    r.on_pre_api_request(**BASE, api_request_id="r", request_messages=[{"role": "user", "content": TOKEN}])
    r.on_post_api_request(**BASE, api_request_id="r", assistant_message={"content": TOKEN})
    r.on_post_tool_call(**BASE, tool_name="read", tool_call_id="tool", args={"password": "SYNTH_PASSWORD"}, result=TOKEN)
    r.on_pre_api_request(**BASE, api_request_id="failed")
    r.on_api_request_error(**BASE, api_request_id="failed", error={"type": "RequestError", "message": TOKEN})
    r.on_post_llm_call(**BASE, assistant_response=TOKEN)
    r.on_session_end(**BASE, failed=True, turn_exit_reason="error(" + TOKEN + ")")
    text = dump(exporter)
    assert TOKEN not in text and "SYNTH_PASSWORD" not in text
    assert "[REDACTED]" in text
    root = next(s for s in exporter.get_finished_spans() if s.name == "hermes.turn")
    assert root.attributes["input.value"].startswith("hello ")
    assert root.status.status_code == StatusCode.ERROR


@pytest.mark.parametrize("prefix", ["sk-", "ghp_", "github_pat_"])
def test_redaction_before_truncation_and_inside_encoded_arguments(provider, exporter, prefix):
    r = HermesSpanRecorder(provider.get_tracer("test"), capture_content=True, max_chars=256)
    r.on_pre_llm_call(**BASE, user_message="x" * 250 + " " + prefix + "abcdefgh12345678")
    r.on_session_end(**BASE, completed=True)
    assert prefix not in dump(exporter)
    source = {"arguments": json.dumps({"nested": {"api_key": "SYNTH_VALUE"}, "tokens": "SYNTH_TOKEN"}),
              "input_tokens": 5}
    original = copy.deepcopy(source)
    clean = redact(source)
    assert source == original and clean["input_tokens"] == 5
    assert "SYNTH_VALUE" not in str(clean) and "SYNTH_TOKEN" not in str(clean)


@pytest.mark.parametrize("flag", ["FI_HIDE_OUTPUTS", "FI_HIDE_OUTPUT_TEXT", "FI_HIDE_OUTPUT_MESSAGES"])
def test_output_flags_apply_to_all_request_history_envelopes(shared, provider, exporter, flag):
    env, _, host, project = shared
    env[flag] = "true"
    config = resolve(env.get, host=host, project=project)
    r = HermesSpanRecorder(provider.get_tracer("test"), capture_content=config["capture_content"],
                           hide_outputs=config["hide_outputs"], hide_errors=config["hide_errors"])
    messages = [
        {"role": "user", "content": "VISIBLE_PROMPT"},
        {"role": "assistant", "content": "HIDDEN_ANSWER", "tool_calls": [{"arguments": "HIDDEN_ARGS"}]},
        {"role": "tool", "content": "HIDDEN_TOOL"},
        {"type": "function_call_output", "output": "HIDDEN_RESPONSE"},
        {"role": "user", "content": [{"type": "text", "text": "VISIBLE_FOLLOWUP"},
                                    {"type": "tool_result", "content": "HIDDEN_BLOCK"}]},
    ]
    original = copy.deepcopy(messages)
    for i, payload in enumerate((dict(request_messages=messages), dict(request={"messages": messages}),
                                 dict(request={"body": {"messages": messages}}),
                                 dict(request={"body": {"input": messages}}))):
        r.on_pre_api_request(**BASE, api_request_id=str(i), **payload)
        r.on_post_api_request(**BASE, api_request_id=str(i), assistant_message={"content": "HIDDEN_CURRENT"})
    r.on_session_end(**BASE, completed=True)
    assert messages == original
    text = dump(exporter)
    assert "HIDDEN_" not in text and "VISIBLE_PROMPT" in text and "VISIBLE_FOLLOWUP" in text


@pytest.mark.parametrize("message", [
    {"role": "tool", "isError": True, "content": "PRIVATE_ERROR"},
    {"role": "tool", "content": '{"error":"PRIVATE_ERROR"}'},
    {"type": "function_call_output", "output": '{"error":"PRIVATE_ERROR"}'},
    {"role": "user", "content": [{"type": "tool_result", "is_error": True, "content": "PRIVATE_ERROR"}]},
])
def test_error_details_filtered_from_history_without_losing_successful_content(provider, exporter, message):
    r = HermesSpanRecorder(provider.get_tracer("test"), capture_content=True, hide_errors=True)
    messages = [{"role": "user", "content": "VISIBLE_PROMPT"}, message,
                {"role": "tool", "content": "VISIBLE_TOOL"}, {"role": "assistant", "content": "VISIBLE_ANSWER"}]
    original = copy.deepcopy(messages)
    r.on_pre_api_request(**BASE, api_request_id="r", request_messages=messages)
    r.on_post_api_request(**BASE, api_request_id="r", assistant_message={"content": "VISIBLE_CURRENT"})
    r.on_session_end(**BASE, failed=True, turn_exit_reason="PRIVATE_ERROR")
    assert messages == original
    text = dump(exporter)
    assert "PRIVATE_ERROR" not in text
    assert all(s in text for s in ("VISIBLE_PROMPT", "VISIBLE_TOOL", "VISIBLE_ANSWER", "VISIBLE_CURRENT"))
    root = next(s for s in exporter.get_finished_spans() if s.name == "hermes.turn")
    assert root.status.status_code == StatusCode.ERROR and root.status.description == "other"
    assert root.attributes["hermes.turn_exit_reason"] == "other"


@pytest.mark.parametrize("capture,hide_errors", [(False, False), (True, True)])
def test_unknown_exit_reasons_are_not_safe_categories(provider, exporter, capture, hide_errors):
    r = HermesSpanRecorder(provider.get_tracer("test"), capture_content=capture, hide_errors=hide_errors)
    r.on_pre_llm_call(**BASE, user_message="hello")
    r.on_session_end(**BASE, failed=True, turn_exit_reason="PRIVATE_ERROR(text)")
    assert "PRIVATE_ERROR" not in dump(exporter)


def test_error_history_visible_when_explicitly_allowed(provider, exporter):
    r = HermesSpanRecorder(provider.get_tracer("test"), capture_content=True, hide_errors=False)
    r.on_pre_api_request(**BASE, api_request_id="r", request_messages=[{"role": "tool", "isError": True, "content": "VISIBLE_ERROR"}])
    r.on_post_api_request(**BASE, api_request_id="r")
    r.on_session_end(**BASE, failed=True, turn_exit_reason="VISIBLE_ERROR")
    spans = exporter.get_finished_spans()
    assert "VISIBLE_ERROR" in spans[0].attributes["input.value"]
    assert spans[-1].attributes["hermes.turn_exit_reason"] == "VISIBLE_ERROR"
