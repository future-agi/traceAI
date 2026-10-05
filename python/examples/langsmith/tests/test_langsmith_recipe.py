"""Contract test for the LangSmith trace-forwarding recipe.

The two halves are tested separately:

* Collector half (no LangSmith install needed). ``fixtures/`` holds a
  hand-built OTLP body of a LangSmith run tree (a chain run with a tool child
  and an LLM child), written from the langsmith 0.14.4 OTEL exporter source.
  It is posted with the harness ``post_otlp()`` and checked with ``compare()``.
* SDK half (needs langsmith 0.14.4). ``src/app.py`` runs as written, in a
  subprocess whose network is limited to 127.0.0.1 (``_guarded_run.py``), in
  LangSmith's OTEL-only mode, with no LangSmith key. Its export must match the
  fixture.

Spans go to the shared harness ``Receiver``, which serves ``/v1/traces`` and
``/tracer/v1/traces`` like fi-collector's HTTP mux but does not authenticate,
stamp projects or store anything. Where LangSmith's REST API is needed, a
loopback stub stands in for it. Nothing here contacts LangSmith or Future AGI,
and no LangSmith key is set anywhere: every child environment is built from
scratch with a throwaway ``HOME``.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, urlsplit

import pytest

from harness import Receiver, compare, post_otlp, run

TESTS_DIR = Path(__file__).resolve().parent
RECIPE_DIR = TESTS_DIR.parent
APP = RECIPE_DIR / "src" / "app.py"
README = RECIPE_DIR / "README.md"
FIXTURE = TESTS_DIR / "fixtures" / "langsmith-0.14.4-run-tree.otlp.json"
SDK_OWN_PROVIDER = TESTS_DIR / "sdk_own_provider.py"
GUARD = TESTS_DIR / "_guarded_run.py"
GUARD_PROBE = TESTS_DIR / "_guard_probe.py"

PINNED_LANGSMITH = "0.14.4"
HAS_LANGSMITH = importlib.util.find_spec("langsmith") is not None
needs_sdk = pytest.mark.skipif(not HAS_LANGSMITH, reason="langsmith is not installed")

LANGSMITH_KEY_VARS = ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY")
PROJECT = "langsmith-recipe-contract"
FI_API_KEY = "fi-api-placeholder-0000"
# A comma and an equals sign: the recipe's percent-encoding must carry them.
FI_SECRET_KEY = "fi-secret-placeholder,part=0000"
# LangSmith exports git describe of the working directory as revision_id
# unless this is set; the tests pin it so the export is reproducible.
REVISION = "fixture-revision"

QUESTION = "QMARK7c1e what is the refund window?"
ANSWER = "Refunds are accepted for 30 days."
POLICY = "Refunds are accepted within 30 days of purchase."
CONTENT_MARKERS = ("QMARK7c1e", ANSWER, POLICY)

ROOT, TOOL, LLM = "support_request", "lookup_policy", "chat_model"
SDK_KINDS = {ROOT: "chain", TOOL: "tool", LLM: "llm"}
OPERATIONS = {ROOT: "chain", TOOL: "execute_tool", LLM: "chat"}
# The four keys fi-collector reads a span kind from before it falls back to
# gen_ai.operation.name (fi-collector/exporter/clickhouse25exporter/converter.go).
SPAN_KIND_KEYS = ("fi.span.kind", "gen_ai.span.kind", "llm.request.type", "openinference.span.kind")
# OTLP/JSON writes enums as integers; protobuf decoded by MessageToDict uses names.
STATUS_CODES = {"STATUS_CODE_UNSET": 0, "STATUS_CODE_OK": 1, "STATUS_CODE_ERROR": 2}
SPAN_KINDS = {
    "SPAN_KIND_UNSPECIFIED": 0,
    "SPAN_KIND_INTERNAL": 1,
    "SPAN_KIND_SERVER": 2,
    "SPAN_KIND_CLIENT": 3,
    "SPAN_KIND_PRODUCER": 4,
    "SPAN_KIND_CONSUMER": 5,
}
# A hang guard, not a speed check.
RUN_TIMEOUT_SECONDS = 120


def otlp_headers(api_key: str, secret_key: str) -> str:
    """Build OTEL_EXPORTER_OTLP_HEADERS exactly as README.md tells users to."""
    return "x-api-key={0},x-secret-key={1}".format(
        quote(api_key, safe=""), quote(secret_key, safe="")
    )


# --------------------------------------------------------------------------
# Span helpers
# --------------------------------------------------------------------------


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
    assert len(named) == len(spans), sorted(span["name"] for span in spans)
    return named


def _as_otlp_json(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Protobuf-decoded spans with enums as OTLP/JSON integers, sorted by name."""
    converted = copy.deepcopy(spans)
    for span in converted:
        status = span.get("status", {})
        if isinstance(status.get("code"), str):
            status["code"] = STATUS_CODES[status["code"]]
        if isinstance(span.get("kind"), str):
            span["kind"] = SPAN_KINDS[span["kind"]]
    return sorted(converted, key=lambda span: span["name"])


