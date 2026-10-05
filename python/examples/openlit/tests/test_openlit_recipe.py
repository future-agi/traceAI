"""Contract test for the OpenLIT recipe.

Runs ``src/app.py`` as written with the real ``openlit`` 1.45.0 and a real
``openai`` client, in a subprocess whose network is limited to 127.0.0.1
(``_guarded_run.py``). The model is a loopback fake of the OpenAI Chat
Completions API; spans go to the shared harness ``Receiver``, which serves
``/v1/traces`` and ``/tracer/v1/traces`` like fi-collector's HTTP mux but
does not authenticate, stamp projects or store anything.

All keys are placeholders. Nothing here contacts OpenLIT, GitHub, OpenAI or
Future AGI.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
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
VARIANT = TESTS_DIR / "recipe_variant.py"
TRACED_FUNCTION = TESTS_DIR / "traced_function.py"
TOOL_CALL_SCRIPT = TESTS_DIR / "tool_call.py"
FAILED_CALL = TESTS_DIR / "failed_call.py"
GUARD = TESTS_DIR / "_guarded_run.py"
GUARD_PROBE = TESTS_DIR / "_guard_probe.py"

sys.path.insert(0, str(TESTS_DIR))
from _fake_openai import RESPONSE_MODEL, USAGE, FakeOpenAI  # noqa: E402

PROJECT = "openlit-recipe-contract"
SERVICE_NAME = "openlit-recipe"
FI_API_KEY = "fi-api-placeholder-0000"
# A comma and an equals sign: the recipe's percent-encoding must carry them.
FI_SECRET_KEY = "fi-secret-placeholder,part=0000"
OPENAI_KEY = "sk-openai-placeholder-0000"

# Content markers: the question (input), the fake model's answer (output)
# and the policy text in the system prompt. None may reach an export with
# content off.
QUESTION = "QMARK7c1e what is the refund window?"
ANSWER = "AMARK5d2b refunds are accepted for 30 days."
POLICY = "Refunds are accepted within 30 days of purchase."
CONTENT_MARKERS = ("QMARK7c1e", "AMARK5d2b", POLICY)

# Markers that DO reach the export with content off; README.md, Privacy,
# lists them as exceptions. A tool call whose generated arguments carry an
# email, the caller's ``user`` parameter, and a server error body.
TOOL_ARGS_MARKER = "TMARK3f9a"
TOOL_CALL = {
    "id": "call_fake0001",
    "name": "lookup_order",
    "arguments": json.dumps({"email": TOOL_ARGS_MARKER + "@example.com"}),
}
END_USER = "UMARK8e4c@example.com"
ERROR_MARKER = "EMARK41d7"
ERROR_BODY = {
    "error": {
        "message": ERROR_MARKER + " The server had an error processing your request.",
        "type": "server_error",
        "param": None,
        "code": None,
    }
}

LLM_SPAN = "chat gpt-4o-mini"
HTTP_SPAN = "POST"

# The key inventory: every attribute key openlit 1.45.0 put on each span of
# one non-streaming chat call, content off. README.md lists the same keys.
LLM_SPAN_KEYS = {
    "deployment.environment",
    "gen_ai.client.token.usage",
    "gen_ai.operation.name",
    "gen_ai.output.type",
    "gen_ai.provider.name",
    "gen_ai.request.frequency_penalty",
    "gen_ai.request.model",
    "gen_ai.request.presence_penalty",
    "gen_ai.request.seed",
    "gen_ai.request.stream",
    "gen_ai.request.temperature",
    "gen_ai.request.top_p",
    "gen_ai.request.user",
    "gen_ai.response.finish_reasons",
    "gen_ai.response.id",
    "gen_ai.response.model",
    "gen_ai.sdk.version",
    "gen_ai.server.time_per_output_token",
    "gen_ai.server.time_to_first_token",
    "gen_ai.usage.cache_creation.input_tokens",
    "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.cost",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "openai.api.type",
    "openlit.agent.version_hash",
    "server.address",
    "server.port",
    "service.name",
    "telemetry.sdk.name",
}
# Added to the model-call span with content on (OpenLIT's default).
CONTENT_KEYS = {
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.system_instructions",
}
# OpenLIT also enables OpenTelemetry's httpx instrumentor; the OpenAI
# client's HTTP request becomes this child span.
HTTP_SPAN_KEYS = {"http.method", "http.status_code", "http.url"}
RESOURCE_KEYS = {
    "deployment.environment",
    "project_name",
    "project_type",
    "service.instance.id",
    "service.name",
    "telemetry.sdk.language",
    "telemetry.sdk.name",
    "telemetry.sdk.version",
}

# A hang guard, not a speed check: one run imports OpenLIT and its
# instrumentors.
RUN_TIMEOUT_SECONDS = 180


def fi_headers() -> dict[str, str]:
    """The otlp_headers dict exactly as src/app.py builds it."""
    return {
        "x-api-key": quote(FI_API_KEY, safe=""),
        "x-secret-key": quote(FI_SECRET_KEY, safe=""),
    }


class _PathRecorder:
    """Loopback catch-all that records every request's method, path and reply.

    By default it answers /v1/traces and /tracer/v1/traces with 200 and
    anything else with 404, as fi-collector's HTTP mux does
    (pkg/server/server.go:233-234). ``status`` forces one reply for all.
    """

    def __init__(self, status: Optional[HTTPStatus] = None) -> None:
        self.calls: list[tuple[str, str, int]] = []
        lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if status is not None:
                    reply = status
                elif path in ("/v1/traces", "/tracer/v1/traces"):
                    reply = HTTPStatus.OK
                else:
                    reply = HTTPStatus.NOT_FOUND
                with lock:
                    owner.calls.append(("POST", path, int(reply)))
                self.send_response(reply)
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
        return {path for _method, path, _status in self.calls}


def _child_env(
    home: Path, guard_log: Path, endpoint: str, openai_base_url: str, **overrides: Optional[str]
) -> dict[str, str]:
    """The recipe's environment, built from scratch (nothing inherited but PATH).

    Bytecode writing stays on so later runs reuse the SDK's compiled modules;
    the scripts themselves run through runpy and leave no .pyc in the repo.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "LOOPBACK_GUARD_LOG": str(guard_log),
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        "OTEL_RESOURCE_ATTRIBUTES": "project_name={0},project_type=observe".format(PROJECT),
        "FI_API_KEY": FI_API_KEY,
        "FI_SECRET_KEY": FI_SECRET_KEY,
        "OPENAI_API_KEY": OPENAI_KEY,
        "OPENAI_BASE_URL": openai_base_url,
    }
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _guard_attempts(guard_log: Path) -> list[dict[str, Any]]:
    if not guard_log.exists():
        return []
    return [json.loads(line) for line in guard_log.read_text().splitlines()]


