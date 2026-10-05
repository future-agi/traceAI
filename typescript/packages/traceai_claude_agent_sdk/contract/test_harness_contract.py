"""Shared-harness contract tests for @traceai/claude-agent-sdk (TH-8235).

The package is TypeScript, so each test starts the shared loopback Receiver,
runs `node contract/run_fixture.cjs` through `harness.run`, and reads the spans
the real fi-core OTLP/HTTP exporter posted to `{origin}/tracer/v1/traces`.
The Claude Agent SDK is replaced by a fake query() over SDK-typed fixtures:
no Claude Code CLI, no Anthropic call, no network beyond 127.0.0.1.

The Node OTLP/HTTP exporter streams each body with `Transfer-Encoding: chunked`
and no Content-Length. The shared Receiver decodes that framing and records one
entry per export (path, lower-cased headers, resource attributes), so headers
and `project_name` are asserted through `receiver.requests()`.
src/__tests__/collector.test.ts (jest) asserts them as well.

Run from the repo root:
  PYTHONPATH=python/tests uv run --no-project --python 3.11 --with pytest \
    --with opentelemetry-exporter-otlp-proto-http \
    pytest typescript/packages/traceai_claude_agent_sdk/contract -q -p no:cacheprovider --noconftest -o addopts=''
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

PKG_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PKG_DIR.parents[2]
sys.path.insert(0, str(REPO_ROOT / "python" / "tests"))

from harness import Receiver, run  # noqa: E402

NODE = os.environ.get("NODE_BINARY") or shutil.which("node") or "node"
PNPM = shutil.which("pnpm") or "pnpm"
TSC = PKG_DIR / "node_modules" / ".bin" / "tsc"
SDK_DIR = PKG_DIR / "node_modules" / "@anthropic-ai" / "claude-agent-sdk"

SESSION_ID = "11111111-2222-4333-8444-555555555555"
MODEL = "claude-sonnet-4-5"
CONTENT_MARKERS = (
    "SECRET_PROMPT_MARKER",
    "SECRET_TOOL_INPUT_MARKER",
    "SECRET_TOOL_OUTPUT_MARKER",
    "SECRET_ASSISTANT_TEXT_MARKER",
    "SECRET_SUBAGENT_PROMPT_MARKER",
)
PLACEHOLDER_API_KEY = "contract-api-key-PLACEHOLDER"
PLACEHOLDER_SECRET_KEY = "contract-secret-key-PLACEHOLDER"


def _check(result: subprocess.CompletedProcess, what: str) -> None:
    assert not getattr(result, "timed_out", False), f"{what} timed out\n{result.stderr.decode()}"
    assert result.returncode == 0, (
        f"{what} exited {result.returncode}\nstdout:\n{result.stdout.decode()}\nstderr:\n{result.stderr.decode()}"
    )


@pytest.fixture(scope="session")
def built_package() -> Path:
    """Build dist/ (CJS + ESM) from the current sources."""
    result = run([PNPM, "--dir", str(PKG_DIR), "run", "build"], env=_base_env(), stdin=None, timeout=300)
    _check(result, "pnpm run build")
    assert (PKG_DIR / "dist" / "src" / "index.js").is_file()
    assert (PKG_DIR / "dist" / "esm" / "index.js").is_file()
    return PKG_DIR


@pytest.fixture(scope="session")
def fixtures_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Compile the SDK-typed fixtures; a type error against the pinned sdk.d.ts fails here."""
    out = tmp_path_factory.mktemp("fixtures")
    result = run(
        [str(TSC), "-p", str(PKG_DIR / "contract" / "tsconfig.fixtures.json"), "--outDir", str(out)],
        env=_base_env(),
        stdin=None,
        timeout=180,
    )
    _check(result, "tsc fixtures")
    assert (out / "messages.js").is_file() and (out / "fakeQuery.js").is_file()
    return out


def _base_env() -> Dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "NO_PROXY": "127.0.0.1,localhost",
    }


