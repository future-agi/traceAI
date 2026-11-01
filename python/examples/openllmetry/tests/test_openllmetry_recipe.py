"""Contract test for the OpenLLMetry (Traceloop SDK) recipe.

Runs ``src/app.py`` as written with the real ``traceloop-sdk`` 0.62.4 and a
real ``openai`` client, in a subprocess whose network is limited to
127.0.0.1 (``_guarded_run.py``). The model is a loopback fake of the OpenAI
Chat Completions API; spans go to the shared harness ``Receiver``, which
serves ``/v1/traces`` and ``/tracer/v1/traces`` like fi-collector's HTTP mux
but does not authenticate, stamp projects or store anything.

All keys are placeholders. Nothing here contacts Traceloop, OpenAI or Future
AGI.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlsplit

import pytest

from harness import Receiver, run

TESTS_DIR = Path(__file__).resolve().parent
RECIPE_DIR = TESTS_DIR.parent
APP = RECIPE_DIR / "src" / "app.py"
README = RECIPE_DIR / "README.md"
BARE_CALL = TESTS_DIR / "bare_llm_call.py"
GUARD = TESTS_DIR / "_guarded_run.py"
GUARD_PROBE = TESTS_DIR / "_guard_probe.py"

sys.path.insert(0, str(TESTS_DIR))
from _fake_openai import RESPONSE_MODEL, USAGE, FakeOpenAI  # noqa: E402

PROJECT = "openllmetry-recipe-contract"
SERVICE_NAME = "openllmetry-recipe"
FI_API_KEY = "fi-api-placeholder-0000"
# A comma and an equals sign: the recipe's percent-encoding must carry them.
FI_SECRET_KEY = "fi-secret-placeholder,part=0000"
OPENAI_KEY = "sk-openai-placeholder-0000"

# Content markers: the question (input), the fake model's answer (output)
# and the tool's result. None may reach an export with content off.
QUESTION = "QMARK7c1e what is the refund window?"
ANSWER = "AMARK5d2b refunds are accepted for 30 days."
TOOL_OUTPUT = "Refunds are accepted within 30 days of purchase."
CONTENT_MARKERS = ("QMARK7c1e", "AMARK5d2b", TOOL_OUTPUT)
CONTENT_KEYS = (
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "traceloop.entity.input",
    "traceloop.entity.output",
)

SPAN_NAMES = {
    "workflow": "support_request.workflow",
    "task": "normalize_question.task",
    "agent": "support_agent.agent",
    "tool": "lookup_policy.tool",
    "llm": "openai.chat",
}
# A hang guard, not a speed check: one run imports the SDK and its ~40
# instrumentors, which took up to a minute on a loaded laptop.
RUN_TIMEOUT_SECONDS = 180


def traceloop_headers(api_key: str, secret_key: str) -> str:
    """Build TRACELOOP_HEADERS exactly as README.md tells users to."""
    return "x-api-key={0},x-secret-key={1}".format(
        quote(api_key, safe=""), quote(secret_key, safe="")
    )


class _PathRecorder:
    """Loopback catch-all that records every request's method and path.

    It answers /v1/traces and /tracer/v1/traces with 200 and anything else
    with 404, as fi-collector's HTTP mux does (pkg/server/server.go).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                with lock:
                    owner.calls.append(("POST", path))
                status = (
                    HTTPStatus.OK
                    if path in ("/v1/traces", "/tracer/v1/traces")
                    else HTTPStatus.NOT_FOUND
                )
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = "http://127.0.0.1:{0}".format(self._server.server_port)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def __enter__(self) -> "_PathRecorder":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def paths(self) -> set[str]:
        return {path for _method, path in self.calls}


