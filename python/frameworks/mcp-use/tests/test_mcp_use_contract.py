"""Shared-harness contract test for traceAI-mcp-use (TH-8331, harness Receiver).

The real MCPAgent runs on the fake LLM against the fake MCP server over
stdio. Spans leave through the real ``fi_instrumentation.register()`` OTLP
exporter into ``harness.Receiver`` on 127.0.0.1. Every key is a
placeholder.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

import pytest
from _mcp_use_support import (
    AGENT,
    ANSWER,
    ARG,
    CONTENT_MARKERS,
    ENV_SECRET,
    ENV_SECRET_NAME,
    LLM_KEY,
    LLM_SPAN,
    MODEL,
    PROMPT,
    ChatFake,
    add_script,
    answer,
    mcp_client,
    tool_call,
)

pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))

from harness import Receiver, _flatten_attributes  # noqa: E402

from traceai_mcp_use import FutureAGICallback  # noqa: E402

FI_API_KEY = "placeholder-fi-api-key"
FI_SECRET_KEY = "placeholder-fi-secret-key"
PROJECT = "mcp-use-contract"


def _register(receiver: Receiver, monkeypatch) -> Any:
    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType

    monkeypatch.setenv("FI_BASE_URL", receiver.origin)
    monkeypatch.setenv("FI_API_KEY", FI_API_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET_KEY)
    monkeypatch.setenv(ENV_SECRET_NAME, ENV_SECRET)
    return register(project_type=ProjectType.OBSERVE, project_name=PROJECT, verbose=False)


def _journey(receiver: Receiver, monkeypatch, **options: Any) -> List[Dict[str, Any]]:
    """J1 (add, then answer) and a failing tool call, through register()."""
    from mcp_use import MCPAgent

    provider = _register(receiver, monkeypatch)
    handler = FutureAGICallback(tracer_provider=provider, **options)

    async def go() -> None:
        client = mcp_client("stdio")
        try:
            agent = MCPAgent(llm=ChatFake(script=add_script()), client=client, callbacks=[handler])
            assert await agent.run(PROMPT + " " + ENV_SECRET) == ANSWER
            failing = MCPAgent(
                llm=ChatFake(script=[tool_call("fail", {"reason": ARG}), answer()]),
                client=client,
                callbacks=[handler],
            )
            assert await failing.run(PROMPT) == ANSWER
        finally:
            await client.close_all_sessions()

    try:
        asyncio.run(go())
        assert provider.force_flush(timeout_millis=10_000)
    finally:
        provider.shutdown()
    return receiver.spans()


def _trees(spans: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Spans grouped by trace, agent span first, in start order."""
    traces: Dict[str, List[Dict[str, Any]]] = {}
    for span in sorted(spans, key=lambda span: int(span["startTimeUnixNano"])):
        traces.setdefault(span["traceId"], []).append(span)
    return sorted(
        (sorted(group, key=lambda span: span["name"] != AGENT) for group in traces.values()),
        key=lambda group: int(group[0]["startTimeUnixNano"]),
    )


def _attrs(span: Dict[str, Any]) -> Dict[str, Any]:
    return _flatten_attributes(span.get("attributes", []))