def _run_journey(receiver: Receiver, fixtures_dir: Path, journey: str, extra_env: Dict[str, str] | None = None) -> Dict[str, Any]:
    env = _base_env()
    env.update(
        {
            "FI_BASE_URL": receiver.origin,  # fi-core appends /tracer/v1/traces
            "FI_API_KEY": PLACEHOLDER_API_KEY,
            "FI_SECRET_KEY": PLACEHOLDER_SECRET_KEY,
            "FI_PROJECT_NAME": "th8235-harness-contract",
            "CONTRACT_FIXTURES_DIR": str(fixtures_dir),
            "JOURNEY": journey,
        }
    )
    env.update(extra_env or {})
    result = run([NODE, str(PKG_DIR / "contract" / "run_fixture.cjs")], env=env, stdin=None, timeout=60)
    _check(result, f"node run_fixture.cjs ({journey})")
    return json.loads(result.stdout.decode("utf-8"))


def _value(value: Dict[str, Any]) -> Any:
    for key in ("stringValue", "boolValue", "doubleValue"):
        if key in value:
            return value[key]
    if "intValue" in value:
        return int(value["intValue"])
    return value


def _attrs(span: Dict[str, Any]) -> Dict[str, Any]:
    return {item["key"]: _value(item["value"]) for item in span.get("attributes", [])}


def _status_code(span: Dict[str, Any]) -> int:
    code = span.get("status", {}).get("code", 0)
    names = {"STATUS_CODE_UNSET": 0, "STATUS_CODE_OK": 1, "STATUS_CODE_ERROR": 2}
    return names[code] if isinstance(code, str) else int(code)


def _by_name(spans: List[Dict[str, Any]], name: str) -> List[Dict[str, Any]]:
    return [span for span in spans if span["name"] == name]


def _one(spans: List[Dict[str, Any]], name: str) -> Dict[str, Any]:
    matches = _by_name(spans, name)
    assert len(matches) == 1, f"expected one {name}, got {[s['name'] for s in spans]}"
    return matches[0]


def test_conversation_turn_tool_reach_collector_path_with_content_off(built_package: Path, fixtures_dir: Path) -> None:
    with Receiver() as receiver:
        output = _run_journey(receiver, fixtures_dir, "simple")
        spans = receiver.spans()
        requests = receiver.requests()

    # AC-08: messages are yielded unchanged and in order.
    assert output["yielded"] == output["expected"]

    # Collector contract (the Receiver drops headers and resource attributes).
    assert requests, "exporter sent nothing"
    for request in requests:
        assert request["path"] == "/tracer/v1/traces"
        headers = request["headers"]
        assert headers["x-api-key"] == PLACEHOLDER_API_KEY
        assert headers["x-secret-key"] == PLACEHOLDER_SECRET_KEY
        assert "authorization" not in headers
        assert request["resource_attributes"]
        for resource in request["resource_attributes"]:
            assert resource["project_name"] == "th8235-harness-contract"
            assert resource["project_type"] == "observe"

    assert sorted(span["name"] for span in spans) == sorted(
        ["claude_agent.conversation", "claude_agent.assistant_turn", "claude_agent.assistant_turn", "tool.Read"]
    )
    assert len({span["traceId"] for span in spans}) == 1

    conversation = _one(spans, "claude_agent.conversation")
    tool = _one(spans, "tool.Read")
    turns = _by_name(spans, "claude_agent.assistant_turn")
    c, t = _attrs(conversation), _attrs(tool)

    # Span kinds: Python parity string + the Future AGI kind keys.
    assert (c["claude_agent.span_kind"], c["gen_ai.span.kind"], c["fi.span.kind"]) == ("conversation", "CHAIN", "CHAIN")
    for turn in turns:
        a = _attrs(turn)
        assert (a["claude_agent.span_kind"], a["gen_ai.span.kind"], a["fi.span.kind"]) == ("assistant_turn", "LLM", "LLM")
        assert a["gen_ai.request.model"] == MODEL
        assert a["gen_ai.provider.name"] == "anthropic"
        assert turn["parentSpanId"] == conversation["spanId"]
    assert (t["claude_agent.span_kind"], t["gen_ai.span.kind"], t["fi.span.kind"]) == ("tool_execution", "TOOL", "TOOL")
    issuing = [turn for turn in turns if _attrs(turn)["claude_agent.message.has_tool_use"] is True]
    assert len(issuing) == 1 and tool["parentSpanId"] == issuing[0]["spanId"]
    assert t["gen_ai.tool.name"] == "Read" and t["claude_agent.tool.source"] == "builtin"

    # Model, tokens, cost, session.
    assert c["gen_ai.request.model"] == MODEL and c["claude_agent.model"] == MODEL
    assert c["gen_ai.usage.input_tokens"] == 120
    assert c["gen_ai.usage.output_tokens"] == 45
    assert c["gen_ai.usage.total_tokens"] == 165
    assert c["gen_ai.cost.total"] == pytest.approx(0.0123)
    assert c["claude_agent.cost.total_usd"] == pytest.approx(0.0123)
    for span in spans:
        assert _attrs(span)["session.id"] == SESSION_ID
        assert _status_code(span) == 1

    # Content capture is off by default.
    blob = json.dumps(spans)
    for marker in CONTENT_MARKERS:
        assert marker not in blob
    for span in spans:
        for key in ("input.value", "output.value", "claude_agent.prompt", "claude_agent.tool.input",
                    "claude_agent.tool.output", "claude_agent.message.content"):
            assert key not in _attrs(span)
    assert PLACEHOLDER_API_KEY not in blob and PLACEHOLDER_SECRET_KEY not in blob


