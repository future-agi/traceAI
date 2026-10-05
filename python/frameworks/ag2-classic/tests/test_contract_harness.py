"""Shared-harness contract test for traceAI-ag2-classic.

Runs ``_contract_app.py`` in a subprocess with the real ``autogen`` 0.14.x,
the real OpenTelemetry SDK and Future AGI's real OTLP/HTTP exporter, pointed at
a loopback harness ``Receiver`` through ``FI_BASE_URL`` (exporter path
``/tracer/v1/traces``). The model is a loopback fake; no network, no vendor
API, placeholder keys only.

The Receiver drops resource attributes and headers; those are asserted in
``test_setup.py``.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

import pytest

HERE = Path(__file__).resolve().parent
PACKAGE_DIR = HERE.parent
PYTHON_DIR = PACKAGE_DIR.parent.parent
sys.path.insert(0, str(PYTHON_DIR / "tests"))

from harness import Receiver, run  # noqa: E402

from _scenarios import SECRET_PROMPT, TOOL_SECRET_CITY  # noqa: E402

CONTENT_KEYS = (
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
    "ag2.human_input.prompt",
    "ag2.human_input.response",
    "ag2.code_execution.output",
    "ag2.chats.summaries",
    "input.value",
    "output.value",
)


def _value(any_value: Dict[str, Any]) -> Any:
    if "stringValue" in any_value:
        return any_value["stringValue"]
    if "intValue" in any_value:
        return int(any_value["intValue"])
    if "doubleValue" in any_value:
        return float(any_value["doubleValue"])
    if "boolValue" in any_value:
        return bool(any_value["boolValue"])
    return any_value


def _flatten(span: Dict[str, Any]) -> Dict[str, Any]:
    return {item["key"]: _value(item.get("value", {})) for item in span.get("attributes", [])}


def _run_app(receiver: Receiver, mode: str) -> Dict[str, Any]:
    env = dict(os.environ)
    env.update(
        {
            "FI_BASE_URL": receiver.origin,
            "FI_API_KEY": "placeholder-api-key",
            "FI_SECRET_KEY": "placeholder-secret-key",
            "OPENAI_API_KEY": "sk-placeholder-not-a-real-key",
            "PYTHONPATH": os.pathsep.join(
                [str(PACKAGE_DIR), str(PYTHON_DIR), str(HERE), env.get("PYTHONPATH", "")]
            ),
            "AUTOGEN_USE_DOCKER": "False",
        }
    )
    env.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
    env.pop("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", None)
    result = run([sys.executable, str(HERE / "_contract_app.py"), mode], env=env, stdin=None, timeout=180)
    stdout = result.stdout.decode("utf-8", "replace")
    stderr = result.stderr.decode("utf-8", "replace")
    assert not result.timed_out, "contract app timed out\nSTDERR:\n" + stderr[-4000:]
    assert result.returncode == 0, "contract app failed\nSTDERR:\n" + stderr[-4000:]
    lines = [line for line in stdout.splitlines() if line.startswith("CONTRACT_RESULT ")]
    assert lines, "no CONTRACT_RESULT line\nSTDOUT:\n" + stdout[-2000:]
    return json.loads(lines[-1][len("CONTRACT_RESULT ") :])


def _by_name(spans: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for span in spans:
        grouped[span["name"]].append(span)
    return grouped


def test_ag2_classic_contract_content_off_round_trip():
    with Receiver() as receiver:
        app = _run_app(receiver, "off")
        spans = receiver.spans()

    assert app["flushed"] is True
    # One provider: setup() never touched the global provider and added no exporter.
    assert app["global_provider_before"] == app["global_provider_after"] == "ProxyTracerProvider"
    assert app["processors"] == ["AG2ClassicSpanProcessor", "BatchSpanProcessor"]
    assert app["llm_requests"] >= 4

    assert spans, "receiver got no spans"
    names = _by_name(spans)
    for expected in (
        "conversation user",
        "invoke_agent assistant",
        "invoke_agent user",
        "chat gpt-4o-mini",
        "execute_tool get_weather",
        "execute_tool broken_tool",
        "speaker_selection",
        "conversation writer",
        "conversation checking_agent",
    ):
        assert expected in names, "missing span {0!r}; got {1}".format(expected, sorted(names))

    # Span kind: upstream ag2.span.type preserved, gen_ai.span.kind added.
    expected_kind = {
        "chat gpt-4o-mini": ("llm", "LLM"),
        "invoke_agent assistant": ("agent", "AGENT"),
        "invoke_agent user": ("agent", "AGENT"),
        "execute_tool get_weather": ("tool", "TOOL"),
        "conversation user": ("conversation", "CHAIN"),
        "speaker_selection": ("speaker_selection", "CHAIN"),
    }
    for name, (span_type, kind) in expected_kind.items():
        for span in names[name]:
            attrs = _flatten(span)
            assert attrs["ag2.span.type"] == span_type, name
            assert attrs["gen_ai.span.kind"] == kind, name

    # Model, provider, usage and cost on LLM spans (the one rejected call is
    # asserted below).
    for span in names["chat gpt-4o-mini"]:
        if span.get("status", {}).get("code") == "STATUS_CODE_ERROR":
            continue
        attrs = _flatten(span)
        assert attrs["gen_ai.operation.name"] == "chat"
        assert attrs["gen_ai.provider.name"] == "openai"
        assert attrs["gen_ai.request.model"] == "gpt-4o-mini"
        assert attrs["gen_ai.response.model"] == "gpt-4o-mini"
        assert attrs["gen_ai.usage.input_tokens"] == 11
        assert attrs["gen_ai.usage.output_tokens"] == 7
        assert attrs["gen_ai.usage.total_tokens"] == 18
        assert attrs["gen_ai.cost.total"] > 0
        assert "gen_ai.agent.name" in attrs

    # Tool name and call id.
    tool = _flatten(names["execute_tool get_weather"][0])
    assert tool["gen_ai.tool.name"] == "get_weather"
    assert tool["gen_ai.tool.type"] == "function"
    assert tool["gen_ai.tool.call.id"].startswith("call_fake_")

    # Session: outermost conversation only.
    conversation = _flatten(names["conversation user"][0])
    assert conversation["gen_ai.conversation.id"] == app["chat_id"]
    assert conversation["session.id"] == app["chat_id"]
    group_root = _flatten(names["conversation writer"][0])
    assert group_root["session.id"] == app["group_chat_id"]
    for nested in names["conversation checking_agent"]:
        nested_attrs = _flatten(nested)
        assert "gen_ai.conversation.id" in nested_attrs
        assert "session.id" not in nested_attrs

    # AC-07: a tool that raises is caught upstream; status is promoted to ERROR.
    broken = names["execute_tool broken_tool"][0]
    assert _flatten(broken)["error.type"] == "ExecutionError"
    assert broken["status"].get("code") == "STATUS_CODE_ERROR"
    assert names["execute_tool get_weather"][0].get("status", {}).get("code") != "STATUS_CODE_ERROR"

    # A model call that raises: upstream sets error.type and re-raises
    # (llm_wrapper.py:97-101). Status stays ERROR through Future AGI's
    # exporting processor, which turns only UNSET into OK.
    assert app["llm_error"] == "BadRequestError"
    assert app["llm_failed_requests"] == 1
    llm_spans = [s for s in spans if _flatten(s).get("gen_ai.span.kind") == "LLM"]
    failed_llm = [s for s in llm_spans if s.get("status", {}).get("code") == "STATUS_CODE_ERROR"]
    assert len(failed_llm) == 1
    failed_attrs = _flatten(failed_llm[0])
    assert failed_attrs["error.type"] == "BadRequestError"
    assert failed_attrs["ag2.span.type"] == "llm"
    for key in ("gen_ai.usage.input_tokens", "gen_ai.usage.total_tokens", "gen_ai.cost.total"):
        assert key not in failed_attrs, key
    ok_llm = [s for s in llm_spans if s not in failed_llm]
    for span in ok_llm:
        assert span.get("status", {}).get("code") != "STATUS_CODE_ERROR", span["name"]

    # AC-04: every span of the group chat run shares one trace id.
    group_trace = names["conversation writer"][0]["traceId"]
    for name in ("speaker_selection", "conversation checking_agent", "invoke_agent critic"):
        for span in names[name]:
            assert span["traceId"] == group_trace, name
    assert names["conversation user"][0]["traceId"] != group_trace
    assert len(base64.b64decode(group_trace)) == 16

    # AC-06: content is absent when capture is off.
    for span in spans:
        attrs = _flatten(span)
        for key in CONTENT_KEYS:
            assert key not in attrs, "{0} leaked on {1}".format(key, span["name"])
        blob = json.dumps(attrs)
        assert SECRET_PROMPT not in blob, span["name"]
        assert TOOL_SECRET_CITY not in blob, span["name"]

    # Usage and cost live on LLM spans only. fi-collector promotes token and
    # cost keys on any span and Observe sums them over a trace, so an
    # aggregate on a conversation/agent span would count every call twice.
    for span in spans:
        attrs = _flatten(span)
        if attrs.get("gen_ai.span.kind") == "LLM":
            continue
        leaked = [
            key
            for key in attrs
            if key.startswith("gen_ai.usage.") or key in ("gen_ai.cost.total", "llm.cost.total")
        ]
        assert not leaked, "usage/cost keys {0} on non-LLM span {1}".format(leaked, span["name"])
    # Each successful model call is one LLM span with 11 + 7 = 18 tokens; the
    # failed call carries none.
    assert len(ok_llm) == app["llm_requests"]
    per_trace: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    for span in spans:
        tokens = _flatten(span).get("gen_ai.usage.total_tokens", 0)
        per_trace[span["traceId"]][0] += tokens
    for span in ok_llm:
        per_trace[span["traceId"]][1] += 1
    for trace_id, (total, calls) in per_trace.items():
        assert total == calls * 18, (trace_id, total, calls)
    assert sum(total for total, _calls in per_trace.values()) == app["llm_requests"] * 18


def test_ag2_classic_contract_content_on_round_trip():
    with Receiver() as receiver:
        _run_app(receiver, "on")
        spans = receiver.spans()

    names = _by_name(spans)
    conversation = _flatten(names["conversation user"][0])
    assert SECRET_PROMPT in conversation["gen_ai.input.messages"]
    assert SECRET_PROMPT in conversation["input.value"]
    assert conversation["input.mime_type"] == "application/json"

    tool = _flatten(names["execute_tool get_weather"][0])
    assert json.loads(tool["input.value"]) == {"city": TOOL_SECRET_CITY}
    assert tool["output.value"] == "sunny in {0}".format(TOOL_SECRET_CITY)

    # capture_content=True is forwarded to upstream instrument_llm_wrapper(capture_messages=True).
    llm = _flatten(names["chat gpt-4o-mini"][0])
    assert "gen_ai.input.messages" in llm
    assert "gen_ai.output.messages" in llm