def _write_golden(directory: Path, spans: list[dict[str, Any]]) -> Path:
    golden = directory / "spans.golden.json"
    golden.write_text(json.dumps(sorted(spans, key=lambda s: s["name"]), indent=1), encoding="utf-8")
    return golden


def _assert_run_tree(spans: list[dict[str, Any]]) -> None:
    """One trace: the chain run is the root, the tool and LLM runs its children."""
    named = _by_name(spans)
    assert set(named) == {ROOT, TOOL, LLM}
    assert len({span["traceId"] for span in spans}) == 1
    root = named[ROOT]
    assert not root.get("parentSpanId")
    for child in (TOOL, LLM):
        assert named[child]["parentSpanId"] == root["spanId"], child
    for name, span in named.items():
        attrs = _attrs(span)
        assert attrs.get("langsmith.span.kind") == SDK_KINDS[name], name
        assert attrs.get("gen_ai.operation.name") == OPERATIONS[name], name
        assert not set(SPAN_KIND_KEYS) & set(attrs), name
        assert _as_otlp_json([span])[0]["status"] == {"code": 1}, name


def _readme_span_rows() -> dict[str, tuple[str, str, str, str]]:
    text = README.read_text(encoding="utf-8")
    rows = re.findall(
        r"^\| `(\w+)` \| (none|`\w+`) \| `(\w+)` \| `(\w+)` \| (\w+) \|$", text, flags=re.MULTILINE
    )
    return {name: (parent.strip("`"), kind, op, fi_type) for name, parent, kind, op, fi_type in rows}


def _readme_run_type_rows() -> dict[str, tuple[str, str]]:
    text = README.read_text(encoding="utf-8")
    rows = re.findall(r"^\| `(\w+)` \| `(\w+)` \| (\w+) \|$", text, flags=re.MULTILINE)
    return {run_type: (op, fi_type) for run_type, op, fi_type in rows}


# --------------------------------------------------------------------------
# The rule: no LangSmith key anywhere.
# --------------------------------------------------------------------------


def test_no_langsmith_key_in_the_environment(tmp_path: Path) -> None:
    for name in LANGSMITH_KEY_VARS:
        assert name not in os.environ, "unset {0}: a LangSmith key is a spend hard stop".format(name)
    env = _child_env(tmp_path, tmp_path / "guard.jsonl", "http://127.0.0.1:9")
    assert not [name for name in env if name in LANGSMITH_KEY_VARS]
    # A LangSmith CLI profile can also supply a key; the child HOME has none.
    assert not (Path(env["HOME"]) / ".langsmith").exists()
    assert "LANGSMITH_CONFIG_FILE" not in env


# --------------------------------------------------------------------------
# Collector half: the hand-built fixture, no LangSmith needed.
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fixture_body() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fixture_spans(fixture_body: dict[str, Any]) -> list[dict[str, Any]]:
    [resource_spans] = fixture_body["resourceSpans"]
    [scope_spans] = resource_spans["scopeSpans"]
    assert scope_spans["scope"] == {"name": "langsmith"}
    return scope_spans["spans"]


def test_fixture_round_trips_through_the_harness(
    fixture_body: dict[str, Any], fixture_spans: list[dict[str, Any]], tmp_path: Path
) -> None:
    golden = _write_golden(tmp_path, fixture_spans)
    with Receiver() as receiver:
        assert post_otlp(fixture_body, receiver.collector_endpoint) == 200
        received = receiver.spans()
        requests = receiver.requests()
    assert len(received) == 3
    compare(sorted(received, key=lambda s: s["name"]), golden)
    [request] = requests
    assert request["path"] == "/tracer/v1/traces"
    assert request["resource_attributes"] == [{"project_name": PROJECT, "project_type": "observe"}]
    _assert_run_tree(received)