def test_subagent_and_mcp_spans_reach_the_receiver(built_package: Path, fixtures_dir: Path) -> None:
    with Receiver() as receiver:
        _run_journey(receiver, fixtures_dir, "subagent")
        subagent_spans = receiver.spans()
        receiver.clear()
        _run_journey(receiver, fixtures_dir, "mcp")
        mcp_spans = receiver.spans()

    agent_tool = _one(subagent_spans, "tool.Agent")
    subagent = _one(subagent_spans, "claude_agent.subagent.code-reviewer")
    grep = _one(subagent_spans, "tool.Grep")
    s = _attrs(subagent)
    assert (s["claude_agent.span_kind"], s["gen_ai.span.kind"]) == ("subagent", "AGENT")
    assert subagent["parentSpanId"] == agent_tool["spanId"]
    grep_turn = [span for span in subagent_spans if span["spanId"] == grep["parentSpanId"]][0]
    assert grep_turn["name"] == "claude_agent.assistant_turn"
    assert grep_turn["parentSpanId"] == subagent["spanId"]

    mcp_tool = _one(mcp_spans, "tool.mcp__docs__search")
    m = _attrs(mcp_tool)
    assert (m["claude_agent.span_kind"], m["gen_ai.span.kind"], m["claude_agent.mcp.server_name"]) == ("mcp_tool", "TOOL", "docs")
    assert _status_code(mcp_tool) == 2
    conversation = _attrs(_one(mcp_spans, "claude_agent.conversation"))
    assert conversation["claude_agent.error.type"] == "error_max_turns"


def test_content_is_exported_only_after_opt_in(built_package: Path, fixtures_dir: Path) -> None:
    with Receiver() as receiver:
        _run_journey(receiver, fixtures_dir, "simple", {"FI_HIDE_INPUTS": "false", "FI_HIDE_OUTPUTS": "false"})
        spans = receiver.spans()
    conversation = _attrs(_one(spans, "claude_agent.conversation"))
    tool = _attrs(_one(spans, "tool.Read"))
    assert "SECRET_PROMPT_MARKER" in conversation["input.value"]
    assert "SECRET_ASSISTANT_TEXT_MARKER" in conversation["output.value"]
    assert "SECRET_TOOL_INPUT_MARKER" in tool["claude_agent.tool.input"]
    assert "SECRET_TOOL_OUTPUT_MARKER" in tool["claude_agent.tool.output"]


def _sdk_platform_binary_installed() -> bool:
    store = SDK_DIR.resolve().parent if SDK_DIR.exists() else None
    return bool(store and list(store.glob("claude-agent-sdk-*")))


