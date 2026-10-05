"""Shared-harness contract tests for @traceai/genkit (TH-8240).

The package is TypeScript. Each test starts the shared loopback Receiver
(python/tests/harness), runs `node contract/run_fixture.mjs`, and reads what the
real @traceai/fi-core OTLP/HTTP exporter posted to `{origin}/tracer/v1/traces`.
The fixture runs real genkit 1.42.0 flows on Genkit's own `mockModel`
(genkit/testing): no model vendor, no GCP, no network beyond 127.0.0.1.

A second loopback server stands in for Genkit's telemetry server (the Dev UI
trace store, POST /api/traces) so the tests can show Genkit's own exporter keeps
working next to FIGenkitSpanProcessor.

Run from the repo root:
  PYTHONPATH=python/tests uv run --no-project --python 3.11 --with pytest \\
    --with protobuf --with opentelemetry-proto \\
    pytest typescript/packages/traceai_genkit/contract -q -p no:cacheprovider --noconftest -o addopts=''

NODE_BINARY selects the node used for fixtures; TRACEAI_NODE_MATRIX (os.pathsep
separated node paths) widens the entry-point test.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

PKG_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PKG_DIR.parents[2]
sys.path.insert(0, str(REPO_ROOT / "python" / "tests"))

from harness import Receiver, run  # noqa: E402

NODE = os.environ.get("NODE_BINARY") or shutil.which("node") or "node"
PNPM = shutil.which("pnpm") or "pnpm"
FIXTURE = PKG_DIR / "contract" / "run_fixture.mjs"
INVENTORY = PKG_DIR / "contract" / "inventory.mjs"
GENKIT_DIR = PKG_DIR / "node_modules" / "genkit"

PROJECT = "th8240-harness-contract"
PLACEHOLDER_API_KEY = "contract-api-key-PLACEHOLDER"
PLACEHOLDER_SECRET_KEY = "contract-secret-key-PLACEHOLDER"
CONTENT_MARKERS = (
    "SECRET_PROMPT_MARKER",
    "SECRET_OUTPUT_MARKER",
    "SECRET_TOOL_OUTPUT_MARKER",
    "SECRET_CHUNK_MARKER",
    "SECRET_STRUCTURED_OUTPUT_MARKER",
)
REDACTED_NOTE = "[data redacted: captureContent is off]"
PROMOTED_EXACT = {
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.total_tokens",
    "gen_ai.cost.total",
    "llm.cost.total",
}
PROMOTED_PREFIXES = ("gen_ai.usage.", "llm.token_count.", "llm.usage.")
# Inventory at genkit 1.42.0 (contract/inventory.mjs): mockModel spans carry
# usage.inputTokens/outputTokens/totalTokens in genkit:output; only those are
# mapped. thoughtsTokens and cachedContentTokens exist in GenerationUsageSchema
# (@genkit-ai/ai src/model-types.ts:260-275) but no inventoried span carried
# them, so they are unavailable: the keys they would map to must be absent.
UNAVAILABLE_ON_FIXTURE = ("gen_ai.usage.output_tokens.reasoning", "gen_ai.usage.cache_read_tokens")


# --------------------------------------------------------------------------- helpers


def _check(result: subprocess.CompletedProcess, what: str) -> None:
    assert not getattr(result, "timed_out", False), f"{what} timed out\n{result.stderr.decode()}"
    assert result.returncode == 0, (
        f"{what} exited {result.returncode}\nstdout:\n{result.stdout.decode()}\nstderr:\n{result.stderr.decode()}"
    )


def _base_env() -> Dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }


def _fixture_env(receiver_origin: str, journey: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = _base_env()
    env.update(
        {
            "FI_BASE_URL": receiver_origin,  # fi-core appends /tracer/v1/traces
            "FI_API_KEY": PLACEHOLDER_API_KEY,
            "FI_SECRET_KEY": PLACEHOLDER_SECRET_KEY,
            "FI_PROJECT_NAME": PROJECT,
            "JOURNEY": journey,
        }
    )
    env.update(extra or {})
    return env


def _last_json_line(stdout: bytes) -> Dict[str, Any]:
    lines = [line for line in stdout.decode("utf-8").splitlines() if line.startswith("{")]
    assert lines, f"no JSON line on stdout: {stdout!r}"
    return json.loads(lines[-1])


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


def _is_promoted(key: str) -> bool:
    return key in PROMOTED_EXACT or key.startswith(PROMOTED_PREFIXES)


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


def _assert_no_content(spans: List[Dict[str, Any]]) -> None:
    blob = json.dumps(spans)
    for marker in CONTENT_MARKERS:
        assert marker not in blob, marker
    for span in spans:
        attrs = _attrs(span)
        for key in ("input.value", "output.value", "genkit:input", "genkit:output", "genkit:metadata:context"):
            assert key not in attrs, (span["name"], key)
    assert PLACEHOLDER_API_KEY not in blob and PLACEHOLDER_SECRET_KEY not in blob


class TelemetryServerStandIn:
    """Loopback stand-in for Genkit's telemetry server (`POST {GENKIT_TELEMETRY_SERVER}/api/traces`)."""

    def __init__(self) -> None:
        self.posts: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                try:
                    payload = json.loads(body.decode("utf-8")) if body else None
                except ValueError:
                    payload = None
                with owner._lock:
                    owner.posts.append({"path": self.path, "body": payload})
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, _format: str, *args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def traces(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [post["body"] for post in self.posts if post["path"] == "/api/traces" and post["body"]]

    def spans(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for trace in self.traces():
            out.extend(trace.get("spans", {}).values())
        return out

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def built_package() -> Path:
    """Build dist/ (CJS + ESM) from the current sources; the fixture imports the built package by name."""
    result = run([PNPM, "--dir", str(PKG_DIR), "run", "build"], env=_base_env(), stdin=None, timeout=300)
    _check(result, "pnpm run build")
    assert (PKG_DIR / "dist" / "src" / "index.js").is_file()
    assert (PKG_DIR / "dist" / "esm" / "index.js").is_file()
    return PKG_DIR


@pytest.fixture(scope="session")
def tool_run(built_package: Path) -> Dict[str, Any]:
    """One `qaFlow` run (prepare step, generate with a tool loop: 2 model calls, 1 tool) with a session id."""
    stand_in = TelemetryServerStandIn()
    try:
        with Receiver() as receiver:
            env = _fixture_env(
                receiver.origin, "tool", {"SESSION_ID": "sess-contract-1", "GENKIT_TELEMETRY_SERVER": stand_in.origin}
            )
            result = run([NODE, str(FIXTURE)], env=env, stdin=None, timeout=90)
            _check(result, "node run_fixture.mjs (tool)")
            spans = receiver.spans()
            requests = receiver.requests()
    finally:
        stand_in.close()
    return {"output": _last_json_line(result.stdout), "spans": spans, "requests": requests, "genkit_spans": stand_in.spans()}


# --------------------------------------------------------------------------- tests


def test_tool_flow_reaches_collector_path_with_kinds_parenting_model_and_tokens(tool_run: Dict[str, Any]) -> None:
    output, spans = tool_run["output"], tool_run["spans"]
    assert output["result"] == "final SECRET_OUTPUT_MARKER"
    assert output["flushError"] is None
    _assert_collector_contract(tool_run["requests"])

    assert sorted(s["name"] for s in spans) == sorted(
        ["qaFlow", "prepare", "generate", "generate", "contract/tool-model", "contract/tool-model", "lookup"]
    )
    assert len({s["traceId"] for s in spans}) == 1
    by_id = {s["spanId"]: s for s in spans}

    flow = _one(spans, "qaFlow")
    prepare = _one(spans, "prepare")
    tool = _one(spans, "lookup")
    models = _by_name(spans, "contract/tool-model")
    generates = _by_name(spans, "generate")

    # Parenting: flow is the root; prepare and the outer generate are its children.
    assert not flow.get("parentSpanId")
    assert prepare["parentSpanId"] == flow["spanId"]
    outer = [g for g in generates if g["parentSpanId"] == flow["spanId"]]
    inner = [g for g in generates if g["parentSpanId"] != flow["spanId"]]
    assert len(outer) == 1 and len(inner) == 1 and inner[0]["parentSpanId"] == outer[0]["spanId"]
    assert tool["parentSpanId"] == outer[0]["spanId"]
    assert sorted(by_id[m["parentSpanId"]]["spanId"] for m in models) == sorted([outer[0]["spanId"], inner[0]["spanId"]])

    # Kinds: fi.span.kind (read first by the collector) and gen_ai.span.kind.
    def kinds(span: Dict[str, Any]) -> tuple:
        a = _attrs(span)
        return a.get("fi.span.kind"), a.get("gen_ai.span.kind")

    assert kinds(flow) == ("CHAIN", "CHAIN")
    assert kinds(prepare) == ("CHAIN", "CHAIN")
    assert all(kinds(g) == ("CHAIN", "CHAIN") for g in generates)
    assert all(kinds(m) == ("LLM", "LLM") for m in models)
    assert kinds(tool) == ("TOOL", "TOOL")
    for span in spans:
        assert span["kind"] in (1, "SPAN_KIND_INTERNAL")
        assert _status_code(span) == 0  # Genkit leaves success spans UNSET

    # Model, tokens, finish reason: per model call, from genkit:name and genkit:output.usage.
    usage = sorted(
        (
            _attrs(m)["gen_ai.usage.input_tokens"],
            _attrs(m)["gen_ai.usage.output_tokens"],
            _attrs(m)["gen_ai.usage.total_tokens"],
        )
        for m in models
    )
    assert usage == [(11, 3, 14), (21, 4, 25)]
    for model in models:
        a = _attrs(model)
        assert a["gen_ai.request.model"] == "contract/tool-model"
        assert a["gen_ai.response.finish_reasons"] == ["stop"]
        for key in UNAVAILABLE_ON_FIXTURE:
            assert key not in a
    t = _attrs(tool)
    assert t["tool.name"] == "lookup" and t["gen_ai.tool.name"] == "lookup"

    # Session set by the app with fi-core setSession() around the flow call.
    for span in spans:
        assert _attrs(span)["session.id"] == "sess-contract-1"


def test_promoted_tokens_only_on_model_calls_and_trace_sum_equals_model_calls(tool_run: Dict[str, Any]) -> None:
    spans = tool_run["spans"]
    for span in spans:
        a = _attrs(span)
        promoted = [k for k in a if _is_promoted(k)]
        if a.get("fi.span.kind") == "LLM":
            assert "gen_ai.usage.input_tokens" in promoted
        else:
            assert promoted == [], (span["name"], promoted)
    total_input = sum(_attrs(s).get("gen_ai.usage.input_tokens", 0) for s in spans)
    total_output = sum(_attrs(s).get("gen_ai.usage.output_tokens", 0) for s in spans)
    total = sum(_attrs(s).get("gen_ai.usage.total_tokens", 0) for s in spans)
    # The mock model reported 11 + 21 input, 3 + 4 output, 14 + 25 total. The generate
    # spans' own output repeats the last turn's usage (21/4/25); it must not be summed.
    assert (total_input, total_output, total) == (32, 7, 39)


def test_content_is_off_by_default(tool_run: Dict[str, Any]) -> None:
    _assert_no_content(tool_run["spans"])


def test_genkit_own_trace_export_keeps_working_and_sees_unmodified_spans(tool_run: Dict[str, Any]) -> None:
    """AC-01 (prod mode): Genkit's TraceServerExporter still receives every span, content intact."""
    genkit_spans = tool_run["genkit_spans"]
    fi_spans = tool_run["spans"]
    assert {s["spanId"] for s in genkit_spans} == {s["spanId"] for s in fi_spans}
    flow = [s for s in genkit_spans if s["displayName"] == "qaFlow"][0]
    assert "SECRET_PROMPT_MARKER" in flow["attributes"]["genkit:input"]
    assert "fi.span.kind" not in flow["attributes"]  # our mapping went to a copy, not Genkit's span


def test_content_is_exported_only_after_opt_in(built_package: Path) -> None:
    with Receiver() as receiver:
        result = run([NODE, str(FIXTURE)], env=_fixture_env(receiver.origin, "tool", {"CAPTURE_CONTENT": "1"}), stdin=None, timeout=90)
        _check(result, "node run_fixture.mjs (tool, captureContent)")
        spans = receiver.spans()
    flow = _attrs(_one(spans, "qaFlow"))
    tool = _attrs(_one(spans, "lookup"))
    assert "SECRET_PROMPT_MARKER" in flow["input.value"]
    assert "SECRET_OUTPUT_MARKER" in flow["output.value"]
    assert flow["input.mime_type"] == "application/json"
    assert "SECRET_TOOL_OUTPUT_MARKER" in tool["output.value"]
    assert "genkit:metadata:context" not in flow
    blob = json.dumps(spans)
    assert PLACEHOLDER_API_KEY not in blob and PLACEHOLDER_SECRET_KEY not in blob


def test_stream_flow_has_one_closed_model_span(built_package: Path) -> None:
    """AC-05: flow.stream() (streamFlow) produces one model span, ended, with usage."""
    with Receiver() as receiver:
        result = run([NODE, str(FIXTURE)], env=_fixture_env(receiver.origin, "stream"), stdin=None, timeout=90)
        _check(result, "node run_fixture.mjs (stream)")
        spans = receiver.spans()
        _assert_collector_contract(receiver.requests())
    output = _last_json_line(result.stdout)
    assert output["chunks"] == ["one ", "two ", "SECRET_CHUNK_MARKER"]
    assert output["result"] == "one two SECRET_CHUNK_MARKER"

    models = [s for s in spans if _attrs(s).get("fi.span.kind") == "LLM"]
    assert len(models) == 1
    model = models[0]
    assert model["name"] == "contract/stream-model"
    assert int(model["endTimeUnixNano"]) >= int(model["startTimeUnixNano"]) > 0
    a = _attrs(model)
    assert (a["gen_ai.usage.input_tokens"], a["gen_ai.usage.output_tokens"], a["gen_ai.usage.total_tokens"]) == (9, 6, 15)
    flow = _one(spans, "streamFlow")
    assert _attrs(flow)["fi.span.kind"] == "CHAIN"
    generate = _one(spans, "generate")
    assert model["parentSpanId"] == generate["spanId"] and generate["parentSpanId"] == flow["spanId"]
    _assert_no_content(spans)


def test_throwing_action_marks_error_with_exception_event(built_package: Path) -> None:
    """AC-07: a tool throws; Genkit's span status is ERROR with an exception event, root included."""
    with Receiver() as receiver:
        result = run([NODE, str(FIXTURE)], env=_fixture_env(receiver.origin, "error"), stdin=None, timeout=90)
        _check(result, "node run_fixture.mjs (error)")
        spans = receiver.spans()
    output = _last_json_line(result.stdout)
    assert output["error"] == "lookup exploded"

    root = _one(spans, "failingFlow")
    tool = _one(spans, "brokenLookup")
    assert not root.get("parentSpanId")
    for span in (root, tool):
        assert _status_code(span) == 2, span["name"]
        assert span["status"].get("message") == "lookup exploded"
        events = [e for e in span.get("events", []) if e["name"] == "exception"]
        assert len(events) == 1, span["name"]
        event_attrs = {i["key"]: _value(i["value"]) for i in events[0].get("attributes", [])}
        assert event_attrs["exception.message"] == "lookup exploded"
    assert _attrs(tool)["genkit:state"] == "error"
    assert _attrs(tool)["fi.span.kind"] == "TOOL"
    # Genkit marks only the first failing span as the failure source (instrumentation.ts:164-172).
    assert _attrs(tool)["genkit:isFailureSource"] is True
    assert "genkit:isFailureSource" not in _attrs(root)


def _exception_events(span: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {i["key"]: _value(i["value"]) for i in e.get("attributes", [])} for e in span.get("events", []) if e["name"] == "exception"
    ]


def _run_schema_journey(extra: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    with Receiver() as receiver:
        result = run([NODE, str(FIXTURE)], env=_fixture_env(receiver.origin, "schema", extra), stdin=None, timeout=90)
        _check(result, "node run_fixture.mjs (schema)")
        spans = receiver.spans()
    output = _last_json_line(result.stdout)
    # Control: Genkit's ValidationError really carried the model output after "Provided data:".
    assert "Schema validation failed" in output["error"], output
    assert "Provided data:" in output["error"] and "SECRET_STRUCTURED_OUTPUT_MARKER" in output["error"], output
    return {"output": output, "spans": spans}


def test_schema_validation_error_does_not_export_model_output_with_content_off(built_package: Path) -> None:
    """N1: ai.generate with an output schema gets non-matching JSON from the model. Genkit's
    ValidationError embeds that output in the error message, which Genkit writes to span status
    and exception events. With captureContent off the model output must appear nowhere."""
    run_ = _run_schema_journey()
    spans, model_output = run_["spans"], run_["output"]["modelOutput"]
    blob = json.dumps(spans)
    assert "SECRET_STRUCTURED_OUTPUT_MARKER" not in blob
    assert model_output not in blob and json.dumps(model_output) not in blob
    _assert_no_content(spans)

    flow = _one(spans, "structuredFlow")
    generate = _one(spans, "generate")
    for span in (flow, generate):
        assert _status_code(span) == 2, span["name"]
        message = span["status"]["message"]
        assert message.startswith("INVALID_ARGUMENT: Schema validation failed. Parse Errors:"), message
        assert message.endswith(REDACTED_NOTE) and "Provided data:" not in message, message
        events = _exception_events(span)
        assert len(events) == 1, span["name"]
        # Kept as recorded: sdk-trace-base 1.25 Span.recordException uses `error.code` first
        # (GenkitError.code is the HTTP status, 400 for INVALID_ARGUMENT).
        assert events[0]["exception.type"] == "400"
        assert events[0]["exception.message"] == message
        assert events[0]["exception.stacktrace"].endswith(REDACTED_NOTE)
    assert _attrs(generate)["genkit:isFailureSource"] is True


def test_schema_validation_error_keeps_data_after_opt_in(built_package: Path) -> None:
    """Control for N1: with captureContent the same error message is exported unchanged."""
    spans = _run_schema_journey({"CAPTURE_CONTENT": "1"})["spans"]
    generate = _one(spans, "generate")
    assert "Provided data:" in generate["status"]["message"]
    assert "SECRET_STRUCTURED_OUTPUT_MARKER" in generate["status"]["message"]
    assert "SECRET_STRUCTURED_OUTPUT_MARKER" in _exception_events(generate)[0]["exception.message"]
    assert REDACTED_NOTE not in json.dumps(spans)


def test_beta_agent_session_id_maps_to_session_id(built_package: Path) -> None:
    """Session: the native key at 1.42.0 is genkit:metadata:agent:sessionId on the beta agent span."""
    with Receiver() as receiver:
        result = run([NODE, str(FIXTURE)], env=_fixture_env(receiver.origin, "agent"), stdin=None, timeout=90)
        _check(result, "node run_fixture.mjs (agent)")
        spans = receiver.spans()
        _assert_collector_contract(receiver.requests())
    output = _last_json_line(result.stdout)
    session_id = output["sessionId"]
    assert session_id and output["result"] == "agent SECRET_OUTPUT_MARKER"

    agent = _one(spans, "supportAgent")
    a = _attrs(agent)
    assert not agent.get("parentSpanId")
    assert (a["fi.span.kind"], a["gen_ai.span.kind"]) == ("AGENT", "AGENT")
    assert a["session.id"] == session_id
    assert a["genkit:metadata:agent:sessionId"] == session_id
    assert "genkit:init" not in a  # agent init state is content
    model = _one(spans, "contract/agent-model")
    m = _attrs(model)
    assert (m["fi.span.kind"], m["gen_ai.usage.input_tokens"], m["gen_ai.usage.output_tokens"]) == ("LLM", 3, 2)
    assert _attrs(_one(spans, "render"))["fi.span.kind"] == "CHAIN"  # genkit:type=promptTemplate
    # Only the agent span carries the native session id; child spans do not.
    assert [s["name"] for s in spans if "session.id" in _attrs(s)] == ["supportAgent"]
    _assert_no_content(spans)


def test_sigterm_flush_on_signals_delivers_every_span(built_package: Path) -> None:
    """AC-06: no explicit flush after the flow; SIGTERM -> flushOnSignals(flushTracing) -> zero spans dropped."""
    with Receiver() as receiver:
        result = run([NODE, str(FIXTURE)], env=_fixture_env(receiver.origin, "sigterm"), stdin=None, timeout=90)
        _check(result, "node run_fixture.mjs (sigterm)")  # exit 0 comes from Genkit's own listener
        spans = receiver.spans()
    output = _last_json_line(result.stdout)
    assert output["signal"] == "SIGTERM" and output["flushed"] is True
    assert output["result"] == "final SECRET_OUTPUT_MARKER"
    assert sorted(s["name"] for s in spans) == sorted(
        ["qaFlow", "prepare", "generate", "generate", "contract/tool-model", "contract/tool-model", "lookup"]
    )
    assert len({s["spanId"] for s in spans}) == 7


def test_sigterm_app_listener_loses_the_race_with_genkit_exit(built_package: Path) -> None:
    """Control for AC-06: an app SIGTERM listener that awaits flushTracing() never finishes, because
    genkit's module-level listener (genkit/src/genkit.ts:786-793) calls process.exit(0) first."""
    with Receiver() as receiver:
        result = run([NODE, str(FIXTURE)], env=_fixture_env(receiver.origin, "sigterm", {"SIGNAL_MODE": "plain"}), stdin=None, timeout=90)
        _check(result, "node run_fixture.mjs (sigterm, plain listener)")
        delivered = len(receiver.spans())
    assert b'"flushed":true' not in result.stdout
    # Not asserted: how many of the 7 spans made it (observed 6 or 7 on a loopback collector).
    print(f"plain SIGTERM listener: {delivered}/7 spans delivered")


def test_collector_down_never_breaks_the_flow(built_package: Path) -> None:
    """Exporter failure: the flow returns, flushTracing() resolves, the process exits 0."""
    with Receiver() as receiver:
        dead = receiver.origin
    # The receiver is closed: the port now refuses connections.
    started = time.monotonic()
    result = run([NODE, str(FIXTURE)], env=_fixture_env(dead, "tool"), stdin=None, timeout=120)
    elapsed = time.monotonic() - started
    _check(result, "node run_fixture.mjs (collector down)")
    output = _last_json_line(result.stdout)
    assert output["result"] == "final SECRET_OUTPUT_MARKER"
    assert output["flushError"] is None
    assert elapsed < 60


def test_global_provider_misconfiguration_is_warned_and_bypasses_mapping(built_package: Path) -> None:
    """Control for the setup deviation: register() with the default setGlobalTracerProvider=true.

    Genkit's NodeSDK then cannot register its provider, Genkit uses the fi-core
    provider directly, and raw spans (content included, no kind) reach the
    collector. The processor warns at construction.
    """
    with Receiver() as receiver:
        result = run([NODE, str(FIXTURE)], env=_fixture_env(receiver.origin, "tool", {"FI_GLOBAL_PROVIDER": "1"}), stdin=None, timeout=90)
        _check(result, "node run_fixture.mjs (global provider)")
        spans = receiver.spans()
    assert b"TRACEAI_GENKIT_GLOBAL_PROVIDER" in result.stderr
    assert spans
    assert all("fi.span.kind" not in _attrs(s) for s in spans)
    assert "SECRET_PROMPT_MARKER" in json.dumps(spans)


def _wait_for_runtime_file(project: Path, deadline: float) -> Dict[str, Any]:
    runtimes = project / ".genkit" / "runtimes"
    while time.monotonic() < deadline:
        for path in runtimes.glob("*.json") if runtimes.exists() else []:
            try:
                return json.loads(path.read_text())
            except ValueError:
                pass
        time.sleep(0.1)
    raise AssertionError("Genkit reflection server never wrote its runtime file")


def test_dev_runtime_reflection_server_and_telemetry_server_still_work(built_package: Path, tmp_path: Path) -> None:
    """AC-01 (dev mode): GENKIT_ENV=dev, the reflection server the Dev UI drives starts, runAction works,
    Genkit's telemetry-server exporter gets the trace, and Future AGI gets the mapped copy."""
    project = tmp_path / "genkit-app"
    project.mkdir()
    (project / "package.json").write_text('{"name": "genkit-app", "private": true}')
    stand_in = TelemetryServerStandIn()
    try:
        with Receiver() as receiver:
            env = _fixture_env(receiver.origin, "devserver", {"GENKIT_ENV": "dev", "GENKIT_TELEMETRY_SERVER": stand_in.origin})
            process = subprocess.Popen([NODE, str(FIXTURE)], env=env, cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                runtime = _wait_for_runtime_file(project, time.monotonic() + 60)
                base = runtime["reflectionServerUrl"].replace("localhost", "127.0.0.1")
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(f"{base}/api/__health", timeout=10) as response:
                    assert response.status == 200
                request = urllib.request.Request(
                    f"{base}/api/runAction",
                    data=json.dumps({"key": "/flow/qaFlow", "input": "  dev ui question  "}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with opener.open(request, timeout=60) as response:
                    run_action = json.loads(response.read().decode())
                trace_id = run_action["telemetry"]["traceId"]
                assert run_action["result"] == "final SECRET_OUTPUT_MARKER"
                # Genkit's telemetry-server exporter posts asynchronously: its flushTracing() does not wait
                # for in-flight posts (sdk-trace-base 1.25 SimpleSpanProcessor). The Dev UI reads the trace
                # later, so wait for it here before stopping the app.
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if len({s["spanId"] for s in stand_in.spans() if s["traceId"] == trace_id}) >= 7:
                        break
                    time.sleep(0.1)
                process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=60)
                assert b'"flushed":true' in stdout
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            assert process.returncode == 0, stderr.decode()
            fi_spans = receiver.spans()
            _assert_collector_contract(receiver.requests())
    finally:
        stand_in.close()

    genkit_spans = stand_in.spans()
    assert {s["traceId"] for s in genkit_spans} >= {trace_id}
    genkit_ids = {s["spanId"] for s in genkit_spans if s["traceId"] == trace_id}
    fi_ids = {s["spanId"] for s in fi_spans if s["traceId"] == trace_id}
    assert len(genkit_ids) == 7 and genkit_ids == fi_ids
    assert all(_attrs(s).get("fi.span.kind") for s in fi_spans if s["traceId"] == trace_id)
    _assert_no_content(fi_spans)


def test_inventory_types_are_all_mapped(built_package: Path) -> None:
    """Drift check: every (genkit:type, subtype) the pinned genkit emits for the fixture has a kind,
    and every genkit:* key it emits is in GenkitAttributes."""
    result = run([NODE, str(INVENTORY)], env=_base_env(), stdin=None, timeout=90)
    _check(result, "node inventory.mjs")
    inventory = json.loads(result.stdout.decode())
    check = (
        "const m=require(process.argv[1]);const inv=JSON.parse(require('fs').readFileSync(0,'utf8'));"
        "const known=new Set(Object.values(m.GenkitAttributes));const out=inv.map(s=>({name:s.name,"
        "kind:m.genkitSpanKind({'genkit:type':s['genkit:type'],'genkit:metadata:subtype':s['genkit:metadata:subtype']})||null,"
        "unknownKeys:Object.keys(s.keys).filter(k=>!known.has(k))}));process.stdout.write(JSON.stringify(out));"
    )
    mapped = subprocess.run(
        [NODE, "-e", check, str(PKG_DIR)], input=result.stdout, capture_output=True, env=_base_env(), timeout=60, check=True
    )
    rows = json.loads(mapped.stdout.decode())
    assert len(rows) == len(inventory) == 14
    for row in rows:
        assert row["kind"] is not None, row
        assert row["unknownKeys"] == [], row
    assert {s["genkit:type"] for s in inventory} == {"action", "flowStep", "util"}
    assert {s.get("genkit:metadata:subtype") for s in inventory if s["genkit:type"] == "action"} == {"flow", "model", "tool"}


def test_genkit_is_apache_2_peer_dependency(built_package: Path) -> None:
    manifest = json.loads((GENKIT_DIR / "package.json").read_text())
    assert manifest["version"] == "1.42.0"
    assert manifest["license"] == "Apache-2.0"
    assert "Apache License" in (GENKIT_DIR / "LICENSE").read_text()[:200]
    ours = json.loads((PKG_DIR / "package.json").read_text())
    assert ours["peerDependencies"] == {"genkit": "^1.42.0"}
    assert "genkit" not in ours["dependencies"]
    assert ours["devDependencies"]["genkit"] == "1.42.0"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _genkit_file_hashes() -> Dict[str, str]:
    """Hash every file of the installed genkit, @genkit-ai/core and @genkit-ai/ai."""
    hashes: Dict[str, str] = {}
    store = GENKIT_DIR.resolve().parent  # node_modules dir inside the pnpm store entry
    roots = [GENKIT_DIR.resolve(), (store / "@genkit-ai" / "ai").resolve(), (store / "@genkit-ai" / "core").resolve()]
    for root in roots:
        assert root.is_dir(), root
        for file in root.rglob("*"):
            if file.is_file() and "node_modules" not in file.relative_to(root).parts:
                hashes[_sha256(file.read_bytes())] = str(file)
    return hashes


def test_packed_tarball_has_no_genkit_sources(built_package: Path, tmp_path: Path) -> None:
    result = run([PNPM, "--dir", str(PKG_DIR), "pack", "--pack-destination", str(tmp_path)], env=_base_env(), stdin=None, timeout=300)
    _check(result, "pnpm pack")
    tarballs = list(tmp_path.glob("*.tgz"))
    assert len(tarballs) == 1, tarballs
    genkit_hashes = _genkit_file_hashes()
    assert len(genkit_hashes) > 100, "installed genkit not found; cannot compare"
    with tarfile.open(tarballs[0]) as tar:
        members = [m for m in tar.getmembers() if m.isfile()]
        manifest = json.loads(tar.extractfile("package/package.json").read())  # type: ignore[union-attr]
        for member in members:
            data = tar.extractfile(member).read()  # type: ignore[union-attr]
            relative = member.name[len("package/"):]
            assert relative.split("/")[0] in {"dist", "package.json", "README.md"}, relative
            assert "node_modules" not in member.name and "__tests__" not in member.name and "contract" not in member.name
            assert not relative.endswith(".tsbuildinfo"), f"build cache in tarball: {relative}"
            assert _sha256(data) not in genkit_hashes, f"{relative} is a copy of {genkit_hashes[_sha256(data)]}"
            assert b"Copyright 2024 Google LLC" not in data and b"Copyright 2025 Google LLC" not in data, relative
    assert manifest["peerDependencies"] == {"genkit": "^1.42.0"}
    assert "genkit" not in manifest.get("dependencies", {})
    assert not manifest.get("bundledDependencies") and not manifest.get("bundleDependencies")
    assert manifest["license"] == "Apache-2.0"
    assert all(not v.startswith("workspace:") for v in manifest.get("dependencies", {}).values())


def _node_matrix() -> List[str]:
    configured = [p for p in os.environ.get("TRACEAI_NODE_MATRIX", "").split(os.pathsep) if p]
    return configured or [NODE]


def test_manifest_entry_points_exist_in_the_build(built_package: Path) -> None:
    manifest = json.loads((PKG_DIR / "package.json").read_text())
    targets = [manifest["main"], manifest["module"], manifest["esnext"], manifest["types"]]
    targets += list(manifest["exports"]["."].values())
    for target in targets:
        assert (PKG_DIR / target).is_file(), target
    assert json.loads((PKG_DIR / "dist" / "esm" / "package.json").read_text()) == {"type": "module"}


@pytest.mark.parametrize("node", _node_matrix())
def test_esm_and_cjs_entry_points_import(built_package: Path, tmp_path: Path, node: str) -> None:
    scope = tmp_path / "node_modules" / "@traceai"
    scope.mkdir(parents=True)
    (scope / "genkit").symlink_to(PKG_DIR, target_is_directory=True)
    check = (
        "const names=['FIGenkitSpanProcessor','mapGenkitAttributes','genkitSpanKind','GenkitAttributes'];"
        "function ok(m,kind){for(const n of names){if(!(n in m))throw new Error(kind+' missing '+n)}}"
    )
    cjs = run(
        [node, "-e", check + "ok(require('@traceai/genkit'),'cjs');console.log(process.version,'cjs ok')"],
        env={**_base_env(), "NODE_PATH": str(tmp_path / "node_modules")},
        stdin=None,
        timeout=60,
    )
    script = tmp_path / "check.mjs"
    script.write_text(check + "ok(await import('@traceai/genkit'),'esm');console.log(process.version,'esm ok');")
    esm = run([node, str(script)], env=_base_env(), stdin=None, timeout=60)
    _check(cjs, f"{node} require")
    _check(esm, f"{node} import")
    assert b"cjs ok" in cjs.stdout and b"esm ok" in esm.stdout
