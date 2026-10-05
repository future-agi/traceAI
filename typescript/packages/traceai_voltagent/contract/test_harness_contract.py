"""Shared-harness contract tests for @traceai/voltagent (TH-8239).

Each test starts the shared loopback Receiver (python/tests/harness), runs
`node contract/run_fixture.mjs` through `harness.run`, and reads what the real
@traceai/fi-core OTLP/HTTP exporter posted to `{origin}/tracer/v1/traces`.
The fixture drives a real @voltagent/core agent with the AI SDK's
MockLanguageModelV3 (no vendor call, no network beyond 127.0.0.1) and imports
the BUILT package (dist/esm through package.json exports).

Run from the repo root:
  PYTHONPATH="python/tests" uv run --no-project --python 3.11 --with pytest \
    --with protobuf --with opentelemetry-proto \
    pytest typescript/packages/traceai_voltagent/contract -q -p no:cacheprovider \
    --noconftest -o addopts=''

Set TRACEAI_NODE_MATRIX=/path/to/node20:/path/to/node22 to run every journey on
several Node binaries.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
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
NODES = [p for p in os.environ.get("TRACEAI_NODE_MATRIX", "").split(os.pathsep) if p] or [NODE]
PNPM = shutil.which("pnpm") or "pnpm"
VOLTAGENT_DIR = PKG_DIR / "node_modules" / "@voltagent" / "core"

PROJECT = "th8239-harness-contract"
PLACEHOLDER_API_KEY = "contract-api-key-PLACEHOLDER"
PLACEHOLDER_SECRET_KEY = "contract-secret-key-PLACEHOLDER"
MARKERS = (
    "SECRET_PROMPT_MARKER",
    "SECRET_INSTRUCTIONS_MARKER",
    "SECRET_TOOL_ARG_MARKER",
    "SECRET_TOOL_RESULT_MARKER",
    "SECRET_ANSWER_MARKER",
    "SECRET_SUBTASK_MARKER",
)
CONTENT_KEYS = ("input", "output", "input.value", "output.value", "llm.messages", "agent.instructions",
                "agent.messages", "agent.messages.ui", "agent.stateSnapshot")
PROMOTED_EXACT = {"gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.usage.total_tokens",
                  "gen_ai.cost.total", "llm.cost.total", "gen_ai.cost.input", "gen_ai.cost.output",
                  "llm.cost.prompt", "llm.cost.completion"}
PROMOTED_PREFIXES = ("llm.token_count.", "llm.usage.")
INPUT_TOKEN_KEYS = ("gen_ai.usage.input_tokens", "llm.usage.prompt_tokens", "llm.token_count.prompt")


def _check(result: subprocess.CompletedProcess, what: str) -> None:
    assert not getattr(result, "timed_out", False), f"{what} timed out\n{result.stderr.decode()[-4000:]}"
    assert result.returncode == 0, (
        f"{what} exited {result.returncode}\nstdout:\n{result.stdout.decode()[-4000:]}"
        f"\nstderr:\n{result.stderr.decode()[-4000:]}"
    )


def _base_env() -> Dict[str, str]:
    return {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "NO_PROXY": "127.0.0.1,localhost"}


@pytest.fixture(scope="session")
def built_package() -> Path:
    result = run([PNPM, "--dir", str(PKG_DIR), "run", "build"], env=_base_env(), stdin=None, timeout=300)
    _check(result, "pnpm run build")
    assert (PKG_DIR / "dist" / "src" / "index.js").is_file()
    assert (PKG_DIR / "dist" / "esm" / "index.js").is_file()
    return PKG_DIR


def _run_fixture(node: str, base_url: str, journey: str, extra_env: Dict[str, str] | None = None,
                 timeout: float = 120) -> Dict[str, Any]:
    env = _base_env()
    env.update({
        "FI_BASE_URL": base_url,  # fi-core appends /tracer/v1/traces
        "FI_API_KEY": PLACEHOLDER_API_KEY,
        "FI_SECRET_KEY": PLACEHOLDER_SECRET_KEY,
        "FI_PROJECT_NAME": PROJECT,
        "JOURNEY": journey,
    })
    env.update(extra_env or {})
    result = run([node, str(PKG_DIR / "contract" / "run_fixture.mjs")], env=env, stdin=None, timeout=timeout)
    _check(result, f"{node} run_fixture.mjs ({journey})")
    lines = [line for line in result.stdout.decode().splitlines() if line.startswith("RESULT_JSON:")]
    assert len(lines) == 1, result.stdout.decode()[-4000:]
    return json.loads(lines[0][len("RESULT_JSON:"):])


def _value(value: Dict[str, Any]) -> Any:
    for key in ("stringValue", "boolValue", "doubleValue"):
        if key in value:
            return value[key]
    if "intValue" in value:
        return int(value["intValue"])
    if "arrayValue" in value:
        return [_value(v) for v in value["arrayValue"].get("values", [])]
    return value


def _attrs(span: Dict[str, Any]) -> Dict[str, Any]:
    return {item["key"]: _value(item["value"]) for item in span.get("attributes", [])}


def _status(span: Dict[str, Any]) -> int:
    code = span.get("status", {}).get("code", 0)
    names = {"STATUS_CODE_UNSET": 0, "STATUS_CODE_OK": 1, "STATUS_CODE_ERROR": 2}
    return names[code] if isinstance(code, str) else int(code)


def _kind(span: Dict[str, Any]) -> str:
    kind = span.get("kind", "SPAN_KIND_INTERNAL")
    return kind if isinstance(kind, str) else {1: "SPAN_KIND_INTERNAL", 3: "SPAN_KIND_CLIENT"}.get(kind, str(kind))


def _one(spans: List[Dict[str, Any]], name: str) -> Dict[str, Any]:
    matches = [span for span in spans if span["name"] == name]
    assert len(matches) == 1, f"expected one {name}, got {sorted(s['name'] for s in spans)}"
    return matches[0]


def _is_promoted(key: str) -> bool:
    return key in PROMOTED_EXACT or key.startswith(PROMOTED_PREFIXES)


def _promoted_input_tokens(spans: List[Dict[str, Any]]) -> int:
    """What Observe adds up: one promoted input-token value per span, over the whole trace."""
    total = 0
    for span in spans:
        a = _attrs(span)
        values = {a[key] for key in INPUT_TOKEN_KEYS if key in a}
        assert len(values) <= 1, f"conflicting promoted input tokens on {span['name']}: {values}"
        total += values.pop() if values else 0
    return total


def _assert_collector_contract(requests: List[Dict[str, Any]]) -> None:
    assert requests, "exporter sent nothing"
    for request in requests:
        assert request["path"] == "/tracer/v1/traces"
        headers = request["headers"]
        assert headers["x-api-key"] == PLACEHOLDER_API_KEY
        assert headers["x-secret-key"] == PLACEHOLDER_SECRET_KEY
        assert "authorization" not in headers
        assert request["resource_attributes"]
        for resource in request["resource_attributes"]:
            assert resource["project_name"] == PROJECT
            assert resource["project_type"] == "observe"


@pytest.mark.parametrize("node", NODES)
def test_tool_journey_reaches_collector_path_mapped_with_content_off(built_package: Path, node: str) -> None:
    with Receiver() as receiver:
        output = _run_fixture(node, receiver.origin, "tools")
        spans = receiver.spans()
        requests = receiver.requests()

    assert output["text"] == "SECRET_ANSWER_MARKER"
    _assert_collector_contract(requests)

    # AC-06: the other processor in the same array and VoltAgent's own storage saw every span,
    # and Future AGI received exactly the spans VoltAgent emitted.
    recorded_ids = sorted(span["spanId"] for span in output["recorded"])
    assert len(spans) == len(output["recorded"]) == output["storedCount"]
    assert sorted(_hex_span_id(span["spanId"]) for span in spans) == recorded_ids

    names = sorted(span["name"] for span in spans)
    for expected in ("assistant", "llm:generateText", "tool.execution:get_weather", "tool.execution:explode",
                     "memory.read"):
        assert expected in names, names
    assert len({span["traceId"] for span in spans}) == 1

    root = _one(spans, "assistant")
    llm = _one(spans, "llm:generateText")
    weather = _one(spans, "tool.execution:get_weather")
    failing = _one(spans, "tool.execution:explode")
    r, l, w, f = _attrs(root), _attrs(llm), _attrs(weather), _attrs(failing)

    # Kinds: fi.span.kind (collector reads it first) and gen_ai.span.kind.
    assert (r["fi.span.kind"], r["gen_ai.span.kind"]) == ("AGENT", "AGENT")
    assert (l["fi.span.kind"], l["gen_ai.span.kind"]) == ("LLM", "LLM")
    assert (w["fi.span.kind"], f["fi.span.kind"]) == ("TOOL", "TOOL")
    for span in spans:
        a = _attrs(span)
        if span["name"] == "memory.read":
            assert a["fi.span.kind"] == "RETRIEVER"
        if span["name"] in ("memory.write", "memory.steps.write"):
            assert a["fi.span.kind"] == "CHAIN"
        assert "fi.span.kind" in a and "gen_ai.span.kind" in a, span["name"]
    assert _kind(llm) == "SPAN_KIND_CLIENT"

    # Parenting.
    assert not root.get("parentSpanId")
    for child in (llm, weather, failing):
        assert child["parentSpanId"] == root["spanId"]

    # Model, provider, tool, session, user.
    assert l["gen_ai.request.model"] == "mockai/mock-model-1" and l["gen_ai.provider.name"] == "mockai"
    assert r["gen_ai.request.model"] == "mockai/mock-model-1"
    assert (w["gen_ai.tool.name"], w["gen_ai.tool.call.id"]) == ("get_weather", "call-weather")
    for span in spans:
        a = _attrs(span)
        assert a["session.id"] == "conv-123" and a["user.id"] == "user-9", span["name"]

    # Tokens: promoted keys on the model-call span only; the trace sums to the mock model's calls.
    calls = output["modelCalls"]
    assert len(calls) == 2
    assert _promoted_input_tokens(spans) == sum(call["input"] for call in calls) == 24
    assert l["gen_ai.usage.input_tokens"] == 24 and l["gen_ai.usage.output_tokens"] == 12
    assert l["gen_ai.usage.total_tokens"] == 36
    assert l["voltagent.llm.last_step_usage.prompt_tokens"] == 13
    for span in spans:
        if _attrs(span).get("fi.span.kind") != "LLM":
            assert not [key for key in _attrs(span) if _is_promoted(key)], span["name"]
    assert r["voltagent.usage.input_tokens"] == 24 and r["usage.prompt_tokens"] == 24

    # Errors.
    assert _status(failing) == 2
    assert f["error.message"] == "tool exploded"
    assert "exception" in [event["name"] for event in failing.get("events", [])]
    assert _status(weather) == 1 and _status(root) == 1

    # No content by default, no credentials anywhere on spans.
    blob = json.dumps(spans)
    for marker in MARKERS:
        assert marker not in blob, marker
    for span in spans:
        for key in CONTENT_KEYS:
            assert key not in _attrs(span), (span["name"], key)
    assert PLACEHOLDER_API_KEY not in blob and PLACEHOLDER_SECRET_KEY not in blob


def _hex_span_id(span_id: str) -> str:
    """OTLP/JSON from protobuf MessageToDict encodes bytes ids as base64."""
    import base64
    import binascii

    if len(span_id) == 16 and all(c in "0123456789abcdef" for c in span_id):
        return span_id
    return binascii.hexlify(base64.b64decode(span_id)).decode()


def test_content_is_exported_only_after_opt_in(built_package: Path) -> None:
    with Receiver() as receiver:
        _run_fixture(NODE, receiver.origin, "tools", {"CAPTURE_CONTENT": "true"})
        spans = receiver.spans()
    root = _attrs(_one(spans, "assistant"))
    weather = _attrs(_one(spans, "tool.execution:get_weather"))
    assert root["input.value"] == "SECRET_PROMPT_MARKER"
    assert root["output.value"] == "SECRET_ANSWER_MARKER"
    assert "SECRET_TOOL_ARG_MARKER" in weather["input.value"]
    assert "SECRET_TOOL_RESULT_MARKER" in weather["output.value"]
    assert PLACEHOLDER_API_KEY not in json.dumps(spans)


def test_subagent_is_one_trace_under_the_supervisor(built_package: Path) -> None:
    with Receiver() as receiver:
        output = _run_fixture(NODE, receiver.origin, "subagent")
        spans = receiver.spans()
        requests = receiver.requests()
    _assert_collector_contract(requests)
    assert len({span["traceId"] for span in spans}) == 1
    root = _one(spans, "supervisor")
    delegate = _one(spans, "tool.execution:delegate_task")
    subagents = [span for span in spans if span["name"].startswith("subagent:")]
    assert len(subagents) == 1
    assert delegate["parentSpanId"] == root["spanId"]
    assert subagents[0]["parentSpanId"] == delegate["spanId"]
    assert _attrs(subagents[0])["fi.span.kind"] == "AGENT"
    assert _status(subagents[0]) == 1
    calls = output["modelCalls"]
    assert len(calls) == 3  # supervisor x2, subagent x1
    assert _promoted_input_tokens(spans) == sum(call["input"] for call in calls)
    assert len(spans) == len(output["recorded"])
    assert "SECRET_SUBTASK_MARKER" not in json.dumps(spans)


def test_burst_then_flush_drops_nothing(built_package: Path) -> None:
    """AC-05 style: 20 invocations, one forceFlush before the process exits, zero dropped."""
    with Receiver() as receiver:
        output = _run_fixture(NODE, receiver.origin, "burst", timeout=300)
        spans = receiver.spans()
    assert output["runs"] == 20
    assert len(output["traceIds"]) == 20
    assert len(spans) == len(output["recorded"]) == output["storedCount"]
    assert len({span["traceId"] for span in spans}) == 20
    assert _promoted_input_tokens(spans) == sum(call["input"] for call in output["modelCalls"]) == 20 * 24


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_collector_down_never_breaks_the_agent(built_package: Path) -> None:
    output = _run_fixture(NODE, f"http://127.0.0.1:{_closed_port()}", "tools", timeout=120)
    assert output["text"] == "SECRET_ANSWER_MARKER"
    assert len(output["recorded"]) > 0  # VoltAgent's own processors still ran


def test_packed_tarball_has_no_voltagent_sources(built_package: Path, tmp_path: Path) -> None:
    result = run([PNPM, "--dir", str(PKG_DIR), "pack", "--pack-destination", str(tmp_path)],
                 env=_base_env(), stdin=None, timeout=300)
    _check(result, "pnpm pack")
    tarballs = list(tmp_path.glob("*.tgz"))
    assert len(tarballs) == 1, tarballs
    voltagent_files = {p.name for p in VOLTAGENT_DIR.resolve().rglob("*") if p.is_file()}
    assert "index.mjs" in voltagent_files, "installed @voltagent/core not found; cannot compare"
    with tarfile.open(tarballs[0]) as tar:
        members = [m for m in tar.getmembers() if m.isfile()]
        manifest = json.loads(tar.extractfile("package/package.json").read())  # type: ignore[union-attr]
        for member in members:
            relative = member.name[len("package/"):]
            assert relative.split("/")[0] in {"dist", "package.json", "README.md", "LICENSE"}, relative
            assert "node_modules" not in member.name and "__tests__" not in member.name
            assert "voltagent/core" not in member.name.lower()
            data = tar.extractfile(member).read()  # type: ignore[union-attr]
            assert b"Copyright (c) 2025 VoltAgent" not in data, relative
            assert b"class VoltAgentObservability" not in data, relative
    assert manifest["peerDependencies"] == {"@voltagent/core": "^2.11.0"}
    assert "@voltagent/core" not in manifest.get("dependencies", {})
    assert not manifest.get("bundledDependencies") and not manifest.get("bundleDependencies")
    assert manifest["license"] == "Apache-2.0"
    assert all(not v.startswith("workspace:") for v in manifest.get("dependencies", {}).values())


@pytest.mark.parametrize("node", NODES)
def test_esm_and_cjs_entry_points_import(built_package: Path, tmp_path: Path, node: str) -> None:
    """require() and import() of the package name resolve through package.json exports."""
    scope = tmp_path / "node_modules" / "@traceai"
    scope.mkdir(parents=True)
    (scope / "voltagent").symlink_to(PKG_DIR, target_is_directory=True)
    check = (
        "const names=['FIVoltAgentSpanProcessor','mapVoltAgentAttributes','resolveSpanKind'];"
        "function ok(m,kind){for(const n of names){if(!(n in m))throw new Error(kind+' missing '+n)}}"
    )
    cjs = run([node, "-e", check + "ok(require('@traceai/voltagent'),'cjs');console.log(process.version,'cjs ok')"],
              env={**_base_env(), "NODE_PATH": str(tmp_path / "node_modules")}, stdin=None, timeout=60)
    script = tmp_path / "check.mjs"
    script.write_text(check + "ok(await import('@traceai/voltagent'),'esm');console.log(process.version,'esm ok');")
    esm = run([node, str(script)], env=_base_env(), stdin=None, timeout=60)
    _check(cjs, f"{node} require")
    _check(esm, f"{node} import")
    assert b"cjs ok" in cjs.stdout and b"esm ok" in esm.stdout