requires_real_sdk = pytest.mark.skipif(
    not _sdk_platform_binary_installed(), reason="claude-agent-sdk platform binary not installed"
)


def _run_real_sdk(scenario: str, project: str, extra_env: Dict[str, str] | None = None):
    """Run contract/run_real_sdk.mjs with SCENARIO against a fresh Receiver.

    Returns (stdout JSON, received spans, export request records).
    """
    env = _base_env()
    env.update(
        {
            "FI_API_KEY": PLACEHOLDER_API_KEY,
            "FI_SECRET_KEY": PLACEHOLDER_SECRET_KEY,
            "FI_PROJECT_NAME": project,
            "SCENARIO": scenario,
        }
    )
    if os.environ.get("CLAUDE_AGENT_SDK_ENTRY"):
        # Run the same contract against another installed SDK build (older 0.3.x matrix).
        env["CLAUDE_AGENT_SDK_ENTRY"] = os.environ["CLAUDE_AGENT_SDK_ENTRY"]
    env.update(extra_env or {})
    with Receiver() as receiver:
        env["FI_BASE_URL"] = receiver.origin
        result = run([NODE, str(PKG_DIR / "contract" / "run_real_sdk.mjs")], env=env, stdin=None, timeout=180)
        _check(result, f"node run_real_sdk.mjs ({scenario})")
        spans = receiver.spans()
        exported = receiver.requests()
    return json.loads(result.stdout.decode("utf-8")), spans, exported


def _assert_exported_with_both_keys(exported: List[Dict[str, Any]], project: str) -> None:
    assert exported, "exporter sent nothing"
    for request in exported:
        assert request["path"] == "/tracer/v1/traces"
        assert request["headers"]["x-api-key"] == PLACEHOLDER_API_KEY
        assert request["headers"]["x-secret-key"] == PLACEHOLDER_SECRET_KEY
        assert request["resource_attributes"]
        for resource in request["resource_attributes"]:
            assert (resource["project_name"], resource["project_type"]) == (project, "observe")


@requires_real_sdk
def test_real_sdk_close_and_async_dispose_end_every_span_cancelled(built_package: Path) -> None:
    """R3: Query.close() (the SDK abort path) and Symbol.asyncDispose bypass next/return/throw.

    Each query is stopped right after the CLI streamed the Read tool_use, so the
    conversation, turn and tool spans are open. All of them must still be exported,
    ended ERROR with claude_agent.cancelled=true.
    """
    output, spans, exported = _run_real_sdk("close", "th8235-real-sdk-close")
    closed, disposed = output["queries"]
    for messages in (closed, disposed):
        assert not [m for m in messages if m["type"] == "result"], "query finished before it was stopped"

    traces: Dict[str, List[Dict[str, Any]]] = {}
    for span in spans:
        traces.setdefault(span["traceId"], []).append(span)
    assert len(traces) == 2, sorted(span["name"] for span in spans)
    for trace_spans in traces.values():
        names = sorted(span["name"] for span in trace_spans)
        assert names == sorted(["claude_agent.conversation", "claude_agent.assistant_turn", "tool.Read"]), names
        for span in trace_spans:
            assert _status_code(span) == 2, span["name"]
            assert _attrs(span)["claude_agent.cancelled"] is True, span["name"]
    _assert_exported_with_both_keys(exported, "th8235-real-sdk-close")


# Keys fi-collector promotes into hot columns on ANY span; Observe sums them per
# trace and per session.id, so they must equal new spend exactly once.
PROMOTED_PREFIXES = ("gen_ai.usage.", "llm.token_count.")
PROMOTED_KEYS = ("gen_ai.cost.total", "llm.cost.total")


def _promoted(attributes: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in attributes.items() if k.startswith(PROMOTED_PREFIXES) or k in PROMOTED_KEYS}