def test_fixture_is_valid_otlp_json(fixture_spans: list[dict[str, Any]]) -> None:
    """OTLP/JSON: hex trace and span ids, integer enums, int64 as strings."""
    for span in fixture_spans:
        assert re.fullmatch(r"[0-9a-f]{32}", span["traceId"]), span["name"]
        assert re.fullmatch(r"[0-9a-f]{16}", span["spanId"]), span["name"]
        assert re.fullmatch(r"[0-9a-f]{16}", span.get("parentSpanId", "0" * 16)), span["name"]
        assert span["kind"] == 1 and span["status"] == {"code": 1}, span["name"]
        assert int(span["startTimeUnixNano"]) < int(span["endTimeUnixNano"]), span["name"]
        for item in span["attributes"]:
            if "intValue" in item["value"]:
                assert isinstance(item["value"]["intValue"], str), item["key"]


def test_fixture_is_one_trace_with_a_tool_and_an_llm_child(
    fixture_spans: list[dict[str, Any]],
) -> None:
    _assert_run_tree(fixture_spans)


def test_fixture_carries_the_model_provider_and_token_keys(
    fixture_spans: list[dict[str, Any]],
) -> None:
    named = {name: _attrs(span) for name, span in _by_name(fixture_spans).items()}
    llm = named[LLM]
    assert llm["gen_ai.request.model"] == "gpt-4o-mini"
    assert llm["gen_ai.system"] == "openai"
    assert (
        llm["gen_ai.usage.input_tokens"],
        llm["gen_ai.usage.output_tokens"],
        llm["gen_ai.usage.total_tokens"],
    ) == (21, 9, 30)
    assert named[TOOL]["gen_ai.tool.name"] == TOOL
    # LangSmith's default system for runs without a model name.
    assert named[ROOT]["gen_ai.system"] == named[TOOL]["gen_ai.system"] == "langchain"
    for name in (ROOT, TOOL):
        assert not [key for key in named[name] if key.startswith("gen_ai.usage.")], name
        assert "gen_ai.request.model" not in named[name], name


def test_fixture_carries_content_and_no_secret(fixture_body: dict[str, Any]) -> None:
    dump = json.dumps(fixture_body)
    for marker in CONTENT_MARKERS:
        assert marker in dump, marker
    for secret in (FI_API_KEY, FI_SECRET_KEY):
        assert secret not in dump


def test_readme_span_table_matches_the_fixture(fixture_spans: list[dict[str, Any]]) -> None:
    rows = _readme_span_rows()
    named = _by_name(fixture_spans)
    assert set(rows) == set(named)
    by_id = {span["spanId"]: name for name, span in named.items()}
    for name, (parent, kind, operation, _fi_type) in rows.items():
        attrs = _attrs(named[name])
        assert parent == by_id.get(named[name].get("parentSpanId", ""), "none"), name
        assert (kind, operation) == (attrs["langsmith.span.kind"], attrs["gen_ai.operation.name"]), name


# --------------------------------------------------------------------------
# SDK half: src/app.py with langsmith 0.14.4, loopback only, no key.
# --------------------------------------------------------------------------