def _child_env(
    home: Path, guard_log: Path, base_url: str, openai_base_url: str, **overrides: Optional[str]
) -> dict[str, str]:
    """The recipe's environment, built from scratch (nothing inherited but PATH).

    Bytecode writing stays on so later runs reuse the SDK's compiled modules;
    the scripts themselves run through runpy and leave no .pyc in the repo.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "LOOPBACK_GUARD_LOG": str(guard_log),
        "TRACELOOP_BASE_URL": base_url,
        "TRACELOOP_HEADERS": traceloop_headers(FI_API_KEY, FI_SECRET_KEY),
        "TRACELOOP_METRICS_ENABLED": "false",
        "TRACELOOP_TRACE_CONTENT": "false",
        "FI_PROJECT_NAME": PROJECT,
        "OPENAI_API_KEY": OPENAI_KEY,
        "OPENAI_BASE_URL": openai_base_url,
    }
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _run_script(
    tmp_path: Path,
    script: Path,
    args: list[str],
    base_url_for: Any,
    **overrides: Optional[str],
) -> dict[str, Any]:
    """Run one script under the loopback guard; return everything the tests read."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    guard_log = tmp_path / "guard.jsonl"
    with Receiver() as receiver, FakeOpenAI(ANSWER) as fake:
        env = _child_env(
            tmp_path, guard_log, base_url_for(receiver), fake.base_url, **overrides
        )
        result = run(
            [sys.executable, str(GUARD), str(script), *args],
            env=env,
            stdin=None,
            timeout=RUN_TIMEOUT_SECONDS,
        )
        record = {
            "result": result,
            "stdout": result.stdout.decode("utf-8", "replace"),
            "stderr": result.stderr.decode("utf-8", "replace"),
            "requests": receiver.requests(),
            "spans": receiver.spans(),
            "model_requests": list(fake.requests),
            "model_authorizations": list(fake.authorizations),
        }
    record["guard_attempts"] = (
        [json.loads(line) for line in guard_log.read_text().splitlines()]
        if guard_log.exists()
        else []
    )
    return record


def _value(value: dict[str, Any]) -> Any:
    for kind in ("stringValue", "boolValue", "doubleValue"):
        if kind in value:
            return value[kind]
    if "intValue" in value:
        return int(value["intValue"])
    if "arrayValue" in value:
        return [_value(item) for item in value["arrayValue"].get("values", [])]
    return value


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    return {item["key"]: _value(item.get("value", {})) for item in span.get("attributes", [])}


def _by_name(spans: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    named = {span["name"]: span for span in spans}
    assert len(named) == len(spans), "span names repeat: {0}".format(
        sorted(span["name"] for span in spans)
    )
    return named


def _export_dump(record: dict[str, Any]) -> str:
    """Everything the export carried except HTTP headers: spans and resources."""
    return json.dumps(
        {
            "spans": record["spans"],
            "resources": [r["resource_attributes"] for r in record["requests"]],
        },
        sort_keys=True,
    )


def _assert_ran(record: dict[str, Any]) -> None:
    result = record["result"]
    assert not result.timed_out, record["stderr"]
    assert result.returncode == 0, record["stderr"]
    assert record["guard_attempts"] == [], record["guard_attempts"]


# --------------------------------------------------------------------------
# The recipe as documented: content off, metrics off, headers percent-encoded.
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def recipe_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    record = _run_script(
        tmp_path_factory.mktemp("recipe"),
        APP,
        [QUESTION],
        lambda receiver: receiver.origin,
    )
    _assert_ran(record)
    return record


def test_recipe_runs_with_loopback_network_only(recipe_run: dict[str, Any]) -> None:
    assert recipe_run["guard_attempts"] == []
    assert ANSWER in recipe_run["stdout"]
    # The model call reached the fake, with the OpenAI key the client was given.
    assert len(recipe_run["model_requests"]) == 1
    assert recipe_run["model_authorizations"] == ["Bearer " + OPENAI_KEY]


def test_guard_refuses_and_logs_non_loopback_connections(tmp_path: Path) -> None:
    """Positive control for every ``guard_attempts == []`` assertion in this file."""
    guard_log = tmp_path / "guard.jsonl"
    result = run(
        [sys.executable, str(GUARD), str(GUARD_PROBE)],
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "LOOPBACK_GUARD_LOG": str(guard_log)},
        stdin=None,
        timeout=60,
    )
    assert not result.timed_out
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert "refused: connect,connect_ex,getaddrinfo" in result.stdout.decode("utf-8", "replace")
    logged = [json.loads(line) for line in guard_log.read_text().splitlines()]
    assert [entry["kind"] for entry in logged] == ["connect", "connect_ex", "getaddrinfo"]
    assert "192.0.2.1" in logged[0]["target"]
    assert "2001:db8::1" in logged[1]["target"]
    assert "example.invalid" in logged[2]["target"]