def _launch(
    tmp_path: Path, script: Path, endpoint: str, fake: FakeOpenAI, **overrides: Optional[str]
) -> tuple[Any, list[dict[str, Any]]]:
    """Run one script under the loopback guard; return its result and the guard log."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    guard_log = tmp_path / "guard.jsonl"
    env = _child_env(tmp_path, guard_log, endpoint, fake.base_url, **overrides)
    result = run(
        [sys.executable, str(GUARD), str(script), QUESTION],
        env=env,
        stdin=None,
        timeout=RUN_TIMEOUT_SECONDS,
    )
    return result, _guard_attempts(guard_log)


def _run_script(
    tmp_path: Path, script: Path, fake: Optional[dict[str, Any]] = None, **overrides: Optional[str]
) -> dict[str, Any]:
    """Run one script against a Receiver; return everything the tests read.

    ``fake`` holds extra FakeOpenAI arguments (``tool_call``, ``error``).
    """
    with Receiver() as receiver, FakeOpenAI(ANSWER, **(fake or {})) as fake_openai:
        result, guard_attempts = _launch(
            tmp_path, script, receiver.origin, fake_openai, **overrides
        )
        record = {
            "result": result,
            "stdout": result.stdout.decode("utf-8", "replace"),
            "stderr": result.stderr.decode("utf-8", "replace"),
            "requests": receiver.requests(),
            "spans": receiver.spans(),
            "model_requests": list(fake_openai.requests),
            "model_authorizations": list(fake_openai.authorizations),
            "guard_attempts": guard_attempts,
        }
    return record


def _run_variant(tmp_path: Path, **init_overrides: Any) -> dict[str, Any]:
    """Run src/app.py with some openlit.init() arguments replaced (None removes one)."""
    return _run_script(tmp_path, VARIANT, RECIPE_INIT_OVERRIDES=json.dumps(init_overrides))


def _run_against_recorder(
    tmp_path: Path, suffix: str = "", status: Optional[HTTPStatus] = None, **overrides: Optional[str]
) -> tuple[Any, list[tuple[str, str, int]], list[dict[str, Any]]]:
    """Run a script with a _PathRecorder as the collector; return result, calls, guard log."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    guard_log = tmp_path / "guard.jsonl"
    script = VARIANT if "RECIPE_INIT_OVERRIDES" in overrides else APP
    with _PathRecorder(status) as recorder, FakeOpenAI(ANSWER) as fake:
        env = _child_env(
            tmp_path, guard_log, recorder.origin + suffix, fake.base_url, **overrides
        )
        result = run(
            [sys.executable, str(GUARD), str(script), QUESTION],
            env=env,
            stdin=None,
            timeout=RUN_TIMEOUT_SECONDS,
        )
        calls = list(recorder.calls)
    return result, calls, _guard_attempts(guard_log)


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