class _LangSmithStub:
    """Loopback stand-in for LangSmith's REST API: answers 200 ``{}`` to anything
    and records each request's method and path."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length:
                    self.rfile.read(length)
                with lock:
                    owner.calls.append((self.command, urlsplit(self.path).path))
                body = b"{}"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PATCH = _answer  # noqa: N815

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = "http://127.0.0.1:{0}".format(self._server.server_port)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def __enter__(self) -> "_LangSmithStub":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


def _child_env(home: Path, guard_log: Path, otlp_origin: str, **overrides: Optional[str]) -> dict[str, str]:
    """The recipe's environment, built from scratch (nothing inherited but PATH)."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "LOOPBACK_GUARD_LOG": str(guard_log),
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_OTEL_ENABLED": "true",
        "LANGSMITH_OTEL_ONLY": "true",
        "LANGCHAIN_REVISION_ID": REVISION,
        "OTEL_EXPORTER_OTLP_ENDPOINT": otlp_origin,
        "OTEL_EXPORTER_OTLP_HEADERS": otlp_headers(FI_API_KEY, FI_SECRET_KEY),
        "FI_PROJECT_NAME": PROJECT,
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
    langsmith_endpoint: bool = False,
    receiver_env: Optional[Callable[[Receiver], dict[str, Optional[str]]]] = None,
    **overrides: Optional[str],
) -> dict[str, Any]:
    """Run one script under the loopback guard; return everything the tests read.

    With ``langsmith_endpoint`` the run's LANGSMITH_ENDPOINT is a loopback
    stub, so a REST call to LangSmith is recorded instead of refused.
    ``receiver_env`` adds overrides that need the receiver's address.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    guard_log = tmp_path / "guard.jsonl"
    with Receiver() as receiver, _LangSmithStub() as stub:
        if langsmith_endpoint:
            overrides["LANGSMITH_ENDPOINT"] = stub.origin
        if receiver_env is not None:
            overrides.update(receiver_env(receiver))
        env = _child_env(tmp_path, guard_log, receiver.origin, **overrides)
        assert not [name for name in env if name in LANGSMITH_KEY_VARS]
        result = run(
            [sys.executable, str(GUARD), str(script), QUESTION],
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
            "langsmith_calls": list(stub.calls),
        }
    record["guard_attempts"] = (
        [json.loads(line) for line in guard_log.read_text().splitlines()]
        if guard_log.exists()
        else []
    )
    return record


def _assert_ran(record: dict[str, Any], exported: bool = True) -> None:
    result = record["result"]
    assert not result.timed_out, record["stderr"]
    assert result.returncode == 0, record["stderr"]
    assert record["guard_attempts"] == [], record["guard_attempts"]
    assert ANSWER in record["stdout"]
    if exported:
        assert record["requests"], "no export reached the receiver"
        assert len(record["spans"]) == 3, [span["name"] for span in record["spans"]]


def _export_dump(record: dict[str, Any]) -> str:
    """Everything the export carried except HTTP headers: spans and resources."""
    return json.dumps(
        {"spans": record["spans"], "resources": [r["resource_attributes"] for r in record["requests"]]},
        sort_keys=True,
    )


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


@needs_sdk
def test_installed_langsmith_is_the_pinned_version() -> None:
    assert version("langsmith") == PINNED_LANGSMITH


@pytest.fixture(scope="module")
def recipe_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    if not HAS_LANGSMITH:
        pytest.skip("langsmith is not installed")
    record = _run_script(tmp_path_factory.mktemp("recipe"), APP)
    _assert_ran(record)
    return record


@needs_sdk
def test_recipe_runs_with_loopback_network_only(recipe_run: dict[str, Any]) -> None:
    # LANGSMITH_ENDPOINT is unset, so any LangSmith call would have gone to
    # api.smith.langchain.com and been refused and logged by the guard.
    assert recipe_run["guard_attempts"] == []
    assert recipe_run["langsmith_calls"] == []


@needs_sdk
def test_sdk_export_matches_the_fixture(
    recipe_run: dict[str, Any], fixture_spans: list[dict[str, Any]], tmp_path: Path
) -> None:
    golden = _write_golden(tmp_path, fixture_spans)
    compare(_as_otlp_json(recipe_run["spans"]), golden)
    _assert_run_tree(recipe_run["spans"])
    for span in _as_otlp_json(recipe_run["spans"]):
        assert span["kind"] == 1, span["name"]
        assert not span.get("events"), span["name"]


@needs_sdk
def test_export_path_headers_and_resource(recipe_run: dict[str, Any]) -> None:
    assert recipe_run["requests"], "no export reached the receiver"
    for request in recipe_run["requests"]:
        assert request["path"] == "/v1/traces"
        headers = request["headers"]
        assert headers.get("x-api-key") == FI_API_KEY
        assert headers.get("x-secret-key") == FI_SECRET_KEY
        assert "authorization" not in headers
        assert "langsmith-project" not in headers
        assert request["resource_attributes"], "export without a resource"
        for resource in request["resource_attributes"]:
            assert resource.get("project_name") == PROJECT
            assert resource.get("project_type") == "observe"


@needs_sdk
def test_content_is_exported_by_default(recipe_run: dict[str, Any]) -> None:
    """LangSmith's default: inputs and outputs on every span. Control for the hide test."""
    named = {name: _attrs(span) for name, span in _by_name(recipe_run["spans"]).items()}
    assert "QMARK7c1e" in named[ROOT]["gen_ai.prompt"]
    assert ANSWER in named[ROOT]["gen_ai.completion"]
    assert POLICY in named[TOOL]["gen_ai.completion"]
    assert POLICY in named[LLM]["gen_ai.prompt"]  # the system prompt
    assert "QMARK7c1e" in named[LLM]["gen_ai.prompt"]
    assert ANSWER in named[LLM]["gen_ai.completion"]