def test_base_url_without_path_posts_to_v1_traces_once(recipe_run: dict[str, Any]) -> None:
    assert recipe_run["requests"], "no export reached the receiver"
    assert {request["path"] for request in recipe_run["requests"]} == {"/v1/traces"}


def test_traceloop_headers_carry_the_future_agi_keys(recipe_run: dict[str, Any]) -> None:
    assert recipe_run["requests"], "no export reached the receiver"
    for request in recipe_run["requests"]:
        headers = request["headers"]
        assert headers.get("x-api-key") == FI_API_KEY
        assert headers.get("x-secret-key") == FI_SECRET_KEY
        # No Traceloop bearer token and no OpenAI key on the export.
        assert "authorization" not in headers
        assert all(OPENAI_KEY not in value for value in headers.values())


def test_resource_carries_the_project(recipe_run: dict[str, Any]) -> None:
    assert recipe_run["requests"], "no export reached the receiver"
    for request in recipe_run["requests"]:
        assert request["resource_attributes"], "export without a resource"
        for resource in request["resource_attributes"]:
            assert resource.get("project_name") == PROJECT
            assert resource.get("project_type") == "observe"
            assert resource.get("service.name") == SERVICE_NAME


def test_traceloop_span_kinds_and_tree(recipe_run: dict[str, Any]) -> None:
    spans = _by_name(recipe_run["spans"])
    assert set(spans) == set(SPAN_NAMES.values())
    for kind in ("workflow", "task", "agent", "tool"):
        assert _attrs(spans[SPAN_NAMES[kind]]).get("traceloop.span.kind") == kind

    llm = _attrs(spans[SPAN_NAMES["llm"]])
    assert "traceloop.span.kind" not in llm
    assert llm.get("gen_ai.operation.name") == "chat"
    assert llm.get("gen_ai.provider.name") == "openai"
    assert llm.get("gen_ai.request.model") == "gpt-4o-mini"
    assert llm.get("gen_ai.response.model") == RESPONSE_MODEL
    assert llm.get("gen_ai.usage.input_tokens") == USAGE["prompt_tokens"]
    assert llm.get("gen_ai.usage.output_tokens") == USAGE["completion_tokens"]
    assert llm.get("gen_ai.usage.total_tokens") == USAGE["total_tokens"]
    # 0.62.4 does not emit the legacy llm.* model/usage keys.
    assert "llm.request.model" not in llm
    assert "llm.usage.prompt_tokens" not in llm
    # Every span, the model call included, carries a traceloop.* key.
    assert llm.get("traceloop.workflow.name") == "support_request"
    for span in spans.values():
        assert any(key.startswith("traceloop.") for key in _attrs(span)), span["name"]

    # One trace: workflow > task, workflow > agent > tool, agent > model call.
    assert len({span["traceId"] for span in spans.values()}) == 1
    workflow = spans[SPAN_NAMES["workflow"]]
    agent_span = spans[SPAN_NAMES["agent"]]
    assert not workflow.get("parentSpanId")
    assert spans[SPAN_NAMES["task"]]["parentSpanId"] == workflow["spanId"]
    assert agent_span["parentSpanId"] == workflow["spanId"]
    assert spans[SPAN_NAMES["tool"]]["parentSpanId"] == agent_span["spanId"]
    assert spans[SPAN_NAMES["llm"]]["parentSpanId"] == agent_span["spanId"]