def _keys_carrying(record: dict[str, Any], marker: str) -> set[str]:
    """Span attribute, event attribute and status keys whose value contains ``marker``."""
    keys: set[str] = set()
    for span in record["spans"]:
        for attributes in [_attrs(span)] + [_attrs(event) for event in span.get("events", [])]:
            keys |= {key for key, value in attributes.items() if marker in str(value)}
        if marker in span.get("status", {}).get("message", ""):
            keys.add("status.message")
    return keys


def _assert_ran(record: dict[str, Any]) -> None:
    result = record["result"]
    assert not result.timed_out, record["stderr"]
    assert result.returncode == 0, record["stderr"]
    assert record["guard_attempts"] == [], record["guard_attempts"]
    assert record["requests"], "no export reached the receiver"
    assert record["spans"], "the export carried no spans"


# --------------------------------------------------------------------------
# The recipe as documented: content, metrics and events off, local prices.
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def recipe_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    record = _run_script(tmp_path_factory.mktemp("recipe"), APP)
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
    logged = _guard_attempts(guard_log)
    assert [entry["kind"] for entry in logged] == ["connect", "connect_ex", "getaddrinfo"]
    assert "192.0.2.1" in logged[0]["target"]
    assert "2001:db8::1" in logged[1]["target"]
    assert "example.invalid" in logged[2]["target"]


def test_origin_endpoint_posts_to_v1_traces(recipe_run: dict[str, Any]) -> None:
    assert recipe_run["requests"], "no export reached the receiver"
    assert {request["path"] for request in recipe_run["requests"]} == {"/v1/traces"}


def test_otlp_headers_carry_the_future_agi_keys(recipe_run: dict[str, Any]) -> None:
    assert recipe_run["requests"], "no export reached the receiver"
    for request in recipe_run["requests"]:
        headers = request["headers"]
        assert headers.get("x-api-key") == FI_API_KEY
        assert headers.get("x-secret-key") == FI_SECRET_KEY
        assert "authorization" not in headers
        assert all(OPENAI_KEY not in value for value in headers.values())


def test_resource_carries_the_project(recipe_run: dict[str, Any]) -> None:
    assert recipe_run["requests"], "no export reached the receiver"
    for request in recipe_run["requests"]:
        assert request["resource_attributes"], "export without a resource"
        for resource in request["resource_attributes"]:
            assert set(resource) == RESOURCE_KEYS
            assert resource["project_name"] == PROJECT
            assert resource["project_type"] == "observe"
            assert resource["service.name"] == SERVICE_NAME
            assert resource["telemetry.sdk.name"] == "openlit"
            assert resource["deployment.environment"] == "default"


def test_one_trace_model_call_with_an_http_child(recipe_run: dict[str, Any]) -> None:
    spans = _by_name(recipe_run["spans"])
    assert set(spans) == {LLM_SPAN, HTTP_SPAN}
    assert len({span["traceId"] for span in spans.values()}) == 1
    llm, http = spans[LLM_SPAN], spans[HTTP_SPAN]
    assert not llm.get("parentSpanId")
    assert http["parentSpanId"] == llm["spanId"]
    assert llm["kind"] == http["kind"] == "SPAN_KIND_CLIENT"
    assert llm["status"].get("code") == "STATUS_CODE_OK"


