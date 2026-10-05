"""Contract test for the MLflow OTLP recipe.

Runs ``src/app.py`` as written with the real ``mlflow`` 3.16.1, in a
subprocess whose network is limited to 127.0.0.1 (``_guarded_run.py``) and
whose working directory and HOME are a fresh temporary directory, so every
file MLflow writes is visible to the test. Spans go to the shared harness
``Receiver``, which serves ``/v1/traces`` and ``/tracer/v1/traces`` like
fi-collector's HTTP mux but does not authenticate, stamp projects or store
anything. There is no model call: the app's model client is a stand-in.

All keys are placeholders. Nothing here contacts an MLflow tracking server,
Databricks, a model provider or Future AGI.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import sqlite3
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from urllib.parse import quote, urlsplit

import pytest

from harness import Receiver, post_otlp, run

TESTS_DIR = Path(__file__).resolve().parent
RECIPE_DIR = TESTS_DIR.parent
APP = RECIPE_DIR / "src" / "app.py"
README = RECIPE_DIR / "README.md"
BARE_SPAN = TESTS_DIR / "bare_span.py"
SET_DESTINATION = TESTS_DIR / "set_destination_app.py"
GUARD = TESTS_DIR / "_guarded_run.py"
GUARD_PROBE = TESTS_DIR / "_guard_probe.py"
GEN_AI_FIXTURE = TESTS_DIR / "fixtures" / "gen_ai_span.otlp.json"

# A comma and a space: the project name must be percent-encoded to arrive.
PROJECT = "MLflow recipe, contract"
SERVICE_NAME = "mlflow-recipe"
FI_API_KEY = "fi-api-placeholder-0000"
# A comma and an equals sign: the recipe's percent-encoding must carry them.
FI_SECRET_KEY = "fi-secret-placeholder,part=0000"

# Content markers: the question (input), the system prompt built from the
# policy (input) and the stand-in model's answer (output).
QUESTION = "QMARK7c1e what is the refund window?"
POLICY = "Refunds are accepted within 30 days of purchase."
ANSWER = "Refunds are accepted for 30 days."  # src/app.py call_model()
CONTENT_MARKERS = ("QMARK7c1e", POLICY, ANSWER)

ROOT = "answer_question"
LLM = "chat_model"
MODEL = "gpt-4o-mini"
# The keys MLflow 3.16.1 puts on each span of src/app.py by default.
DEFAULT_KEYS = {
    ROOT: {
        "mlflow.traceRequestId",
        "mlflow.spanType",
        "mlflow.spanInputs",
        "mlflow.spanOutputs",
        "mlflow.spanLogLevel",
    },
    LLM: {
        "mlflow.traceRequestId",
        "mlflow.spanType",
        "mlflow.spanInputs",
        "mlflow.spanOutputs",
        "mlflow.spanLogLevel",
        "mlflow.llm.model",
        "mlflow.llm.provider",
        "mlflow.chat.tokenUsage",
        "gen_ai.request.model",
        "gen_ai.provider.name",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
    },
}
# A hang guard, not a speed check: importing mlflow takes a few seconds.
RUN_TIMEOUT_SECONDS = 180


def otlp_headers(api_key: str, secret_key: str) -> str:
    """Build OTEL_EXPORTER_OTLP_TRACES_HEADERS exactly as README.md tells users to."""
    return "x-api-key={0},x-secret-key={1}".format(
        quote(api_key, safe=""), quote(secret_key, safe="")
    )


def resource_attributes(project: str) -> str:
    """Build OTEL_RESOURCE_ATTRIBUTES, percent-encoding the project name as README.md says."""
    return "project_name={0},project_type=observe".format(quote(project, safe=""))


class _Recorder:
    """Loopback catch-all that records every request's method and path.

    It answers POST /v1/traces and /tracer/v1/traces with 200, the two trace
    routes fi-collector serves (pkg/server/server.go:233-234), and anything
    else with 404.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                path = urlsplit(self.path).path
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                with lock:
                    owner.calls.append((self.command, path))
                ok = self.command == "POST" and path in ("/v1/traces", "/tracer/v1/traces")
                self.send_response(HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = _answer  # noqa: N815

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = "http://127.0.0.1:{0}".format(self._server.server_port)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def __enter__(self) -> "_Recorder":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


@contextlib.contextmanager
def _working_directory(path: Path) -> Iterator[None]:
    # harness.run() has no cwd argument; MLflow resolves its default store
    # (./mlflow.db) against the working directory, so the child inherits ours.
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _child_env(work: Path, guard_log: Path, endpoint: str, **overrides: Optional[str]) -> dict[str, str]:
    """The recipe's environment, built from scratch (nothing inherited but PATH)."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(work),
        "LOOPBACK_GUARD_LOG": str(guard_log),
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": endpoint,
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS": otlp_headers(FI_API_KEY, FI_SECRET_KEY),
        "OTEL_RESOURCE_ATTRIBUTES": resource_attributes(PROJECT),
        "OTEL_SERVICE_NAME": SERVICE_NAME,
        "MLFLOW_DISABLE_TELEMETRY": "true",
    }
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _execute(
    tmp_path: Path,
    script: Path,
    args: list[str],
    endpoint: str,
    **overrides: Optional[str],
) -> dict[str, Any]:
    """Run one script under the loopback guard in tmp_path/work; return what the tests read."""
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    guard_log = tmp_path / "guard.jsonl"
    env = _child_env(work, guard_log, endpoint, **overrides)
    with _working_directory(work):
        result = run(
            [sys.executable, str(GUARD), str(script), *args],
            env=env,
            stdin=None,
            timeout=RUN_TIMEOUT_SECONDS,
        )
    return {
        "result": result,
        "stdout": result.stdout.decode("utf-8", "replace"),
        "stderr": result.stderr.decode("utf-8", "replace"),
        "work": work,
        "files": sorted(p.relative_to(work).as_posix() for p in work.rglob("*") if p.is_file()),
        "guard_attempts": (
            [json.loads(line) for line in guard_log.read_text().splitlines()]
            if guard_log.exists()
            else []
        ),
    }


def _run_script(
    tmp_path: Path,
    script: Path,
    args: list[str],
    endpoint_for: Callable[[Receiver], str] = lambda receiver: receiver.collector_endpoint,
    **overrides: Optional[str],
) -> dict[str, Any]:
    """Run a script against a harness Receiver."""
    with Receiver() as receiver:
        record = _execute(tmp_path, script, args, endpoint_for(receiver), **overrides)
        record["requests"] = receiver.requests()
        record["spans"] = receiver.spans()
    return record


def _run_recorded(
    tmp_path: Path,
    script: Path,
    args: list[str],
    endpoint_for: Callable[[_Recorder], str],
    **overrides: Optional[str],
) -> dict[str, Any]:
    """Run a script against the catch-all recorder, which sees every request."""
    with _Recorder() as recorder:
        record = _execute(tmp_path, script, args, endpoint_for(recorder), **overrides)
        record["calls"] = list(recorder.calls)
    return record


def _value(value: dict[str, Any]) -> Any:
    for kind in ("stringValue", "boolValue", "doubleValue"):
        if kind in value:
            return value[kind]
    if "intValue" in value:
        return int(value["intValue"])
    return value


def _raw(span: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Attribute key -> OTLP value object, e.g. {"stringValue": "..."}."""
    return {item["key"]: item.get("value", {}) for item in span.get("attributes", [])}


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    return {key: _value(value) for key, value in _raw(span).items()}


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


def _store(path: Path) -> dict[str, Any]:
    """The trace ids and span names in an MLflow SQLite store."""
    connection = sqlite3.connect(str(path))
    try:
        return {
            "traces": [row[0] for row in connection.execute("select request_id from trace_info")],
            "spans": sorted(row[0] for row in connection.execute("select name from spans")),
        }
    finally:
        connection.close()


def _trace_request_id(record: dict[str, Any]) -> str:
    ids = {json.loads(_attrs(span)["mlflow.traceRequestId"]) for span in record["spans"]}
    assert len(ids) == 1, ids
    return ids.pop()


def _assert_exited_cleanly(record: dict[str, Any]) -> None:
    result = record["result"]
    assert not result.timed_out, record["stderr"]
    assert result.returncode == 0, record["stderr"]
    assert record["guard_attempts"] == [], record["guard_attempts"]


def _assert_ran(record: dict[str, Any]) -> None:
    _assert_exited_cleanly(record)
    assert ANSWER in record["stdout"]
    assert record["requests"], "no export reached the receiver"
    assert record["spans"], "the export carried no spans"


# --------------------------------------------------------------------------
# The recipe as documented: full /tracer/v1/traces URL, http/protobuf,
# percent-encoded headers and resource attributes, telemetry off.
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def recipe_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    record = _run_script(tmp_path_factory.mktemp("recipe"), APP, [QUESTION])
    _assert_ran(record)
    return record


def test_recipe_runs_with_loopback_network_only_and_writes_no_file(
    recipe_run: dict[str, Any],
) -> None:
    assert recipe_run["guard_attempts"] == []
    assert ANSWER in recipe_run["stdout"]
    # No ./mlflow.db, no ./mlruns, no ~/.config/mlflow: OTLP only.
    assert recipe_run["files"] == []


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


def test_traces_endpoint_is_posted_to_as_is(recipe_run: dict[str, Any]) -> None:
    assert {request["path"] for request in recipe_run["requests"]} == {"/tracer/v1/traces"}


def test_headers_carry_the_future_agi_keys(recipe_run: dict[str, Any]) -> None:
    for request in recipe_run["requests"]:
        headers = request["headers"]
        assert headers.get("x-api-key") == FI_API_KEY
        assert headers.get("x-secret-key") == FI_SECRET_KEY
        assert headers.get("content-type") == "application/x-protobuf"
        # No MLflow tracking token or Basic auth on the export.
        assert "authorization" not in headers


def test_resource_carries_the_project(recipe_run: dict[str, Any]) -> None:
    for request in recipe_run["requests"]:
        assert request["resource_attributes"], "export without a resource"
        for resource in request["resource_attributes"]:
            assert resource.get("project_name") == PROJECT
            assert resource.get("project_type") == "observe"
            assert resource.get("service.name") == SERVICE_NAME
            assert resource.get("telemetry.sdk.name") == "mlflow"
            assert resource.get("telemetry.sdk.version") == "3.16.1"


def test_span_tree(recipe_run: dict[str, Any]) -> None:
    spans = _by_name(recipe_run["spans"])
    assert set(spans) == {ROOT, LLM}
    assert len({span["traceId"] for span in spans.values()}) == 1
    assert not spans[ROOT].get("parentSpanId")
    assert spans[LLM]["parentSpanId"] == spans[ROOT]["spanId"]
    for span in spans.values():
        assert span["kind"] == "SPAN_KIND_INTERNAL"
        assert span["status"] == {"code": "STATUS_CODE_OK"}
        assert not span.get("events")


def test_mlflow_json_encodes_every_attribute_value(recipe_run: dict[str, Any]) -> None:
    spans = _by_name(recipe_run["spans"])
    assert {name: set(_raw(span)) for name, span in spans.items()} == DEFAULT_KEYS
    for span in spans.values():
        for key, value in _raw(span).items():
            assert set(value) == {"stringValue"}, (key, value)
            json.loads(value["stringValue"])
    llm = _raw(spans[LLM])
    # MLflow lets the app set the gen_ai.* keys, but encodes the values.
    assert llm["gen_ai.request.model"] == {"stringValue": '"gpt-4o-mini"'}
    assert llm["gen_ai.provider.name"] == {"stringValue": '"openai"'}
    assert llm["gen_ai.usage.input_tokens"] == {"stringValue": "12"}
    assert llm["gen_ai.usage.output_tokens"] == {"stringValue": "7"}
    assert llm["mlflow.spanType"] == {"stringValue": '"CHAT_MODEL"'}
    assert json.loads(llm["mlflow.chat.tokenUsage"]["stringValue"]) == {
        "input_tokens": 12,
        "output_tokens": 7,
        "total_tokens": 19,
    }
    assert _raw(spans[ROOT])["mlflow.spanType"] == {"stringValue": '"CHAIN"'}
    assert _trace_request_id(recipe_run).startswith("tr-")


def test_readme_lists_every_exported_key(recipe_run: dict[str, Any]) -> None:
    text = README.read_text(encoding="utf-8")
    keys = set().union(*DEFAULT_KEYS.values())
    for request in recipe_run["requests"]:
        for resource in request["resource_attributes"]:
            keys |= set(resource)
    missing = sorted(key for key in keys if "`{0}`".format(key) not in text)
    assert missing == []


def test_no_experiment_or_session_on_the_export(recipe_run: dict[str, Any]) -> None:
    for span in recipe_run["spans"]:
        keys = set(_attrs(span))
        assert not [key for key in keys if "experiment" in key.lower()]
        assert not keys & {"session.id", "user.id"}
    for request in recipe_run["requests"]:
        for resource in request["resource_attributes"]:
            assert not [key for key in resource if "experiment" in key.lower()]


def test_content_is_exported_by_default(recipe_run: dict[str, Any]) -> None:
    spans = {name: _attrs(span) for name, span in _by_name(recipe_run["spans"]).items()}
    assert "QMARK7c1e" in spans[ROOT]["mlflow.spanInputs"]
    assert ANSWER in spans[ROOT]["mlflow.spanOutputs"]
    assert POLICY in spans[LLM]["mlflow.spanInputs"]  # the system prompt
    assert "QMARK7c1e" in spans[LLM]["mlflow.spanInputs"]
    assert ANSWER in spans[LLM]["mlflow.spanOutputs"]
    # Only those two keys carry content.
    for name, attrs in spans.items():
        for key, value in attrs.items():
            if key in ("mlflow.spanInputs", "mlflow.spanOutputs"):
                continue
            for marker in CONTENT_MARKERS:
                assert marker not in str(value), (name, key)


def test_no_secret_in_the_export_or_output(recipe_run: dict[str, Any]) -> None:
    dump = _export_dump(recipe_run)
    output = recipe_run["stdout"] + recipe_run["stderr"]
    for secret in (FI_API_KEY, FI_SECRET_KEY, quote(FI_SECRET_KEY, safe="")):
        assert secret not in dump
        assert secret not in output


# --------------------------------------------------------------------------
# Content: MLflow has no switch; its span-processor hook is the way.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("semconv", [None, "true"])
def test_drop_content_processor_removes_inputs_and_outputs(
    tmp_path: Path, semconv: Optional[str]
) -> None:
    record = _run_script(
        tmp_path,
        APP,
        ["--drop-content", QUESTION],
        MLFLOW_ENABLE_OTEL_GENAI_SEMCONV=semconv,
    )
    _assert_ran(record)
    dump = _export_dump(record)
    for marker in CONTENT_MARKERS:
        assert marker not in dump, marker
    assert len(record["spans"]) == 2
    for span in record["spans"]:
        attrs = _attrs(span)
        if semconv is None:
            assert attrs["mlflow.spanInputs"] == '"[REDACTED]"'
            assert attrs["mlflow.spanOutputs"] == '"[REDACTED]"'
        else:
            assert "gen_ai.input.messages" not in attrs
            assert "gen_ai.output.messages" not in attrs
            assert "gen_ai.system_instructions" not in attrs


# --------------------------------------------------------------------------
# Endpoint, protocol and the settings src/app.py refuses.
# --------------------------------------------------------------------------


def test_v1_traces_is_accepted_too(tmp_path: Path) -> None:
    record = _run_script(tmp_path, APP, [QUESTION], lambda receiver: receiver.endpoint)
    _assert_ran(record)
    assert {request["path"] for request in record["requests"]} == {"/v1/traces"}
    assert {span["name"] for span in record["spans"]} == {ROOT, LLM}


@pytest.mark.parametrize(
    ("suffix", "posted_to"),
    [("", "/"), ("/", "/"), ("/tracer", "/tracer")],
)
def test_mlflow_does_not_append_v1_traces(tmp_path: Path, suffix: str, posted_to: str) -> None:
    record = _run_recorded(
        tmp_path, BARE_SPAN, [QUESTION], lambda recorder: recorder.origin + suffix
    )
    _assert_exited_cleanly(record)
    assert record["calls"] == [("POST", posted_to)]
    assert "Failed to export spans batch code: 404" in record["stderr"]


def test_without_an_endpoint_mlflow_writes_a_local_mlflow_db(tmp_path: Path) -> None:
    record = _run_recorded(
        tmp_path,
        BARE_SPAN,
        [QUESTION],
        lambda recorder: recorder.origin + "/tracer/v1/traces",
        OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=None,
        MLFLOW_MODEL_CATALOG_URI="",
    )
    _assert_exited_cleanly(record)
    assert record["calls"] == []
    assert record["files"] == ["mlflow.db"]
    assert _store(record["work"] / "mlflow.db")["spans"] == [ROOT, LLM]


def test_without_the_protocol_nothing_is_exported_or_logged(tmp_path: Path) -> None:
    """MLflow defaults to gRPC; that exporter is not installed, so export is skipped."""
    record = _run_recorded(
        tmp_path,
        BARE_SPAN,
        [QUESTION],
        lambda recorder: recorder.origin + "/tracer/v1/traces",
        OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=None,
    )
    _assert_exited_cleanly(record)
    assert ANSWER in record["stdout"]
    assert record["calls"] == []
    assert record["stderr"] == ""
    assert record["files"] == []


@pytest.mark.parametrize(
    ("extra", "expected_calls", "expected_log"),
    [
        (
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "{origin}"},
            [],
            "No module named 'opentelemetry.exporter.otlp.proto.grpc'",
        ),
        (
            {"OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "{origin}/v1/metrics"},
            [],
            "No module named 'opentelemetry.exporter.otlp.proto.grpc'",
        ),
        (
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "{origin}", "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf"},
            [("POST", "/tracer/v1/traces"), ("POST", "/v1/metrics")],
            "Failed to export metrics batch code: 404",
        ),
    ],
    ids=["base-endpoint", "metrics-endpoint", "base-endpoint-http"],
)
def test_a_metrics_endpoint_turns_on_metrics_export(
    tmp_path: Path, extra: dict[str, str], expected_calls: list[tuple[str, str]], expected_log: str
) -> None:
    with _Recorder() as recorder:
        overrides = {key: value.format(origin=recorder.origin) for key, value in extra.items()}
        record = _execute(
            tmp_path, BARE_SPAN, [QUESTION], recorder.origin + "/tracer/v1/traces", **overrides
        )
        calls = sorted(recorder.calls)
    _assert_exited_cleanly(record)
    assert calls == sorted(expected_calls)
    assert expected_log in record["stderr"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": None}, "full traces URL"),
        ({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://127.0.0.1:9"}, "full traces URL"),
        ({"OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": None}, "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf"),
        ({"OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "grpc"}, "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf"),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9"}, "Unset OTEL_EXPORTER_OTLP_ENDPOINT"),
        (
            {"OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "http://127.0.0.1:9/v1/metrics"},
            "Unset OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        ),
        ({"OTEL_RESOURCE_ATTRIBUTES": None}, "Add project_name="),
        ({"OTEL_RESOURCE_ATTRIBUTES": "project_type=observe"}, "Add project_name="),
        ({"OTEL_RESOURCE_ATTRIBUTES": "project_name=a,project_type=observe,"}, "key=value pairs"),
    ],
    ids=[
        "no-endpoint",
        "origin-only",
        "no-protocol",
        "grpc",
        "base-endpoint",
        "metrics-endpoint",
        "no-resource",
        "no-project",
        "trailing-comma",
    ],
)
def test_app_refuses_settings_that_export_nothing(
    tmp_path: Path, overrides: dict[str, Optional[str]], message: str
) -> None:
    record = _run_script(tmp_path, APP, [QUESTION], **overrides)
    assert not record["result"].timed_out
    assert record["result"].returncode != 0
    assert message in record["stderr"]
    assert record["requests"] == []
    assert record["guard_attempts"] == []
    assert record["files"] == []


def test_unencoded_comma_in_a_header_value_truncates_it(tmp_path: Path) -> None:
    """Why the recipe percent-encodes: the exporter splits the variable on commas."""
    raw = "x-api-key={0},x-secret-key={1}".format(FI_API_KEY, FI_SECRET_KEY)
    record = _run_script(
        tmp_path, APP, [QUESTION], OTEL_EXPORTER_OTLP_TRACES_HEADERS=raw
    )
    _assert_ran(record)
    for request in record["requests"]:
        assert request["headers"].get("x-api-key") == FI_API_KEY
        assert request["headers"].get("x-secret-key") == FI_SECRET_KEY.split(",")[0]


@pytest.mark.parametrize(
    "resource",
    [None, "project_name=mlflow-recipe,project_type=observe,"],
    ids=["unset", "trailing-comma"],
)
def test_resource_without_project_name(tmp_path: Path, resource: Optional[str]) -> None:
    """fi-collector rejects such a batch (pkg/auth/stamp.go); src/app.py refuses both."""
    record = _run_script(
        tmp_path,
        BARE_SPAN,
        [QUESTION],
        OTEL_RESOURCE_ATTRIBUTES=resource,
        OTEL_SERVICE_NAME=None,
    )
    _assert_ran(record)
    for request in record["requests"]:
        for attributes in request["resource_attributes"]:
            assert "project_name" not in attributes
            assert "project_type" not in attributes
            assert attributes.get("telemetry.sdk.name") == "mlflow"


def test_experiment_id_is_exported_only_as_a_resource_attribute(tmp_path: Path) -> None:
    record = _run_script(
        tmp_path,
        APP,
        [QUESTION],
        MLFLOW_EXPERIMENT_ID="42",
        OTEL_RESOURCE_ATTRIBUTES=resource_attributes(PROJECT) + ",mlflow.experiment_id=42",
    )
    _assert_ran(record)
    assert record["files"] == []
    for request in record["requests"]:
        for resource in request["resource_attributes"]:
            assert resource.get("mlflow.experiment_id") == "42"
            assert resource.get("project_name") == PROJECT
    for span in record["spans"]:
        keys = set(_attrs(span))
        # MLFLOW_EXPERIMENT_ID itself reaches no span, and no session is set.
        assert not [key for key in keys if "experiment" in key.lower()]
        assert "session.id" not in keys


# --------------------------------------------------------------------------
# Dual export and set_destination.
# --------------------------------------------------------------------------


def test_dual_export_false_is_otlp_only(tmp_path: Path) -> None:
    record = _run_script(
        tmp_path, APP, [QUESTION], MLFLOW_TRACE_ENABLE_OTLP_DUAL_EXPORT="false"
    )
    _assert_ran(record)
    assert {span["name"] for span in record["spans"]} == {ROOT, LLM}
    assert record["files"] == []


def test_dual_export_without_a_tracking_uri_writes_a_local_mlflow_db(tmp_path: Path) -> None:
    record = _run_script(
        tmp_path,
        APP,
        [QUESTION],
        MLFLOW_TRACE_ENABLE_OTLP_DUAL_EXPORT="true",
        MLFLOW_MODEL_CATALOG_URI="",
    )
    _assert_ran(record)
    assert {span["name"] for span in record["spans"]} == {ROOT, LLM}
    # MLflow's default store, SQLite in the working directory.
    assert record["files"] == ["mlflow.db"]
    store = _store(record["work"] / "mlflow.db")
    assert store == {"traces": [_trace_request_id(record)], "spans": [ROOT, LLM]}


def test_dual_export_with_a_tracking_uri_writes_there(tmp_path: Path) -> None:
    database = tmp_path / "work" / "traces.db"
    record = _run_script(
        tmp_path,
        APP,
        [QUESTION],
        MLFLOW_TRACE_ENABLE_OTLP_DUAL_EXPORT="true",
        MLFLOW_TRACKING_URI="sqlite:///" + database.as_posix(),  # four slashes: absolute
        MLFLOW_MODEL_CATALOG_URI="",
    )
    _assert_ran(record)
    assert {span["name"] for span in record["spans"]} == {ROOT, LLM}
    assert record["files"] == ["traces.db"]
    assert _store(database) == {"traces": [_trace_request_id(record)], "spans": [ROOT, LLM]}


def test_dual_export_fetches_the_model_catalog_unless_disabled(tmp_path: Path) -> None:
    """Writing to an MLflow store fetches MLflow's model catalog (default: github.com).

    The catalog URI is pointed at a loopback recorder, so the fetch is seen
    without any lookup of the real host.
    """
    with Receiver() as receiver, _Recorder() as catalog:
        record = _execute(
            tmp_path,
            APP,
            [QUESTION],
            receiver.collector_endpoint,
            MLFLOW_TRACE_ENABLE_OTLP_DUAL_EXPORT="true",
            MLFLOW_MODEL_CATALOG_URI=catalog.origin + "/catalog",
        )
        spans = receiver.spans()
        catalog_calls = list(catalog.calls)
    _assert_exited_cleanly(record)
    assert {span["name"] for span in spans} == {ROOT, LLM}
    assert catalog_calls
    assert set(catalog_calls) == {("GET", "/catalog/openai.json")}


def test_set_destination_silences_otlp(tmp_path: Path) -> None:
    record = _run_recorded(
        tmp_path,
        SET_DESTINATION,
        [QUESTION],
        lambda recorder: recorder.origin + "/tracer/v1/traces",
        MLFLOW_MODEL_CATALOG_URI="",
    )
    _assert_exited_cleanly(record)
    assert ANSWER in record["stdout"]
    # Not one request of any kind reached the OTLP endpoint ...
    assert record["calls"] == []
    # ... because the trace went to the destination: the default local store.
    assert record["files"] == ["mlflow.db"]
    store = _store(record["work"] / "mlflow.db")
    assert len(store["traces"]) == 1
    assert store["spans"] == [ROOT, LLM]


def test_set_destination_with_dual_export_sends_both(tmp_path: Path) -> None:
    record = _run_script(
        tmp_path,
        SET_DESTINATION,
        [QUESTION],
        MLFLOW_TRACE_ENABLE_OTLP_DUAL_EXPORT="true",
        MLFLOW_MODEL_CATALOG_URI="",
    )
    _assert_ran(record)
    assert {span["name"] for span in record["spans"]} == {ROOT, LLM}
    assert record["files"] == ["mlflow.db"]
    assert _store(record["work"] / "mlflow.db") == {
        "traces": [_trace_request_id(record)],
        "spans": [ROOT, LLM],
    }


@pytest.mark.parametrize("disabled", [None, "true"])
def test_telemetry_switch(tmp_path: Path, disabled: Optional[str]) -> None:
    record = _run_script(tmp_path, APP, [QUESTION], MLFLOW_DISABLE_TELEMETRY=disabled)
    _assert_ran(record)
    # With telemetry on, importing mlflow writes an installation id to HOME.
    expected = [] if disabled else [".config/mlflow/telemetry.json"]
    assert record["files"] == expected


# --------------------------------------------------------------------------
# MLFLOW_ENABLE_OTEL_GENAI_SEMCONV=true: MLflow's own GenAI translation.
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def semconv_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    record = _run_script(
        tmp_path_factory.mktemp("semconv"),
        APP,
        [QUESTION],
        MLFLOW_ENABLE_OTEL_GENAI_SEMCONV="true",
    )
    _assert_ran(record)
    return record


def test_semconv_exports_plain_gen_ai_values(semconv_run: dict[str, Any]) -> None:
    spans = _by_name(semconv_run["spans"])
    chat_name = "chat " + MODEL
    # The model span is renamed "<operation> <model>"; a span with a type but
    # no model is renamed to its type.
    assert set(spans) == {chat_name, "CHAIN"}
    chat, chain = spans[chat_name], spans["CHAIN"]
    assert chat["parentSpanId"] == chain["spanId"]
    assert chat["kind"] == "SPAN_KIND_CLIENT"
    assert chain["kind"] == "SPAN_KIND_INTERNAL"
    raw = _raw(chat)
    assert raw["gen_ai.operation.name"] == {"stringValue": "chat"}
    assert raw["gen_ai.request.model"] == {"stringValue": MODEL}
    assert raw["gen_ai.provider.name"] == {"stringValue": "openai"}
    assert raw["gen_ai.usage.input_tokens"] == {"intValue": "12"}
    assert raw["gen_ai.usage.output_tokens"] == {"intValue": "7"}
    assert set(raw) == {
        "gen_ai.operation.name",
        "gen_ai.request.model",
        "gen_ai.provider.name",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.system_instructions",
        "gen_ai.input.messages",
        "gen_ai.output.messages",
    }
    # Every mlflow.* key is dropped, the CHAIN span's inputs and outputs too.
    assert _raw(chain) == {"gen_ai.operation.name": {"stringValue": "CHAIN"}}


def test_semconv_moves_chat_content_to_gen_ai_messages(semconv_run: dict[str, Any]) -> None:
    attrs = _attrs(_by_name(semconv_run["spans"])["chat " + MODEL])
    assert "QMARK7c1e" in attrs["gen_ai.input.messages"]
    assert POLICY in attrs["gen_ai.system_instructions"]  # the system message
    assert POLICY not in attrs["gen_ai.input.messages"]
    assert ANSWER in attrs["gen_ai.output.messages"]
    dump = json.dumps(semconv_run["spans"])
    for marker in CONTENT_MARKERS:
        assert dump.count(marker) == 1, marker


def test_readme_lists_every_semconv_key(semconv_run: dict[str, Any]) -> None:
    text = README.read_text(encoding="utf-8")
    keys = set().union(*(set(_raw(span)) for span in semconv_run["spans"]))
    assert sorted(key for key in keys if "`{0}`".format(key) not in text) == []


# --------------------------------------------------------------------------
# The hand-built gen_ai.* fixture, posted with the harness post_otlp().
# --------------------------------------------------------------------------


def _fixture() -> dict[str, Any]:
    return json.loads(GEN_AI_FIXTURE.read_text(encoding="utf-8"))


def _fixture_spans() -> dict[str, dict[str, Any]]:
    payload = _fixture()
    return _by_name(payload["resourceSpans"][0]["scopeSpans"][0]["spans"])


def test_hand_built_gen_ai_span_arrives_with_keys_intact() -> None:
    payload = _fixture()
    with Receiver() as receiver:
        assert post_otlp(payload, receiver.collector_endpoint) == 200
        spans = receiver.spans()
        requests = receiver.requests()
    assert spans == payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert [request["path"] for request in requests] == ["/tracer/v1/traces"]
    assert requests[0]["resource_attributes"] == [
        {"project_name": "mlflow-recipe-contract", "project_type": "observe", "service.name": "mlflow-recipe"}
    ]
    arrived = _by_name(spans)
    plain = _raw(arrived["chat " + MODEL])
    assert plain["gen_ai.request.model"] == {"stringValue": MODEL}
    assert plain["gen_ai.usage.input_tokens"] == {"intValue": "12"}
    encoded = _raw(arrived[LLM])
    assert encoded["gen_ai.request.model"] == {"stringValue": '"gpt-4o-mini"'}
    assert encoded["gen_ai.usage.input_tokens"] == {"stringValue": "12"}


def test_fixture_spans_are_what_mlflow_sends(
    recipe_run: dict[str, Any], semconv_run: dict[str, Any]
) -> None:
    """The fixture is hand-built, but each span's keys and values are MLflow's own."""
    fixture = _fixture_spans()
    live = {
        "chat " + MODEL: _raw(_by_name(semconv_run["spans"])["chat " + MODEL]),
        LLM: _raw(_by_name(recipe_run["spans"])[LLM]),
    }
    for name, span in fixture.items():
        for key, value in _raw(span).items():
            assert live[name][key] == value, (name, key)


# --------------------------------------------------------------------------
# Opt-in: read fi-collector's alias tables instead of copying them.
# --------------------------------------------------------------------------


def _collector_source() -> tuple[str, str]:
    root = os.environ.get("FI_COLLECTOR_SRC")
    if not root:
        pytest.skip("set FI_COLLECTOR_SRC to a fi-collector checkout (the directory with pkg/)")
    adapter = Path(root) / "pkg" / "adapter" / "adapter.go"
    converter = Path(root) / "exporter" / "clickhouse25exporter" / "converter.go"
    return adapter.read_text(encoding="utf-8"), converter.read_text(encoding="utf-8")


def _go_string_lists(source: str) -> dict[str, list[str]]:
    lists = {}
    for name, body in re.findall(r"(\w+)\s*=\s*\[\]string\{(.*?)\}", source, flags=re.DOTALL):
        lists[name] = re.findall(r'"([^"]+)"', body)
    return lists


def _collector_keys() -> dict[str, Any]:
    adapter, converter = _collector_source()
    lists = {**_go_string_lists(adapter), **_go_string_lists(converter)}
    synonyms_body = re.search(
        r"spanKindSynonyms\s*=\s*map\[string\]string\{(.*?)\n\}", converter, flags=re.DOTALL
    ).group(1)
    known_body = re.search(
        r"knownObservationTypes\s*=\s*map\[string\]struct\{\}\{(.*?)\n\}", converter, flags=re.DOTALL
    ).group(1)
    span_constants = dict(re.findall(r'(attr\w+)\s*=\s*"([^"]+)"', converter))
    return {
        "lists": lists,
        "synonyms": dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', synonyms_body)),
        "known": set(re.findall(r'"([^"]+)":', known_body)),
        "columns": set(re.findall(r'overflowAsString\(overflow, "([^"]+)"\)', converter)),
        "span_constants": set(span_constants.values()),
    }


# The adapter.go / converter.go lists whose keys fi-collector reads off a span.
_ALIAS_LISTS = (
    "modelNameKeys",
    "providerKeys",
    "inputTokenKeys",
    "outputTokenKeys",
    "totalTokenKeys",
    "costTotalKeys",
    "costInputKeys",
    "costOutputKeys",
    "spanKindAttrKeys",
    "operationNameAttrKeys",
)


def _read_by_collector(keys: dict[str, Any]) -> set[str]:
    read = set().union(*(keys["lists"][name] for name in _ALIAS_LISTS))
    return read | keys["columns"] | keys["span_constants"]


def test_collector_reads_the_gen_ai_keys_and_no_mlflow_key() -> None:
    keys = _collector_keys()
    lists = keys["lists"]
    assert "gen_ai.request.model" in lists["modelNameKeys"]
    assert "gen_ai.provider.name" in lists["providerKeys"]
    assert "gen_ai.usage.input_tokens" in lists["inputTokenKeys"]
    assert "gen_ai.usage.output_tokens" in lists["outputTokenKeys"]
    assert lists["operationNameAttrKeys"] == ["gen_ai.operation.name"]
    assert not [key for key in _read_by_collector(keys) if key.startswith("mlflow.")]
    # How MLflow's GenAI operation names would be typed (lower-cased first).
    assert keys["synonyms"]["chat"] == "llm"
    assert "chain" in keys["known"]
    assert "invoke_agent" not in keys["synonyms"]
    assert keys["columns"] == {"input.value", "output.value"}


def test_readme_inventory_marks_what_the_collector_reads() -> None:
    read = _read_by_collector(_collector_keys())
    rows = re.findall(
        r"^\| `((?:mlflow|gen_ai)\.[\w.]+)` \|.*\| (.+?) \|$",
        README.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    assert rows, "no inventory rows found in README.md"
    claims: dict[str, set[bool]] = {}
    for key, last_cell in rows:
        claims.setdefault(key, set()).add(not last_cell.startswith("no"))
    assert {key: {key in read} for key in claims} == claims


def _mlflow_source(relative: str) -> str:
    """Read a file of the installed mlflow package without importing mlflow."""
    spec = importlib.util.find_spec("mlflow")
    assert spec is not None and spec.submodule_search_locations
    root = Path(next(iter(spec.submodule_search_locations)))
    return (root / relative).read_text(encoding="utf-8")


def test_readme_span_type_table_matches_mlflow_and_the_collector() -> None:
    keys = _collector_keys()
    span_module = _mlflow_source("entities/span.py")
    span_type_body = re.search(r"^class SpanType\b.*?:\n(.*?)\n\S", span_module, re.M | re.S).group(1)
    span_types = re.findall(r'^\s+([A-Z_]+) = "([A-Z_]+)"$', span_type_body, flags=re.MULTILINE)
    translator = _mlflow_source("tracing/export/genai_semconv/translator.py")
    operations = dict(re.findall(r'SpanType\.(\w+): "(\w+)"', translator))
    expected = {}
    for name, value in span_types:
        # translator.py passes a type with no GenAI name through as is.
        operation = operations.get(name, value)
        kind = operation.strip().lower()
        kind = keys["synonyms"].get(kind, kind)
        expected[value] = (operation, kind if kind in keys["known"] else "unknown")
    rows = re.findall(
        r"^\| `([A-Z_]+)` \| `(\w+)` \| ([a-z]+) \|$",
        README.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    assert {span_type: (operation, kind) for span_type, operation, kind in rows} == expected
