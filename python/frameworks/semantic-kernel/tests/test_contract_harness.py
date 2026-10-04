"""Shared-harness contract test for traceAI-semantic-kernel.

Runs ``_contract_app.py`` in a subprocess with the real ``semantic-kernel``,
the real OpenTelemetry SDK and Future AGI's real OTLP/HTTP exporter from
``fi_instrumentation.register()``, pointed at a loopback harness ``Receiver``
through ``FI_BASE_URL`` (exporter path ``/tracer/v1/traces``). The model is a
loopback fake OpenAI-compatible server; placeholder keys only, no vendor call.

Journeys (in the app): one plain chat completion, one kernel prompt function
that makes the model call one tool, one ``ChatCompletionAgent`` invocation
inside ``using_session``, one streaming chat completion, two rejected model
calls (plain and streaming) and one tool that raises.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
PACKAGE_DIR = HERE.parent
PYTHON_DIR = PACKAGE_DIR.parent.parent
sys.path.insert(0, str(PYTHON_DIR / "tests"))
sys.path.insert(0, str(HERE))

from harness import Receiver, run  # noqa: E402

from _fake_openai import COMPLETION_TOKENS, PROMPT_TOKENS  # noqa: E402
from _scenarios import (  # noqa: E402
    API_KEY,
    PROJECT_NAME,
    SECRET_KEY,
    SECRET_PROMPT,
    SESSION,
    TOOL_SECRET_CITY,
)

CONTENT_KEYS = (
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
    "input.value",
    "output.value",
)
PROMOTED_INPUT_KEYS = ("llm.token_count.prompt", "gen_ai.usage.input_tokens", "llm.usage.prompt_tokens")
PROMOTED_KEYS = PROMOTED_INPUT_KEYS + (
    "llm.token_count.completion",
    "gen_ai.usage.output_tokens",
    "llm.usage.completion_tokens",
    "llm.token_count.total",
    "gen_ai.usage.total_tokens",
    "llm.usage.total_tokens",
    "gen_ai.cost.total",
    "llm.cost.total",
)
ERROR = "STATUS_CODE_ERROR"


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


def _hex(b64: str) -> str:
    return base64.b64decode(b64).hex() if b64 else ""


def _env(receiver_origin: str) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SEMANTICKERNEL_")}
    env.update(
        {
            "FI_BASE_URL": receiver_origin,
            "FI_API_KEY": API_KEY,
            "FI_SECRET_KEY": SECRET_KEY,
            "PYTHONPATH": os.pathsep.join([str(PACKAGE_DIR), str(PYTHON_DIR), str(HERE), env.get("PYTHONPATH", "")]),
        }
    )
    for key in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "FI_PROJECT_NAME"):
        env.pop(key, None)
    return env


def _run_app(origin: str, mode: str) -> Dict[str, Any]:
    result = run([sys.executable, str(HERE / "_contract_app.py"), mode], env=_env(origin), stdin=None, timeout=240)
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


def _children(spans: List[Dict[str, Any]], parent: Dict[str, Any]) -> List[Dict[str, Any]]:
    kids = [s for s in spans if s.get("parentSpanId") and s["parentSpanId"] == parent["spanId"]]
    return sorted(kids, key=lambda s: int(s["startTimeUnixNano"]))


def _assert_tool_loop(spans, root, tool_name):
    """root -> AutoFunctionInvocationLoop -> [chat (tool_calls), execute_tool, chat (stop)]."""
    loops = _children(spans, root)
    assert [s["name"] for s in loops] == ["AutoFunctionInvocationLoop"], [s["name"] for s in loops]
    steps = _children(spans, loops[0])
    assert [s["name"] for s in steps] == ["chat gpt-4o-mini", tool_name, "chat gpt-4o-mini"], [
        s["name"] for s in steps
    ]
    assert _flatten(steps[0])["gen_ai.response.finish_reason"] == "FinishReason.TOOL_CALLS"
    assert _flatten(steps[2])["gen_ai.response.finish_reason"] == "FinishReason.STOP"
    assert {s["traceId"] for s in [root, loops[0]] + steps} == {root["traceId"]}
    return loops[0], steps


def test_semantic_kernel_contract_sensitive_off_round_trip():
    with Receiver() as receiver:
        app = _run_app(receiver.origin, "off")
        spans = receiver.spans()
        requests = receiver.requests()

    # Journeys completed and the exporter flushed.
    assert app["flushed"] is True
    assert app["chat_reply"] == "Hello from the fake model."
    assert app["prompt_reply"] == app["agent_reply"] == "The tool says it is sunny."
    assert app["stream_reply"] == "Hello from the fake model."
    assert app["model_errors"] == ["ServiceResponseException", "ServiceResponseException"]
    assert app["llm_requests"] == 10

    # AC-01: two instrument() calls, one processor, ahead of the exporter; global provider untouched.
    assert app["processors"] == ["SemanticKernelSpanProcessor", "BatchSpanProcessor"]
    assert app["global_provider_before"] == app["global_provider_after"] == "ProxyTracerProvider"

    # Transport: collector path, auth headers, resource.
    assert requests, "receiver got no export requests"
    for request in requests:
        assert request["path"] == "/tracer/v1/traces"
        assert request["headers"]["x-api-key"] == API_KEY
        assert request["headers"]["x-secret-key"] == SECRET_KEY
        for resource in request["resource_attributes"]:
            assert resource["project_name"] == PROJECT_NAME
            assert resource["project_type"] == "observe"

    assert spans, "receiver got no spans"
    names = _by_name(spans)
    for expected, count in {
        "chat gpt-4o-mini": 7,
        "chat gpt-4o-mini-stream": 1,
        "chat broken-model": 2,
        "execute_tool Prompts-ask_weather": 1,
        "execute_tool Prompts-ask_broken": 1,
        "execute_tool Weather-get_weather": 2,
        "execute_tool Broken-explode": 1,
        "AutoFunctionInvocationLoop": 3,
        "invoke_agent Assistant": 1,
    }.items():
        assert len(names[expected]) == count, (expected, len(names[expected]), sorted(names))

    # AC-03: kinds. fi.span.kind (read first by fi-collector) and gen_ai.span.kind agree.
    expected_kind = {
        "chat gpt-4o-mini": "LLM",
        "chat gpt-4o-mini-stream": "LLM",
        "chat broken-model": "LLM",
        "invoke_agent Assistant": "AGENT",
        "execute_tool Weather-get_weather": "TOOL",
        "execute_tool Broken-explode": "TOOL",
        "execute_tool Prompts-ask_weather": "CHAIN",
        "execute_tool Prompts-ask_broken": "CHAIN",
        "AutoFunctionInvocationLoop": "CHAIN",
    }
    for name, kind in expected_kind.items():
        for span in names[name]:
            attrs = _flatten(span)
            assert attrs["fi.span.kind"] == kind, name
            assert attrs["gen_ai.span.kind"] == kind, name

    # AC-04: parent ids follow Semantic Kernel's call order.
    plain_chat = [s for s in names["chat gpt-4o-mini"] if not s.get("parentSpanId")]
    assert len(plain_chat) == 1
    prompt_fn = names["execute_tool Prompts-ask_weather"][0]
    assert not prompt_fn.get("parentSpanId")
    _assert_tool_loop(spans, prompt_fn, "execute_tool Weather-get_weather")
    agent = names["invoke_agent Assistant"][0]
    assert not agent.get("parentSpanId")
    _, agent_steps = _assert_tool_loop(spans, agent, "execute_tool Weather-get_weather")
    broken_fn = names["execute_tool Prompts-ask_broken"][0]
    _assert_tool_loop(spans, broken_fn, "execute_tool Broken-explode")
    assert len({prompt_fn["traceId"], agent["traceId"], broken_fn["traceId"], plain_chat[0]["traceId"]}) == 4
    assert len(_hex(agent["traceId"])) == 32

    # AC-05: model, provider (alias of gen_ai.system), tokens on every successful model call.
    for name in ("chat gpt-4o-mini", "chat gpt-4o-mini-stream"):
        for span in names[name]:
            attrs = _flatten(span)
            assert attrs["gen_ai.operation.name"] == "chat"
            assert attrs["gen_ai.request.model"] == name.split(" ", 1)[1]
            assert attrs["gen_ai.system"] == "openai"
            assert attrs["gen_ai.provider.name"] == "openai"
            assert attrs["gen_ai.usage.input_tokens"] == PROMPT_TOKENS
            assert attrs["gen_ai.usage.output_tokens"] == COMPLETION_TOKENS
            assert attrs["gen_ai.usage.total_tokens"] == PROMPT_TOKENS + COMPLETION_TOKENS

    # Promoted token/cost keys appear only on model-call spans, and each trace's
    # promoted input-token sum equals its model calls (Observe sums every span).
    by_trace: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for span in spans:
        by_trace[span["traceId"]].append(span)
        attrs = _flatten(span)
        if attrs.get("gen_ai.span.kind") != "LLM":
            assert not set(PROMOTED_KEYS) & set(attrs), span["name"]
    for trace_spans in by_trace.values():
        promoted = sum(_flatten(s).get(k, 0) for s in trace_spans for k in PROMOTED_INPUT_KEYS)
        model_calls = [s for s in trace_spans if _flatten(s).get("gen_ai.span.kind") == "LLM" and s["status"].get("code") != ERROR]
        assert promoted == PROMPT_TOKENS * len(model_calls)

    # AC-02: every gen_ai.* key Semantic Kernel emitted survives the processor unchanged.
    raw = app["raw"]
    assert len(raw) == len(spans)
    for span in spans:
        before = raw[_hex(span["spanId"])]
        after = _flatten(span)
        for key, value in before.items():
            if key.startswith("gen_ai."):
                assert after.get(key) == value, (span["name"], key)

    # Session: Semantic Kernel 1.44.1 emits no conversation/thread id, so none is invented;
    # the app-set using_session() value reaches every native span of the agent run.
    for span in spans:
        attrs = _flatten(span)
        if span["traceId"] == agent["traceId"]:
            assert attrs["session.id"] == SESSION, span["name"]
        else:
            assert "session.id" not in attrs, span["name"]
            assert "gen_ai.conversation.id" not in attrs, span["name"]
    assert app["agent_thread_id"] not in json.dumps([_flatten(s) for s in spans])

    # AC-06: streaming and error spans are closed with a status.
    stream = names["chat gpt-4o-mini-stream"][0]
    assert stream["status"].get("code") == "STATUS_CODE_OK"
    assert int(stream["endTimeUnixNano"]) >= int(stream["startTimeUnixNano"])
    for span in names["chat broken-model"]:
        attrs = _flatten(span)
        assert span["status"]["code"] == ERROR
        assert "ServiceResponseException" in attrs["error.type"]
        assert "gen_ai.usage.input_tokens" not in attrs
    explode = names["execute_tool Broken-explode"][0]
    assert explode["status"]["code"] == ERROR
    assert _flatten(explode)["error.type"] == "RuntimeError"
    assert app["broken_tool_reply"] == "The tool says it is sunny."  # the kernel run still finished
    for span in spans:
        if span["name"] not in ("chat broken-model", "execute_tool Broken-explode"):
            assert span["status"].get("code") != ERROR, span["name"]

    # AC-07: no message bodies, tool arguments or tool results when sensitive is off.
    for span in spans:
        attrs = _flatten(span)
        for key in CONTENT_KEYS:
            assert key not in attrs, "{0} leaked on {1}".format(key, span["name"])
        blob = json.dumps(attrs)
        assert SECRET_PROMPT not in blob, span["name"]
        assert TOOL_SECRET_CITY not in blob, span["name"]
    # Agent span carries tool definitions (schemas), which are not message content.
    assert "Weather-get_weather" in _flatten(agent)["gen_ai.tool.definitions"]
    assert agent_steps[1]["name"] == "execute_tool Weather-get_weather"


def test_semantic_kernel_contract_sensitive_on_control():
    """Control run: with sensitive=True the markers do flow, so the off-run absence is real."""
    with Receiver() as receiver:
        app = _run_app(receiver.origin, "on")
        spans = receiver.spans()

    assert app["flushed"] is True
    names = _by_name(spans)
    agent = _flatten(names["invoke_agent Assistant"][0])
    assert SECRET_PROMPT in agent["gen_ai.input.messages"]
    assert SECRET_PROMPT in agent["input.value"]
    assert agent["input.mime_type"] == "application/json"
    assert "The tool says it is sunny." in agent["output.value"]

    for span in names["execute_tool Weather-get_weather"]:
        tool = _flatten(span)
        assert json.loads(tool["gen_ai.tool.call.arguments"]) == {"city": TOOL_SECRET_CITY}
        assert json.loads(tool["input.value"]) == {"city": TOOL_SECRET_CITY}
        assert tool["output.value"] == "sunny in {0}".format(TOOL_SECRET_CITY)

    # Gap, documented: Semantic Kernel writes model-call message bodies to Python
    # logging (model_diagnostics/decorators.py), not to the span, so LLM spans
    # carry no input/output even when sensitive is on.
    for span in names["chat gpt-4o-mini"]:
        llm = _flatten(span)
        assert "input.value" not in llm and "output.value" not in llm
        assert SECRET_PROMPT not in json.dumps(llm)


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_collector_down_never_breaks_the_kernel():
    app = _run_app("http://127.0.0.1:{0}".format(_closed_port()), "deadcollector")
    assert app["chat_reply"] == "Hello from the fake model."
    assert app["prompt_reply"] == app["agent_reply"] == "The tool says it is sunny."
    assert app["stream_reply"] == "Hello from the fake model."
    assert app["processors"] == ["SemanticKernelSpanProcessor", "BatchSpanProcessor"]


def test_example_runs_against_the_receiver():
    with Receiver() as receiver:
        env = _env(receiver.origin)
        env["FI_PROJECT_NAME"] = "semantic-kernel-example"
        result = run([sys.executable, str(PACKAGE_DIR / "examples" / "basic_agent.py")], env=env, stdin=None, timeout=240)
        spans = receiver.spans()
        requests = receiver.requests()
    stderr = result.stderr.decode("utf-8", "replace")
    assert result.returncode == 0, stderr[-4000:]
    assert "sunny, 21 C in Paris" in result.stdout.decode("utf-8")
    kinds = {s["name"]: _flatten(s).get("gen_ai.span.kind") for s in spans}
    assert kinds["invoke_agent WeatherAgent"] == "AGENT"
    assert kinds["chat gpt-4o-mini"] == "LLM"
    assert kinds["execute_tool Weather-get_weather"] == "TOOL"
    assert all(_flatten(s)["session.id"] == "example-session-1" for s in spans)
    assert requests[0]["resource_attributes"][0]["project_name"] == "semantic-kernel-example"