def test_key_inventory(recipe_run: dict[str, Any]) -> None:
    spans = _by_name(recipe_run["spans"])
    llm = _attrs(spans[LLM_SPAN])
    assert set(llm) == LLM_SPAN_KEYS
    assert set(_attrs(spans[HTTP_SPAN])) == HTTP_SPAN_KEYS

    # The values the collector's alias lists can read.
    assert llm["gen_ai.operation.name"] == "chat"
    assert llm["gen_ai.provider.name"] == "openai"
    assert llm["gen_ai.request.model"] == "gpt-4o-mini"
    assert llm["gen_ai.response.model"] == RESPONSE_MODEL
    assert llm["gen_ai.usage.input_tokens"] == USAGE["prompt_tokens"]
    assert llm["gen_ai.usage.output_tokens"] == USAGE["completion_tokens"]
    # The total is sent under OpenLIT's own key, not gen_ai.usage.total_tokens.
    assert llm["gen_ai.client.token.usage"] == USAGE["total_tokens"]
    # With an empty price table OpenLIT still sends a cost, of 0.
    assert llm["gen_ai.usage.cost"] == 0
    assert llm["gen_ai.usage.cache_read.input_tokens"] == 0
    assert llm["gen_ai.usage.cache_creation.input_tokens"] == 0
    # Request parameters the call did not set are sent with OpenLIT's defaults.
    assert llm["gen_ai.request.temperature"] == 1.0
    assert llm["gen_ai.request.top_p"] == 1.0
    assert llm["gen_ai.request.frequency_penalty"] == 0.0
    assert llm["gen_ai.request.presence_penalty"] == 0.0
    assert llm["gen_ai.request.seed"] == 0
    assert llm["gen_ai.request.user"] == ""
    assert llm["gen_ai.request.stream"] is False
    assert llm["gen_ai.response.id"] == "chatcmpl-fake"
    assert llm["gen_ai.response.finish_reasons"] == ["stop"]
    assert llm["gen_ai.output.type"] == "text"
    assert llm["gen_ai.sdk.version"] == importlib.metadata.version("openai")
    assert isinstance(llm["gen_ai.server.time_to_first_token"], float)
    assert llm["gen_ai.server.time_per_output_token"] == 0
    assert llm["openai.api.type"] == "chat_completions"
    assert re.fullmatch(r"[0-9a-f]{16}", llm["openlit.agent.version_hash"])
    assert llm["server.address"] == "127.0.0.1"
    assert isinstance(llm["server.port"], int)
    assert llm["service.name"] == SERVICE_NAME
    assert llm["deployment.environment"] == "default"
    assert llm["telemetry.sdk.name"] == "openlit"
    # None of the keys fi-collector reads a span kind from (converter.go:79-84).
    for key in ("fi.span.kind", "gen_ai.span.kind", "llm.request.type", "openinference.span.kind"):
        assert key not in llm
    assert "gen_ai.system" not in llm

    http = _attrs(spans[HTTP_SPAN])
    assert http["http.method"] == "POST"
    assert http["http.url"].startswith("http://127.0.0.1:")
    assert http["http.url"].endswith("/v1/chat/completions")
    assert http["http.status_code"] == 200


def test_content_off_exports_no_content(recipe_run: dict[str, Any]) -> None:
    dump = _export_dump(recipe_run)
    for marker in CONTENT_MARKERS:
        assert marker not in dump, marker
    for span in recipe_run["spans"]:
        assert not CONTENT_KEYS & set(_attrs(span)), span["name"]
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


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        ("", ("/v1/traces", 200)),
        ("/", ("/v1/traces", 200)),
        # The exporter always appends v1/traces, so a full path doubles it.
        ("/v1/traces", ("/v1/traces/v1/traces", 404)),
        # fi-collector also serves /tracer/v1/traces.
        ("/tracer", ("/tracer/v1/traces", 200)),
    ],
)
def test_endpoint_is_the_origin_and_v1_traces_is_appended(
    tmp_path: Path, suffix: str, expected: tuple[str, int]
) -> None:
    result, calls, guard_attempts = _run_against_recorder(tmp_path, suffix)
    assert not result.timed_out
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert guard_attempts == []
    assert calls, "no export reached the recorder"
    assert {(path, status) for _method, path, status in calls} == {expected}