def test_agent_runs_reach_the_collector_contract(monkeypatch):
    with Receiver() as receiver:
        spans = _journey(receiver, monkeypatch)
        exports = receiver.requests()

    assert exports
    for export in exports:
        assert export["path"] == "/tracer/v1/traces"
        assert export["headers"]["x-api-key"] == FI_API_KEY
        assert export["headers"]["x-secret-key"] == FI_SECRET_KEY
        assert "authorization" not in export["headers"]
        for resource in export["resource_attributes"]:
            assert resource["project_name"] == PROJECT
            assert resource["project_type"] == "observe"

    j1, failed = _trees(spans)
    agent, *children = j1
    assert agent["name"] == AGENT
    assert not agent.get("parentSpanId")
    assert [child["name"] for child in children] == [LLM_SPAN, "execute_tool add", LLM_SPAN]
    for child in children:
        assert child["parentSpanId"] == agent["spanId"]
    kinds = [_attrs(span)["gen_ai.span.kind"] for span in j1]
    assert kinds == ["AGENT", "LLM", "TOOL", "LLM"]
    assert _attrs(children[0])["gen_ai.request.model"] == MODEL
    assert _attrs(children[1])["gen_ai.tool.name"] == "add"
    assert int(_attrs(agent)["mcp_use.agent.tool_call_count"]) == 1
    # register()'s processor also promotes UNSET to OK on export, so the
    # handler's own OK is pinned by the in-memory tests.
    assert all(span["status"]["code"] == "STATUS_CODE_OK" for span in j1)

    failed_agent, *failed_children = failed
    tool = next(span for span in failed_children if span["name"] == "execute_tool fail")
    assert tool["parentSpanId"] == failed_agent["spanId"]
    assert tool["status"]["code"] == "STATUS_CODE_ERROR"
    assert tool["status"]["message"] == "RuntimeError"
    assert [event["name"] for event in tool["events"]] == ["exception"]
    assert failed_agent["status"]["code"] == "STATUS_CODE_OK"
    assert int(_attrs(failed_agent)["mcp_use.agent.tool_error_count"]) == 1

    # One span per tool call: nothing else shares a tool span's name and parent.
    for tree in (j1, failed):
        tools = Counter((span["name"], span.get("parentSpanId")) for span in tree if span["name"].startswith("execute_tool"))
        assert set(tools.values()) == {1}


def test_no_key_or_content_is_exported_by_default(monkeypatch):
    with Receiver() as receiver:
        spans = _journey(receiver, monkeypatch)

    assert len(spans) == 8  # two runs: agent, LLM, tool, LLM each
    text = json.dumps(spans)
    for secret in (FI_API_KEY, FI_SECRET_KEY, LLM_KEY, ENV_SECRET) + CONTENT_MARKERS:
        assert secret not in text, secret
    for span in spans:
        for key in _attrs(span):
            assert not key.startswith(("input.", "output.", "gen_ai.input.", "gen_ai.output."))


def test_opt_in_capture_reaches_the_collector_without_keys(monkeypatch):
    # Control for the test above: the same journey with capture_content
    # carries the prompt, arguments, results and answer, still without keys.
    with Receiver() as receiver:
        spans = _journey(receiver, monkeypatch, capture_content=True)

    text = json.dumps(spans)
    for marker in ("PROMPT-MARKER", "ANSWER-MARKER", "SUM-RESULT-5", ARG, "tool failed because"):
        assert marker in text, marker
    for secret in (FI_API_KEY, FI_SECRET_KEY, LLM_KEY, ENV_SECRET):
        assert secret not in text, secret
    agent = _attrs(_trees(spans)[0][0])
    assert agent["input.value"] == PROMPT + " [redacted]"


def _two_turns(receiver: Receiver, monkeypatch, **options: Any) -> List[Dict[str, Any]]:
    """J1, then a second query on the same MCPAgent (memory on, the default)."""
    from mcp_use import MCPAgent

    provider = _register(receiver, monkeypatch)
    handler = FutureAGICallback(tracer_provider=provider, capture_content=True, **options)

    async def go() -> None:
        client = mcp_client("stdio")
        try:
            agent = MCPAgent(
                llm=ChatFake(script=add_script() + [answer("SECOND-ANSWER done")]),
                client=client,
                callbacks=[handler],
            )
            assert await agent.run(PROMPT) == ANSWER
            assert await agent.run("SECOND-PROMPT") == "SECOND-ANSWER done"
        finally:
            await client.close_all_sessions()

    try:
        asyncio.run(go())
        assert provider.force_flush(timeout_millis=10_000)
    finally:
        provider.shutdown()
    return receiver.spans()


@pytest.mark.parametrize("hide_outputs", [False, True])
def test_hide_outputs_keeps_the_first_turn_out_of_the_second_turn_export(monkeypatch, hide_outputs):
    from fi_instrumentation import TraceConfig

    with Receiver() as receiver:
        spans = _two_turns(receiver, monkeypatch, config=TraceConfig(hide_outputs=hide_outputs))

    _, second = _trees(spans)
    assert [span["name"] for span in second] == [AGENT, LLM_SPAN]
    assert _attrs(second[0])["input.value"] == "SECOND-PROMPT"
    text = json.dumps(second)
    for marker in ("SUM-RESULT-5", "ANSWER-MARKER"):
        # The control (hide_outputs off) shows the second turn replays them.
        assert (marker in text) is not hide_outputs, marker