def test_spans_that_carry_both_gen_ai_and_traceloop_keys(recipe_run: dict[str, Any]) -> None:
    """Recorded, not resolved: one stored kind for these spans is SF-1's assertion."""
    both = set()
    for name, span in _by_name(recipe_run["spans"]).items():
        keys = _attrs(span)
        if any(k.startswith("gen_ai.") for k in keys) and any(
            k.startswith("traceloop.") for k in keys
        ):
            both.add(name)
    assert both == {SPAN_NAMES["agent"], SPAN_NAMES["tool"], SPAN_NAMES["llm"]}


def test_content_off_exports_no_content(recipe_run: dict[str, Any]) -> None:
    dump = _export_dump(recipe_run)
    for marker in CONTENT_MARKERS:
        assert marker not in dump, marker
    for span in recipe_run["spans"]:
        assert not set(CONTENT_KEYS) & set(_attrs(span)), span["name"]
        assert not span.get("events"), span["name"]


def test_no_secret_in_the_export_or_output(recipe_run: dict[str, Any]) -> None:
    dump = _export_dump(recipe_run)
    output = recipe_run["stdout"] + recipe_run["stderr"]
    secrets = (FI_API_KEY, FI_SECRET_KEY, quote(FI_SECRET_KEY, safe=""), OPENAI_KEY)
    for secret in secrets:
        assert secret not in dump
        assert secret not in output


# --------------------------------------------------------------------------
# Controls and the documented edges.
# --------------------------------------------------------------------------


def test_content_is_exported_when_trace_content_is_unset(tmp_path: Path) -> None:
    """Traceloop's default is content ON. Control for the content-off test."""
    record = _run_script(
        tmp_path,
        APP,
        [QUESTION],
        lambda receiver: receiver.origin,
        TRACELOOP_TRACE_CONTENT=None,
    )
    _assert_ran(record)
    spans = {name: _attrs(span) for name, span in _by_name(record["spans"]).items()}
    llm = spans[SPAN_NAMES["llm"]]
    assert "QMARK7c1e" in llm["gen_ai.input.messages"]
    assert TOOL_OUTPUT in llm["gen_ai.input.messages"]  # the system prompt
    assert "AMARK5d2b" in llm["gen_ai.output.messages"]
    assert "QMARK7c1e" in spans[SPAN_NAMES["workflow"]]["traceloop.entity.input"]
    assert "AMARK5d2b" in spans[SPAN_NAMES["workflow"]]["traceloop.entity.output"]
    assert TOOL_OUTPUT in spans[SPAN_NAMES["tool"]]["traceloop.entity.output"]
    dump = _export_dump(record)
    for secret in (FI_API_KEY, FI_SECRET_KEY, OPENAI_KEY):
        assert secret not in dump


@pytest.mark.parametrize(
    ("suffix", "expected_path"),
    [
        ("/", "/v1/traces"),
        # 0.62.4 does not double a suffix that is already there.
        ("/v1/traces", "/v1/traces"),
        # fi-collector also serves /tracer/v1/traces.
        ("/tracer", "/tracer/v1/traces"),
    ],
)
def test_sdk_appends_v1_traces_unless_already_present(
    tmp_path: Path, suffix: str, expected_path: str
) -> None:
    record = _run_script(
        tmp_path,
        APP,
        [QUESTION],
        lambda receiver: receiver.origin + suffix,
    )
    _assert_ran(record)
    assert {request["path"] for request in record["requests"]} == {expected_path}
    assert {span["name"] for span in record["spans"]} == set(SPAN_NAMES.values())


def test_unencoded_comma_in_a_header_value_truncates_it(tmp_path: Path) -> None:
    """Why the recipe percent-encodes: the SDK splits TRACELOOP_HEADERS on commas."""
    raw = "x-api-key={0},x-secret-key={1}".format(FI_API_KEY, FI_SECRET_KEY)
    record = _run_script(
        tmp_path,
        APP,
        [QUESTION],
        lambda receiver: receiver.origin,
        TRACELOOP_HEADERS=raw,
    )
    _assert_ran(record)
    assert record["requests"]
    for request in record["requests"]:
        assert request["headers"].get("x-api-key") == FI_API_KEY
        assert request["headers"].get("x-secret-key") == FI_SECRET_KEY.split(",")[0]