def test_unencoded_comma_in_a_header_value_truncates_it(tmp_path: Path) -> None:
    """Why the recipe percent-encodes: the exporter splits the headers on commas."""
    record = _run_variant(
        tmp_path, otlp_headers={"x-api-key": FI_API_KEY, "x-secret-key": FI_SECRET_KEY}
    )
    _assert_ran(record)
    for request in record["requests"]:
        assert request["headers"].get("x-api-key") == FI_API_KEY
        assert request["headers"].get("x-secret-key") == FI_SECRET_KEY.split(",")[0]


def test_content_is_exported_when_the_argument_is_left_out(tmp_path: Path) -> None:
    """OpenLIT's default is content ON. Control for the content-off test."""
    record = _run_variant(tmp_path, capture_message_content=None)
    _assert_ran(record)
    llm = _attrs(_by_name(record["spans"])[LLM_SPAN])
    assert set(llm) == LLM_SPAN_KEYS | CONTENT_KEYS
    assert "QMARK7c1e" in llm["gen_ai.input.messages"]
    assert POLICY in llm["gen_ai.input.messages"]  # the system prompt
    assert POLICY in llm["gen_ai.system_instructions"]
    assert "AMARK5d2b" in llm["gen_ai.output.messages"]
    dump = _export_dump(record)
    for secret in (FI_API_KEY, FI_SECRET_KEY, OPENAI_KEY):
        assert secret not in dump


def test_env_false_turns_content_off_over_an_explicit_true(tmp_path: Path) -> None:
    record = _run_script(
        tmp_path,
        VARIANT,
        RECIPE_INIT_OVERRIDES=json.dumps({"capture_message_content": True}),
        OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT="false",
    )
    _assert_ran(record)
    dump = _export_dump(record)
    for marker in CONTENT_MARKERS:
        assert marker not in dump, marker


@pytest.mark.parametrize(
    ("left_out", "extra_path"),
    [
        (None, None),
        ("disable_metrics", "/v1/metrics"),
        ("disable_events", "/v1/logs"),
    ],
)
def test_metrics_and_events_go_to_paths_fi_collector_does_not_serve(
    tmp_path: Path, left_out: Optional[str], extra_path: Optional[str]
) -> None:
    overrides = {left_out: None} if left_out else {}
    result, calls, guard_attempts = _run_against_recorder(
        tmp_path, RECIPE_INIT_OVERRIDES=json.dumps(overrides)
    )
    assert not result.timed_out
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert guard_attempts == []
    paths = {path for _method, path, _status in calls}
    if extra_path is None:
        assert paths == {"/v1/traces"}
    else:
        assert paths == {"/v1/traces", extra_path}
        assert {status for _method, path, status in calls if path == extra_path} == {404}


def test_default_pricing_download_is_the_only_outbound_attempt(tmp_path: Path) -> None:
    """Without pricing_json, init() fetches prices from GitHub. The guard refuses it."""
    record = _run_variant(tmp_path, pricing_json=None)
    assert not record["result"].timed_out
    assert record["result"].returncode == 0, record["stderr"]
    assert [entry["kind"] for entry in record["guard_attempts"]] == ["getaddrinfo"]
    assert "raw.githubusercontent.com" in record["guard_attempts"][0]["target"]
    # The run still exported; the recipe's run above made no attempt at all.
    assert record["requests"] and record["spans"]


def test_trace_decorator_ignores_the_content_switch(tmp_path: Path) -> None:
    """@openlit.trace records arguments and return value with content off."""
    record = _run_script(tmp_path, TRACED_FUNCTION)
    _assert_ran(record)
    spans = _by_name(record["spans"])
    assert set(spans) == {"support_request", LLM_SPAN, HTTP_SPAN}
    decorated = _attrs(spans["support_request"])
    assert "QMARK7c1e" in decorated["function.args"]
    assert decorated["gen_ai.output.messages"] == ANSWER
    # The model call under it stays content-free.
    assert not CONTENT_KEYS & set(_attrs(spans[LLM_SPAN]))
    assert spans[LLM_SPAN]["parentSpanId"] == spans["support_request"]["spanId"]


# The next tests prove the exceptions README.md lists under Privacy. They
# show what still leaks with content off; they are not privacy guarantees.


@pytest.fixture(scope="module")
def tool_call_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    record = _run_script(
        tmp_path_factory.mktemp("tool_call"),
        TOOL_CALL_SCRIPT,
        fake={"tool_call": TOOL_CALL},
        RECIPE_USER=END_USER,
    )
    _assert_ran(record)
    return record