def _results(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [m for m in messages if m["type"] == "result"]


def _init_session(messages: List[Dict[str, Any]]) -> str:
    return [m for m in messages if m["type"] == "system" and m.get("subtype") == "init"][0]["session_id"]


def _cumulative(result: Dict[str, Any]) -> Dict[str, Any]:
    """The running totals a result carries: total_cost_usd and modelUsage summed over models."""
    usage = result["modelUsage"].values()
    return {
        "cost": result["total_cost_usd"],
        "input": sum(u["inputTokens"] for u in usage),
        "output": sum(u["outputTokens"] for u in usage),
    }


def _conversations_in_query_order(spans: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    conversations = _by_name(spans, "claude_agent.conversation")
    return sorted(conversations, key=lambda span: int(span["startTimeUnixNano"]))


def _assert_only_conversations_carry_usage(spans: List[Dict[str, Any]]) -> None:
    for span in spans:
        if span["name"] != "claude_agent.conversation":
            assert not _promoted(_attrs(span)), (span["name"], _promoted(_attrs(span)))


def _assert_promoted_sum_equals(conversations: List[Dict[str, Any]], final: Dict[str, Any]) -> None:
    attrs = [_attrs(c) for c in conversations]
    assert sum(a["gen_ai.cost.total"] for a in attrs) == pytest.approx(final["cost"], rel=1e-9)
    assert sum(a["gen_ai.usage.input_tokens"] for a in attrs) == final["input"]
    assert sum(a["gen_ai.usage.output_tokens"] for a in attrs) == final["output"]
    assert sum(a["gen_ai.usage.total_tokens"] for a in attrs) == final["input"] + final["output"]


@requires_real_sdk
def test_real_sdk_resume_counts_cost_and_tokens_once(built_package: Path) -> None:
    """R1: a resumed session's first result already carries the earlier turns (sdk.d.ts:5679).

    Two query() calls, the second with options.resume. The promoted cost/tokens summed
    over both traces must equal the final cumulative totals, not exceed them.
    """
    output, spans, exported = _run_real_sdk("resume", "th8235-real-sdk-resume")
    first, second = output["queries"]
    (r1,), (r2,) = _results(first), _results(second)
    session_id = _init_session(first)
    assert _init_session(second) == session_id
    final = _cumulative(r2)
    # What the reviewer saw: the resumed result is cumulative over both queries.
    assert final["cost"] > _cumulative(r1)["cost"] > 0 and final["input"] > _cumulative(r1)["input"]

    conversations = _conversations_in_query_order(spans)
    assert len(conversations) == 2 and len({c["traceId"] for c in conversations}) == 2
    _assert_promoted_sum_equals(conversations, final)
    c1, c2 = (_attrs(c) for c in conversations)
    assert c1["gen_ai.cost.total"] == pytest.approx(r1["total_cost_usd"])
    assert (c2["session.id"], c2["claude_agent.session.is_new"], c2["claude_agent.session.is_resumed"]) == (
        session_id, False, True,
    )
    assert c2["claude_agent.cumulative.cost_usd"] == pytest.approx(final["cost"])
    assert (c2["claude_agent.cumulative.input_tokens"], c2["claude_agent.cumulative.output_tokens"]) == (
        final["input"], final["output"],
    )
    assert c2["claude_agent.usage.baseline_unknown"] is False
    _assert_only_conversations_carry_usage(spans)
    _assert_exported_with_both_keys(exported, "th8235-real-sdk-resume")


@requires_real_sdk
def test_real_sdk_continue_and_fork_count_cost_and_tokens_once(built_package: Path) -> None:
    """R1: options.continue keeps the session id and its totals; a fork gets a new id but
    starts from the parent's saved totals. Sum of promoted keys == the fork's final totals."""
    output, spans, _ = _run_real_sdk("continue_fork", "th8235-real-sdk-continue-fork")
    first, continued, forked = output["queries"]
    parent = _init_session(first)
    assert _init_session(continued) == parent
    fork_id = _init_session(forked)
    assert fork_id != parent
    final = _cumulative(_results(forked)[-1])

    conversations = _conversations_in_query_order(spans)
    assert len(conversations) == 3
    _assert_promoted_sum_equals(conversations, final)
    _, c_continue, c_fork = (_attrs(c) for c in conversations)
    assert (c_continue["claude_agent.session.is_new"], c_continue["claude_agent.session.is_resumed"]) == (False, True)
    assert c_continue["claude_agent.is_resumed"] is True
    assert (c_fork["session.id"], c_fork["claude_agent.session.fork_from"]) == (fork_id, parent)
    assert c_fork["claude_agent.session.is_new"] is True
    _assert_only_conversations_carry_usage(spans)


@requires_real_sdk
def test_real_sdk_resume_after_process_restart_marks_baseline_unknown(built_package: Path, tmp_path: Path) -> None:
    """R1: a resume with no baseline in this process (restart) must not put promoted keys:
    the result is cumulative and the earlier share is unknown. Cumulative values go on
    claude_agent.cumulative.* and claude_agent.usage.baseline_unknown=true."""
    workdir = tmp_path / "home"
    first_out, first_spans, _ = _run_real_sdk("restart", "th8235-real-sdk-restart", {"WORKDIR": str(workdir), "PHASE": "first"})
    session_id = _init_session(first_out["queries"][0])
    second_out, second_spans, _ = _run_real_sdk(
        "restart", "th8235-real-sdk-restart",
        {"WORKDIR": str(workdir), "PHASE": "second", "RESUME_SESSION_ID": session_id},
    )
    (r1,), (r2,) = _results(first_out["queries"][0]), _results(second_out["queries"][0])
    final = _cumulative(r2)
    assert final["cost"] > r1["total_cost_usd"]

    c1 = _attrs(_one(first_spans, "claude_agent.conversation"))
    c2 = _attrs(_one(second_spans, "claude_agent.conversation"))
    assert c1["gen_ai.cost.total"] == pytest.approx(r1["total_cost_usd"])  # a new session: baseline 0
    assert c1["claude_agent.usage.baseline_unknown"] is False
    assert _promoted(c2) == {}, _promoted(c2)
    assert "claude_agent.cost.total_usd" not in c2
    assert c2["claude_agent.usage.baseline_unknown"] is True
    assert c2["claude_agent.cumulative.cost_usd"] == pytest.approx(final["cost"])
    assert (c2["claude_agent.cumulative.input_tokens"], c2["claude_agent.cumulative.output_tokens"]) == (
        final["input"], final["output"],
    )
    assert c2["session.id"] == session_id and c2["claude_agent.session.is_new"] is False
    _assert_only_conversations_carry_usage(first_spans + second_spans)


@requires_real_sdk
def test_real_sdk_streaming_input_counts_tokens_once(built_package: Path) -> None:
    """R2 + R5: one query() with two user turns yields two results. result.usage is per turn
    (sdk.d.ts:5683); modelUsage and total_cost_usd are running totals (sdk.d.ts:5679, 5687).
    The conversation span must carry the latest running totals, and turn 2 must start
    after turn 1 ended."""
    output, spans, _ = _run_real_sdk("streaming", "th8235-real-sdk-streaming")
    (messages,) = output["queries"]
    results = _results(messages)
    assert len(results) == 2
    final = _cumulative(results[-1])
    # What the reviewer saw: usage on the last result is one turn only.
    assert results[-1]["usage"]["input_tokens"] < final["input"]

    conversation = _one(spans, "claude_agent.conversation")
    _assert_promoted_sum_equals([conversation], final)
    turns = sorted(_by_name(spans, "claude_agent.assistant_turn"), key=lambda s: int(s["startTimeUnixNano"]))
    assert len(turns) == 2
    assert int(turns[1]["startTimeUnixNano"]) >= int(turns[0]["endTimeUnixNano"])
    _assert_only_conversations_carry_usage(spans)


@requires_real_sdk
def test_real_sdk_query_through_anthropic_base_url_mock(built_package: Path) -> None:
    """The real SDK query() and bundled CLI, wrapped, against a loopback Messages API mock.

    Mechanism from the ADR: Options.env.ANTHROPIC_BASE_URL. No Anthropic call: placeholder
    key, dead-port HTTPS_PROXY, temp HOME. Spans go through fi-core to the shared Receiver.
    """
    output, spans, exported = _run_real_sdk("tool", "th8235-real-sdk-contract")

    # The CLI called the mock (host root + /v1/messages), streaming, with the placeholder key.
    mock_requests = output["requests"]
    assert len(mock_requests) == 2, mock_requests
    for request in mock_requests:
        assert request["method"] == "POST" and request["url"].startswith("/v1/messages"), request
        assert request["stream"] is True
    assert [r["hasToolResult"] for r in mock_requests] == [False, True]

    messages = output["messages"]
    init = [m for m in messages if m["type"] == "system" and m.get("subtype") == "init"]
    results = [m for m in messages if m["type"] == "result"]
    assert len(init) == 1 and len(results) == 1 and results[0]["subtype"] == "success"
    session_id = init[0]["session_id"]

    assert sorted(span["name"] for span in spans) == sorted(
        ["claude_agent.conversation", "claude_agent.assistant_turn", "claude_agent.assistant_turn", "tool.Read"]
    )
    conversation = _one(spans, "claude_agent.conversation")
    tool = _one(spans, "tool.Read")
    turns = _by_name(spans, "claude_agent.assistant_turn")
    issuing = [t for t in turns if _attrs(t)["claude_agent.message.has_tool_use"] is True]
    assert len(issuing) == 1 and tool["parentSpanId"] == issuing[0]["spanId"]
    for turn in turns:
        a = _attrs(turn)
        assert turn["parentSpanId"] == conversation["spanId"]
        assert (a["gen_ai.span.kind"], a["gen_ai.request.model"], a["gen_ai.provider.name"]) == ("LLM", "claude-sonnet-4-5", "custom")
    c = _attrs(conversation)
    assert (c["claude_agent.span_kind"], c["gen_ai.span.kind"]) == ("conversation", "CHAIN")
    assert c["session.id"] == session_id and c["claude_agent.session.id"] == session_id
    assert c["gen_ai.usage.input_tokens"] > 0 and c["gen_ai.usage.output_tokens"] > 0
    assert "gen_ai.cost.total" in c and "claude_agent.cost.total_usd" in c
    t = _attrs(tool)
    assert (t["claude_agent.span_kind"], t["gen_ai.span.kind"], t["claude_agent.tool.source"]) == ("tool_execution", "TOOL", "builtin")
    assert t["claude_agent.tool.is_error"] is False
    for span in spans:
        assert _status_code(span) == 1

    blob = json.dumps(spans)
    for marker in ("SECRET_PROMPT_MARKER", "SECRET_TOOL_OUTPUT_MARKER", "SECRET_ASSISTANT_TEXT_MARKER"):
        assert marker not in blob
    assert PLACEHOLDER_API_KEY not in blob and "PLACEHOLDER-not-a-key" not in blob
    _assert_exported_with_both_keys(exported, "th8235-real-sdk-contract")


def _read_member(tar: tarfile.TarFile, member: Any) -> bytes:
    handle = tar.extractfile(member)
    assert handle is not None, member
    return handle.read()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sdk_file_hashes() -> Dict[str, str]:
    """Hash every file of the installed SDK and its platform binary package."""
    hashes: Dict[str, str] = {}
    roots = [SDK_DIR.resolve()]
    platform_root = SDK_DIR.resolve().parent  # node_modules/@anthropic-ai inside the pnpm store entry
    roots.extend(p.resolve() for p in platform_root.glob("claude-agent-sdk-*"))
    for root in roots:
        for file in root.rglob("*"):
            if file.is_file():
                hashes[_sha256(file.read_bytes())] = str(file)
    return hashes


NATIVE_MAGIC = (
    b"\x7fELF",  # ELF
    b"\xcf\xfa\xed\xfe",  # Mach-O 64-bit LE
    b"\xce\xfa\xed\xfe",  # Mach-O 32-bit LE
    b"\xca\xfe\xba\xbe",  # Mach-O fat
    b"MZ",  # PE
)


def test_packed_tarball_has_no_sdk_sources_or_native_binary(built_package: Path, tmp_path: Path) -> None:
    """AC-09: the SDK (Anthropic Commercial Terms) is a peer dependency, never bundled."""
    result = run(
        [PNPM, "--dir", str(PKG_DIR), "pack", "--pack-destination", str(tmp_path)],
        env=_base_env(),
        stdin=None,
        timeout=300,
    )
    _check(result, "pnpm pack")
    tarballs = list(tmp_path.glob("*.tgz"))
    assert len(tarballs) == 1, tarballs

    sdk_hashes = _sdk_file_hashes()
    assert len(sdk_hashes) > 10, "installed SDK not found; cannot compare"
    with tarfile.open(tarballs[0]) as tar:
        members = [m for m in tar.getmembers() if m.isfile()]
        names = [m.name for m in members]
        manifest = json.loads(_read_member(tar, "package/package.json"))
        for member in members:
            data = _read_member(tar, member)
            assert member.name.startswith("package/"), member.name
            relative = member.name[len("package/"):]
            assert relative.split("/")[0] in {"dist", "src", "package.json", "README.md", "LICENSE"}, relative
            assert "node_modules" not in member.name
            assert "__tests__" not in member.name
            assert not relative.endswith((".mjs", ".node", ".exe", ".wasm")), relative
            assert not relative.endswith(".tsbuildinfo"), f"build cache in tarball: {relative}"
            assert Path(relative).name not in {"sdk.mjs", "sdk.d.ts", "sdk-tools.d.ts", "bridge.mjs", "claude", "cli.js"}
            assert not data.startswith(NATIVE_MAGIC), f"native binary in tarball: {relative}"
            assert _sha256(data) not in sdk_hashes, f"{relative} is a copy of {sdk_hashes[_sha256(data)]}"
            assert b"Anthropic PBC. All rights reserved" not in data, relative

    assert manifest["peerDependencies"] == {"@anthropic-ai/claude-agent-sdk": "^0.3.142"}
    assert "@anthropic-ai/claude-agent-sdk" not in manifest.get("dependencies", {})
    assert not manifest.get("bundledDependencies") and not manifest.get("bundleDependencies")
    assert manifest["license"] == "Apache-2.0"
    # workspace: ranges must be rewritten to publishable versions by pnpm pack.
    assert all(not v.startswith("workspace:") for v in manifest.get("dependencies", {}).values())
    assert sum(1 for n in names if n.startswith("package/dist/")) > 0


@pytest.mark.parametrize("node", [p for p in os.environ.get("TRACEAI_NODE_MATRIX", "").split(os.pathsep) if p] or [NODE])
def test_esm_and_cjs_entry_points_import(built_package: Path, tmp_path: Path, node: str) -> None:
    """AC-09: require() and import() of the package name resolve through package.json exports."""
    scope = tmp_path / "node_modules" / "@traceai"
    scope.mkdir(parents=True)
    (scope / "claude-agent-sdk").symlink_to(PKG_DIR, target_is_directory=True)
    check = (
        "const names=['wrapQuery','shutdown','ClaudeAgentSDKInstrumentation','ClaudeAgentAttributes'];"
        "function ok(m,kind){for(const n of names){if(!(n in m))throw new Error(kind+' missing '+n)}}"
    )
    cjs = run(
        [node, "-e", check + "ok(require('@traceai/claude-agent-sdk'),'cjs');console.log(process.version,'cjs ok')"],
        env={**_base_env(), "NODE_PATH": str(tmp_path / "node_modules")}, stdin=None, timeout=60,
    )
    script = tmp_path / "check.mjs"
    script.write_text(check + "ok(await import('@traceai/claude-agent-sdk'),'esm');console.log(process.version,'esm ok');")
    esm = run([node, str(script)], env=_base_env(), stdin=None, timeout=60)
    _check(cjs, f"{node} require")
    _check(esm, f"{node} import")
    assert b"cjs ok" in cjs.stdout and b"esm ok" in esm.stdout