def test_app_refuses_a_traceloop_api_key(tmp_path: Path) -> None:
    record = _run_script(
        tmp_path,
        APP,
        [QUESTION],
        lambda receiver: receiver.origin,
        TRACELOOP_API_KEY="tl-placeholder-0000",
    )
    assert not record["result"].timed_out
    assert record["result"].returncode != 0
    assert "Unset TRACELOOP_API_KEY" in record["stderr"]
    assert record["requests"] == []
    assert record["model_requests"] == []
    assert record["guard_attempts"] == []


def test_model_call_outside_a_workflow_has_no_traceloop_key(tmp_path: Path) -> None:
    record = _run_script(
        tmp_path,
        BARE_CALL,
        [QUESTION],
        lambda receiver: receiver.origin,
    )
    _assert_ran(record)
    spans = _by_name(record["spans"])
    assert set(spans) == {SPAN_NAMES["llm"]}
    attrs = _attrs(spans[SPAN_NAMES["llm"]])
    assert attrs.get("gen_ai.operation.name") == "chat"
    assert not [key for key in attrs if key.startswith("traceloop.")]


@pytest.mark.parametrize("metrics_enabled", [None, "false"])
def test_metrics_go_to_v1_metrics_unless_disabled(
    tmp_path: Path, metrics_enabled: Optional[str]
) -> None:
    """fi-collector routes no /v1/metrics; the recipe turns metrics off."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    guard_log = tmp_path / "guard.jsonl"
    with _PathRecorder() as recorder, FakeOpenAI(ANSWER) as fake:
        env = _child_env(
            tmp_path,
            guard_log,
            recorder.origin,
            fake.base_url,
            TRACELOOP_METRICS_ENABLED=metrics_enabled,
        )
        result = run(
            [sys.executable, str(GUARD), str(APP), QUESTION],
            env=env,
            stdin=None,
            timeout=RUN_TIMEOUT_SECONDS,
        )
        paths = recorder.paths()
    assert not result.timed_out
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert not guard_log.exists() or guard_log.read_text() == ""
    assert "/v1/traces" in paths
    if metrics_enabled is None:
        assert "/v1/metrics" in paths
    else:
        assert "/v1/metrics" not in paths


# --------------------------------------------------------------------------
# Opt-in: read the backend's kind map instead of copying it.
# --------------------------------------------------------------------------


def _backend_maps() -> tuple[dict[str, str], dict[str, str]]:
    path = os.environ.get("FI_BACKEND_OPENLLMETRY_PY")
    if not path:
        pytest.skip(
            "set FI_BACKEND_OPENLLMETRY_PY to futureagi/tracer/utils/adapters/openllmetry.py"
        )
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    maps: dict[str, dict[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in (
                "_TRACELOOP_KIND_MAP",
                "_OPERATION_KIND_MAP",
            ):
                maps[target.id] = ast.literal_eval(node.value)
    return maps["_TRACELOOP_KIND_MAP"], maps["_OPERATION_KIND_MAP"]


def test_emitted_kinds_are_keys_of_the_backend_map(recipe_run: dict[str, Any]) -> None:
    kind_map, operation_map = _backend_maps()
    attrs = [_attrs(span) for span in recipe_run["spans"]]
    kinds = {a["traceloop.span.kind"] for a in attrs if "traceloop.span.kind" in a}
    operations = {a["gen_ai.operation.name"] for a in attrs if "gen_ai.operation.name" in a}
    assert kinds == {"workflow", "task", "agent", "tool"}
    assert kinds <= set(kind_map)
    assert operations == {"chat"}
    assert operations <= set(operation_map)


def test_readme_kind_table_matches_the_backend_map() -> None:
    kind_map, _operation_map = _backend_maps()
    text = README.read_text(encoding="utf-8")
    rows = dict(re.findall(r"^\| `([a-z_]+)` \| ([A-Z_]+) \|$", text, flags=re.MULTILINE))
    assert rows == kind_map