def test_content_off_still_exports_the_models_tool_calls(tool_call_run: dict[str, Any]) -> None:
    """Tool name, call ID and the model's generated arguments (utils.py:1524-1551)."""
    request = tool_call_run["model_requests"][0]
    assert [tool["function"]["name"] for tool in request["tools"]] == [TOOL_CALL["name"]]
    assert tool_call_run["stdout"].strip() == TOOL_CALL["name"]
    llm = _attrs(_by_name(tool_call_run["spans"])[LLM_SPAN])
    assert llm["gen_ai.tool.name"] == TOOL_CALL["name"]
    assert llm["gen_ai.tool.call.id"] == TOOL_CALL["id"]
    # The arguments arrive verbatim, planted email included.
    assert llm["gen_ai.tool.args"] == TOOL_CALL["arguments"]
    assert TOOL_ARGS_MARKER in llm["gen_ai.tool.args"]
    # The messages themselves stay out.
    assert not CONTENT_KEYS & set(llm)
    dump = _export_dump(tool_call_run)
    for marker in CONTENT_MARKERS:
        assert marker not in dump, marker


def test_content_off_still_exports_the_request_user(tool_call_run: dict[str, Any]) -> None:
    """The ``user`` argument goes to gen_ai.request.user (utils.py:1484-1487)."""
    assert tool_call_run["model_requests"][0]["user"] == END_USER
    llm = _attrs(_by_name(tool_call_run["spans"])[LLM_SPAN])
    assert llm["gen_ai.request.user"] == END_USER


@pytest.fixture(scope="module")
def failed_call_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    record = _run_script(
        tmp_path_factory.mktemp("failed_call"),
        FAILED_CALL,
        fake={"error": (HTTPStatus.INTERNAL_SERVER_ERROR, ERROR_BODY)},
    )
    _assert_ran(record)
    return record


def test_failed_call_records_the_error_body_but_no_content(failed_call_run: dict[str, Any]) -> None:
    """OpenLIT records the exception, then the SDK's span context records it again."""
    assert failed_call_run["stdout"].strip() == "raised InternalServerError 500"
    assert len(failed_call_run["model_requests"]) == 1  # max_retries=0
    spans = _by_name(failed_call_run["spans"])
    assert set(spans) == {LLM_SPAN, HTTP_SPAN}
    llm = spans[LLM_SPAN]
    # openai.py:186-209 -> utils.py:77-84, then opentelemetry-api use_span.
    events = llm.get("events", [])
    assert [event["name"] for event in events] == ["exception", "exception"]
    for event in events:
        attributes = _attrs(event)
        assert attributes["exception.type"] == "openai.InternalServerError"
        # The openai SDK puts the whole response body in the message.
        assert attributes["exception.message"] == "Error code: 500 - {0}".format(ERROR_BODY)
        assert attributes["exception.message"] in attributes["exception.stacktrace"]
    assert llm["status"]["code"] == "STATUS_CODE_ERROR"
    assert llm["status"]["message"] == "InternalServerError: Error code: 500 - {0}".format(
        ERROR_BODY
    )
    assert _attrs(llm)["error.type"] == "InternalServerError"
    assert _keys_carrying(failed_call_run, ERROR_MARKER) == {
        "exception.message",
        "exception.stacktrace",
        "status.message",
    }
    # The fake's error body echoes no input, so no prompt or answer text arrives.
    dump = _export_dump(failed_call_run)
    for marker in CONTENT_MARKERS:
        assert marker not in dump, marker


def test_rejected_export_is_logged_and_the_app_still_exits_zero(tmp_path: Path) -> None:
    """fi-collector answers 401 without valid keys; OpenLIT only logs it."""
    result, calls, guard_attempts = _run_against_recorder(
        tmp_path, status=HTTPStatus.UNAUTHORIZED
    )
    assert not result.timed_out
    assert guard_attempts == []
    assert {(path, status) for _method, path, status in calls} == {("/v1/traces", 401)}
    assert result.returncode == 0
    stderr = result.stderr.decode("utf-8", "replace")
    assert "Failed to export spans batch code: 401" in stderr, stderr
    assert ANSWER in result.stdout.decode("utf-8", "replace")