@needs_sdk
def test_no_secret_in_the_export_or_output(recipe_run: dict[str, Any]) -> None:
    dump = _export_dump(recipe_run)
    output = recipe_run["stdout"] + recipe_run["stderr"]
    for secret in (FI_API_KEY, FI_SECRET_KEY, quote(FI_SECRET_KEY, safe="")):
        assert secret not in dump
        assert secret not in output


@needs_sdk
@pytest.mark.parametrize(
    ("mode", "overrides", "expected_calls"),
    [
        # The recipe: OTEL only. The LangSmith endpoint is reachable and unused.
        ("otel_only", {}, set()),
        # The same mode under its 0.14.4 name.
        (
            "tracing_mode_otel",
            {"LANGSMITH_TRACING_MODE": "otel", "LANGSMITH_OTEL_ENABLED": None, "LANGSMITH_OTEL_ONLY": None},
            set(),
        ),
        # The official page's two flags: hybrid, LangSmith's REST API as well.
        ("hybrid", {"LANGSMITH_OTEL_ONLY": None}, {("GET", "/info"), ("POST", "/runs/multipart")}),
    ],
)
def test_which_modes_call_langsmith(
    tmp_path: Path, mode: str, overrides: dict[str, Optional[str]], expected_calls: set[tuple[str, str]]
) -> None:
    record = _run_script(tmp_path, APP, langsmith_endpoint=True, **overrides)
    _assert_ran(record)
    assert set(record["langsmith_calls"]) == expected_calls, mode
    _assert_run_tree(record["spans"])


@needs_sdk
def test_tracing_unset_exports_nothing(tmp_path: Path) -> None:
    """LANGSMITH_TRACING is the on switch; the OTEL variables alone trace nothing."""
    record = _run_script(tmp_path, APP, LANGSMITH_TRACING=None)
    _assert_ran(record, exported=False)
    assert record["requests"] == []
    assert record["spans"] == []


@needs_sdk
def test_hide_inputs_and_outputs_drop_content_and_token_counts(tmp_path: Path) -> None:
    record = _run_script(tmp_path, APP, LANGSMITH_HIDE_INPUTS="true", LANGSMITH_HIDE_OUTPUTS="true")
    _assert_ran(record)
    dump = _export_dump(record)
    for marker in CONTENT_MARKERS:
        assert marker not in dump, marker
    named = {name: _attrs(span) for name, span in _by_name(record["spans"]).items()}
    for name, attrs in named.items():
        assert attrs["gen_ai.prompt"] == "{}", name
        assert attrs["gen_ai.completion"] == "{}", name
    llm = named[LLM]
    # The token counts come from the outputs, so they go with them...
    assert not [key for key in llm if key.startswith("gen_ai.usage.")]
    assert "gen_ai.response.finish_reasons" not in llm
    # ...while the model name (metadata) and the metadata copy of the usage stay.
    assert llm["gen_ai.request.model"] == "gpt-4o-mini"
    assert json.loads(llm["langsmith.metadata.usage_metadata"])["total_tokens"] == 30


@needs_sdk
def test_traces_endpoint_is_used_as_given(tmp_path: Path) -> None:
    """OTEL_EXPORTER_OTLP_TRACES_ENDPOINT is a full URL: fi-collector's /tracer path works."""
    record = _run_script(
        tmp_path,
        APP,
        receiver_env=lambda receiver: {
            "OTEL_EXPORTER_OTLP_ENDPOINT": None,
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": receiver.collector_endpoint,
        },
    )
    _assert_ran(record)
    assert {request["path"] for request in record["requests"]} == {"/tracer/v1/traces"}


@needs_sdk
def test_langsmith_own_provider_has_no_project_and_sends_its_own_headers(tmp_path: Path) -> None:
    """Why init_tracing() exists: without it, fi-collector would reject the batch."""
    record = _run_script(
        tmp_path,
        SDK_OWN_PROVIDER,
        OTEL_RESOURCE_ATTRIBUTES="project_name={0},project_type=observe".format(PROJECT),
        OTEL_EXPORTER_OTLP_HEADERS=None,
    )
    _assert_ran(record)
    _assert_run_tree(record["spans"])
    for request in record["requests"]:
        assert request["path"] == "/v1/traces"
        # OTEL_RESOURCE_ATTRIBUTES is not read; there is no project_name.
        assert request["resource_attributes"] == [
            {"service.name": "langsmith", "langsmith.internal_provider": True}
        ]
        # LangSmith's key header (empty: no key is set) and project header.
        assert request["headers"].get("x-api-key") == ""
        assert request["headers"].get("langsmith-project") == "default"
        assert "x-secret-key" not in request["headers"]