@pytest.mark.parametrize(
    ("unset", "message"),
    [
        ("OTEL_EXPORTER_OTLP_ENDPOINT", "Set OTEL_EXPORTER_OTLP_ENDPOINT"),
        ("OTEL_RESOURCE_ATTRIBUTES", "Set OTEL_RESOURCE_ATTRIBUTES"),
        ("FI_API_KEY", "Set FI_API_KEY and FI_SECRET_KEY"),
        ("FI_SECRET_KEY", "Set FI_API_KEY and FI_SECRET_KEY"),
    ],
)
def test_app_refuses_to_start_without_endpoint_keys_or_project(
    tmp_path: Path, unset: str, message: str
) -> None:
    record = _run_script(tmp_path, APP, **{unset: None})
    assert not record["result"].timed_out
    assert record["result"].returncode != 0
    assert message in record["stderr"]
    assert "Traceback" not in record["stderr"]
    for secret in (FI_API_KEY, FI_SECRET_KEY):
        assert secret not in record["stderr"]
    assert record["requests"] == []
    assert record["model_requests"] == []
    assert record["guard_attempts"] == []


def _skip_without_traceai_openai() -> None:
    if importlib.util.find_spec("traceai_openai") is None:
        pytest.skip("traceai-openai is not installed; see README.md, Tests")


def test_openlit_and_traceai_openai_both_trace_the_same_call(tmp_path: Path) -> None:
    """Both enabled: two model-call spans for one call. The README says enable one."""
    _skip_without_traceai_openai()
    record = _run_script(tmp_path, VARIANT, RECIPE_ADD_TRACEAI_OPENAI="1")
    _assert_ran(record)
    spans = _by_name(record["spans"])
    assert set(spans) == {LLM_SPAN, "ChatCompletion", HTTP_SPAN}
    assert len(record["model_requests"]) == 1
    assert len({span["traceId"] for span in spans.values()}) == 1
    # OpenLIT's span wraps traceAI's, which wraps the HTTP request.
    assert spans["ChatCompletion"]["parentSpanId"] == spans[LLM_SPAN]["spanId"]
    assert spans[HTTP_SPAN]["parentSpanId"] == spans["ChatCompletion"]["spanId"]
    openlit_llm, traceai_llm = _attrs(spans[LLM_SPAN]), _attrs(spans["ChatCompletion"])
    assert openlit_llm["gen_ai.operation.name"] == "chat"
    assert traceai_llm["gen_ai.span.kind"] == "LLM"
    # Both carry the same usage, so a sum over spans counts the call twice.
    for llm in (openlit_llm, traceai_llm):
        assert llm["gen_ai.usage.input_tokens"] == USAGE["prompt_tokens"]
        assert llm["gen_ai.usage.output_tokens"] == USAGE["completion_tokens"]
    # OpenLIT's content switch does not reach traceAI's span.
    assert POLICY in traceai_llm["input.value"]
    assert "QMARK7c1e" in traceai_llm["gen_ai.input.messages.1.message.content"]
    assert "AMARK5d2b" in traceai_llm["output.value"]
    assert not CONTENT_KEYS & set(openlit_llm)


def test_disabling_openlits_openai_instrumentor_leaves_one_model_span(tmp_path: Path) -> None:
    """The README's way to enable only traceAI's: disabled_instrumentors=["openai"]."""
    _skip_without_traceai_openai()
    record = _run_script(
        tmp_path,
        VARIANT,
        RECIPE_INIT_OVERRIDES=json.dumps({"disabled_instrumentors": ["openai"]}),
        RECIPE_ADD_TRACEAI_OPENAI="1",
    )
    _assert_ran(record)
    spans = _by_name(record["spans"])
    assert set(spans) == {"ChatCompletion", HTTP_SPAN}
    assert spans[HTTP_SPAN]["parentSpanId"] == spans["ChatCompletion"]["spanId"]


# --------------------------------------------------------------------------
# README checks, and an opt-in check against the collector's alias lists.
# --------------------------------------------------------------------------


def _readme_section(title: str) -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index("\n## {0}\n".format(title))
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def test_readme_key_inventory_matches_the_emitted_keys() -> None:
    section = _readme_section("Key inventory")
    listed = set(re.findall(r"^\| `([a-z_.]+)` \|", section, flags=re.MULTILINE))
    assert listed == LLM_SPAN_KEYS | CONTENT_KEYS | HTTP_SPAN_KEYS


def test_readme_privacy_names_what_content_off_still_exports(
    tool_call_run: dict[str, Any], failed_call_run: dict[str, Any]
) -> None:
    """Each key a tool call adds, and each key a planted marker reached, is listed."""
    privacy = _readme_section("Privacy")
    not_covered = privacy[privacy.index("Not covered by the content switch") :]
    tool_llm = _attrs(_by_name(tool_call_run["spans"])[LLM_SPAN])
    exported = (
        (set(tool_llm) - LLM_SPAN_KEYS)
        | _keys_carrying(tool_call_run, TOOL_ARGS_MARKER)
        | _keys_carrying(tool_call_run, END_USER)
        | _keys_carrying(failed_call_run, ERROR_MARKER)
    )
    assert {"gen_ai.tool.args", "gen_ai.request.user", "exception.message"} <= exported
    missing = sorted(key for key in exported if "`{0}`".format(key) not in not_covered)
    assert missing == [], "not listed in README Privacy: {0}".format(missing)


def _collector_aliases() -> dict[str, Any]:
    src = os.environ.get("FI_COLLECTOR_SRC")
    if not src:
        pytest.skip("set FI_COLLECTOR_SRC to a future-agi checkout's fi-collector directory")
    adapter = (Path(src) / "pkg" / "adapter" / "adapter.go").read_text(encoding="utf-8")
    converter = (Path(src) / "exporter" / "clickhouse25exporter" / "converter.go").read_text(
        encoding="utf-8"
    )

    def string_slice(source: str, name: str) -> list[str]:
        match = re.search(r"\b{0}\s*=\s*\[\]string\{{(.*?)\}}".format(name), source, re.DOTALL)
        assert match, name
        body = re.sub(r"//[^\n]*", "", match.group(1))
        return re.findall(r'"([^"]+)"', body)

    synonyms_body = re.search(
        r"\bspanKindSynonyms\s*=\s*map\[string\]string\{(.*?)\}", converter, re.DOTALL
    )
    assert synonyms_body
    names = (
        "modelNameKeys",
        "providerKeys",
        "inputTokenKeys",
        "outputTokenKeys",
        "totalTokenKeys",
        "costTotalKeys",
        "costInputKeys",
        "costOutputKeys",
    )
    aliases: dict[str, Any] = {name: string_slice(adapter, name) for name in names}
    aliases["spanKindAttrKeys"] = string_slice(converter, "spanKindAttrKeys")
    aliases["operationNameAttrKeys"] = string_slice(converter, "operationNameAttrKeys")
    aliases["spanKindSynonyms"] = dict(
        re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', synonyms_body.group(1))
    )
    return aliases


def _first(attrs: dict[str, Any], keys: list[str]) -> Optional[str]:
    return next((key for key in keys if attrs.get(key) not in (None, "")), None)


def test_readme_display_table_matches_the_collector_aliases(recipe_run: dict[str, Any]) -> None:
    """Which emitted key fills each fi-collector column, read from the Go source."""
    aliases = _collector_aliases()
    llm = _attrs(_by_name(recipe_run["spans"])[LLM_SPAN])
    kind_key = _first(llm, aliases["spanKindAttrKeys"]) or _first(
        llm, aliases["operationNameAttrKeys"]
    )
    expected = {
        "model": _first(llm, aliases["modelNameKeys"]),
        "provider": _first(llm, aliases["providerKeys"]),
        "prompt_tokens": _first(llm, aliases["inputTokenKeys"]),
        "completion_tokens": _first(llm, aliases["outputTokenKeys"]),
        "total_tokens": _first(llm, aliases["totalTokenKeys"]) or "derived",
        "cost": _first(
            llm,
            aliases["costTotalKeys"] + aliases["costInputKeys"] + aliases["costOutputKeys"],
        ),
        "observation_type": kind_key,
        "input": "input.value" if "input.value" in llm else None,
        "output": "output.value" if "output.value" in llm else None,
    }
    # gen_ai.operation.name=chat resolves to the llm observation type.
    assert aliases["spanKindSynonyms"].get(llm[kind_key]) == "llm"

    rows = {
        column: key or word
        for column, key, word in re.findall(
            r"^\| `([a-z_]+)` \| (?:`([a-z_.]+)`|(derived|none))",
            _readme_section("What Future AGI shows"),
            flags=re.MULTILINE,
        )
    }
    assert rows == {column: key or "none" for column, key in expected.items()}