@needs_sdk
def test_readme_run_type_table_matches_the_sdk() -> None:
    from typing import get_args

    from langsmith._internal.otel._otel_exporter import _get_operation_name
    from langsmith.client import RUN_TYPE_T

    rows = _readme_run_type_rows()
    assert set(rows) == set(get_args(RUN_TYPE_T))
    for run_type, (operation, _fi_type) in rows.items():
        assert operation == _get_operation_name(run_type), run_type


# --------------------------------------------------------------------------
# Opt-in: read fi-collector's span-kind and alias rules instead of copying them.
# --------------------------------------------------------------------------


def _go_block(text: str, name: str) -> str:
    start = text.index(name + " = ")
    opening = text.index("{\n", start)
    closing = re.compile(r"\n\t?\}").search(text, opening)
    assert closing is not None, name
    return text[opening + 2 : closing.start()]


def _collector_rules() -> dict[str, Any]:
    root = os.environ.get("FI_COLLECTOR_SRC")
    if not root:
        pytest.skip("set FI_COLLECTOR_SRC to a fi-collector checkout")
    converter = (Path(root) / "exporter" / "clickhouse25exporter" / "converter.go").read_text(encoding="utf-8")
    adapter = (Path(root) / "pkg" / "adapter" / "adapter.go").read_text(encoding="utf-8")
    quoted = re.compile(r'"([^"]+)"')
    rules: dict[str, Any] = {
        "known": set(re.findall(r'"([^"]+)":\s*\{\}', _go_block(converter, "knownObservationTypes"))),
        "kind_keys": quoted.findall(_go_block(converter, "spanKindAttrKeys")),
        "operation_keys": quoted.findall(_go_block(converter, "operationNameAttrKeys")),
        "synonyms": dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', _go_block(converter, "spanKindSynonyms"))),
    }
    for name in ("modelNameKeys", "providerKeys", "inputTokenKeys", "outputTokenKeys", "totalTokenKeys"):
        rules[name] = quoted.findall(_go_block(adapter, name))
    assert tuple(rules["kind_keys"]) == SPAN_KIND_KEYS
    return rules


def _collector_type(attrs: dict[str, Any], rules: dict[str, Any]) -> str:
    """resolveObservationType (converter.go), applied to rules read from the source."""
    raw = next((str(attrs[k]) for k in rules["kind_keys"] if attrs.get(k)), "")
    if not raw:
        raw = next((str(attrs[k]) for k in rules["operation_keys"] if attrs.get(k)), "")
    kind = raw.strip().lower()
    kind = rules["synonyms"].get(kind, kind)
    return kind if kind in rules["known"] else "unknown"


def _first(attrs: dict[str, Any], keys: list[str]) -> Any:
    return next((attrs[k] for k in keys if attrs.get(k) not in (None, "")), None)


def test_collector_types_and_hot_keys_for_the_fixture(fixture_spans: list[dict[str, Any]]) -> None:
    rules = _collector_rules()
    rows = _readme_span_rows()
    for name, span in _by_name(fixture_spans).items():
        attrs = _attrs(span)
        assert _collector_type(attrs, rules) == rows[name][3] == SDK_KINDS[name], name
    llm = _attrs(_by_name(fixture_spans)[LLM])
    assert _first(llm, rules["modelNameKeys"]) == "gpt-4o-mini"
    assert _first(llm, rules["providerKeys"]) == "openai"
    assert _first(llm, rules["inputTokenKeys"]) == 21
    assert _first(llm, rules["outputTokenKeys"]) == 9
    assert _first(llm, rules["totalTokenKeys"]) == 30
    for name in (ROOT, TOOL):
        attrs = _attrs(_by_name(fixture_spans)[name])
        assert _first(attrs, rules["modelNameKeys"]) is None, name
        assert _first(attrs, rules["providerKeys"]) == "langchain", name


def test_readme_run_type_table_matches_the_collector() -> None:
    rules = _collector_rules()
    for run_type, (operation, fi_type) in _readme_run_type_rows().items():
        assert _collector_type({"gen_ai.operation.name": operation}, rules) == fi_type, run_type
