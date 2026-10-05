"""Contract test for the Cognee tracing recipe.

Runs ``src/app.py`` as written with the real ``cognee`` 1.6.2 and traceAI's
``register()``, plus two one-call scenario scripts, each in a subprocess whose
network is limited to 127.0.0.1 (``_guarded_run.py``). Cognee's LLM and
embedding endpoints are a loopback fake of the OpenAI API
(``_fake_openai.py``). Spans go to the shared harness ``Receiver``, which
serves ``/v1/traces`` and ``/tracer/v1/traces`` like fi-collector's HTTP mux
but does not authenticate, stamp projects or store anything.

The recorded fixture (``fixtures/cognee-1.6.2-recipe-spans.json``) is the
recipe run's spans as the Receiver decoded them. The fixture tests post it
with ``post_otlp()`` and check it with ``compare()`` and the same mapping
assertions as the live run, without Cognee installed. Regenerate it with
``COGNEE_RECORD_FIXTURE=1``.

All keys are placeholders. Nothing here contacts an LLM provider, Cognee's
telemetry host or Future AGI.
"""

from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from harness import Receiver, compare, post_otlp, run

TESTS_DIR = Path(__file__).resolve().parent
RECIPE_DIR = TESTS_DIR.parent
PYTHON_DIR = RECIPE_DIR.parents[1]
REPO_ROOT = PYTHON_DIR.parent
APP = RECIPE_DIR / "src" / "app.py"
README = RECIPE_DIR / "README.md"
ADD_SCRIPT = TESTS_DIR / "cognee_add.py"
GUARD = TESTS_DIR / "_guarded_run.py"
FIXTURE = TESTS_DIR / "fixtures" / "cognee-1.6.2-recipe-spans.json"

sys.path.insert(0, str(TESTS_DIR))
from _fake_openai import ANSWER_MARKER, FakeOpenAI  # noqa: E402

PROJECT = "cognee-recipe-contract"
FI_API_KEY = "fi-api-placeholder-0000"
FI_SECRET_KEY = "fi-secret-placeholder-0000"
LLM_API_KEY = "llm-placeholder-key-0000"
LLM_MODEL = "openai/gpt-4o-mini"
EMBEDDING_MODEL = "openai/text-embedding-3-small"

# Content markers: the document text, the search question and the fake
# model's answer.
DOC_MARKER = "DMARK7f3a"
QUERY_MARKER = "QMARK91c2"
DOCUMENT = "Ada works on the Lighthouse project. " + DOC_MARKER
QUESTION = "Who works on Lighthouse? " + QUERY_MARKER

# Which attribute keys carry each kind of content by default (README.md,
# "Content Cognee exports").
CONTENT_KEYS = {
    DOC_MARKER: {"langfuse.observation.input"},
    QUERY_MARKER: {"langfuse.observation.input", "cognee.search.query", "memory.query.text"},
    ANSWER_MARKER: {"langfuse.observation.output"},
}

APP_SPAN = "remember_and_ask"
INTERNAL = "SPAN_KIND_INTERNAL"
CLIENT = "SPAN_KIND_CLIENT"
GENERATION_SPAN = "cognee.observe.acreate_structured_output"
EMBEDDING_SPAN = "cognee.observe.embed_text"
# Every span name the recipe run produces, with its OTel span kind
# (README.md, "Spans Cognee 1.6.2 emits").
SPAN_KINDS = {
    APP_SPAN: INTERNAL,
    "memory.store": INTERNAL,
    "memory.process": INTERNAL,
    "memory.retrieve": INTERNAL,
    "cognee.pipeline.task.resolve_data_directories": INTERNAL,
    "cognee.pipeline.task.ingest_data": INTERNAL,
    "cognee.pipeline.task.classify_documents": INTERNAL,
    "cognee.pipeline.task.extract_chunks_from_documents": INTERNAL,
    "cognee.pipeline.task.extract_graph_and_summarize": INTERNAL,
    "cognee.pipeline.task.add_data_points": INTERNAL,
    GENERATION_SPAN: CLIENT,
    EMBEDDING_SPAN: CLIENT,
    "cognee.llm.completion": INTERNAL,
    "cognee.db.graph.query": INTERNAL,
    "cognee.db.vector.search": INTERNAL,
    "cognee.search.authorize": INTERNAL,
    "cognee.search.dataset": INTERNAL,
    "cognee.retrieval.get_objects": INTERNAL,
    "cognee.retrieval.triplet_search": INTERNAL,
    "cognee.retrieval.vector_search": INTERNAL,
    "cognee.retrieval.embed_query": INTERNAL,
    "cognee.retrieval.get_context": INTERNAL,
    "cognee.retrieval.get_completion": INTERNAL,
    "cognee.session.get_session": INTERNAL,
    "cognee.session.add_qa": INTERNAL,
}
COGNEE_ROOTS = ("memory.store", "memory.process", "memory.retrieve")
# fi-collector reads the span kind from these keys, then falls back to
# gen_ai.operation.name (exporter/clickhouse25exporter/converter.go).
SPAN_KIND_KEYS = ("fi.span.kind", "gen_ai.span.kind", "llm.request.type", "openinference.span.kind")
TOKEN_KEYS = (
    "llm.token_count.prompt",
    "llm.token_count.completion",
    "llm.token_count.total",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.total_tokens",
    "llm.usage.prompt_tokens",
    "llm.usage.completion_tokens",
    "llm.usage.total_tokens",
)

# Production URL forms for the no-register() option, and whether Cognee
# 1.6.2 treats each as HTTP-only (tracing.py _requires_http_exporter).
OTLP_URL_FORMS = {
    "https://api.futureagi.com/tracer/v1/traces": False,
    "https://api.futureagi.com:443/tracer/v1/traces": True,
}
RUN_TIMEOUT_SECONDS = 900

COGNEE_INSTALLED = importlib.util.find_spec("cognee") is not None


# --------------------------------------------------------------------------
# Helpers shared by the live run and the recorded fixture
# --------------------------------------------------------------------------


def attributes(span: dict[str, Any]) -> dict[str, Any]:
    flat = {}
    for attribute in span.get("attributes", []):
        value = attribute.get("value", {})
        flat[attribute["key"]] = next(iter(value.values())) if value else None
    return flat


def spans_named(spans: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [span for span in spans if span["name"] == name]


def shape(spans: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per span name: kinds, attribute keys, status codes and count."""
    out: dict[str, dict[str, Any]] = {}
    for span in spans:
        entry = out.setdefault(
            span["name"], {"kinds": set(), "keys": set(), "status": set(), "count": 0}
        )
        entry["kinds"].add(span.get("kind"))
        entry["keys"] |= set(attributes(span))
        entry["status"].add(span.get("status", {}).get("code", "STATUS_CODE_UNSET"))
        entry["count"] += 1
    return out


def scrub(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace machine-local paths in exception stack traces before recording."""
    text = json.dumps(spans)
    text = text.replace(str(REPO_ROOT), "<repo>")
    text = re.sub(r'File \\"[^"\\]*?/lib/python3\.\d+/', r'File \\"<python-lib>/', text)
    return json.loads(text)


def assert_span_names_and_kinds(spans: list[dict[str, Any]]) -> None:
    assert {span["name"]: span["kind"] for span in spans} == SPAN_KINDS
    for span in spans:
        assert span["kind"] == SPAN_KINDS[span["name"]], span["name"]


def assert_collector_mapping_inputs(spans: list[dict[str, Any]]) -> None:
    """Assert the keys fi-collector maps from, as README.md's table states."""
    for span in spans:
        attrs = attributes(span)
        assert not set(SPAN_KIND_KEYS) & set(attrs), span["name"]
        assert not set(TOKEN_KEYS) & set(attrs), span["name"]
        if span["name"] == EMBEDDING_SPAN:
            assert attrs["gen_ai.operation.name"] == "embeddings"
            assert attrs["gen_ai.request.model"] == EMBEDDING_MODEL
            assert attrs["gen_ai.provider.name"] == "openai"
            assert attrs["cognee.span.category"] == "embeddings"
        else:
            assert "gen_ai.operation.name" not in attrs, span["name"]
        if span["name"] == GENERATION_SPAN:
            # AC-04: the model key is present, so no copy processor is written.
            assert attrs["gen_ai.request.model"] == LLM_MODEL
            assert attrs["gen_ai.system"] == "litellm-native"
            assert attrs["cognee.span.category"] == "generation"
            assert attrs["langfuse.observation.type"] == "generation"
    generation_spans = spans_named(spans, GENERATION_SPAN)
    assert generation_spans
    # cognee.llm.model is set only on schema-bound (structured) calls.
    with_cognee_model = [s for s in generation_spans if "cognee.llm.model" in attributes(s)]
    assert 0 < len(with_cognee_model) < len(generation_spans)
    (search,) = spans_named(spans, "memory.retrieve")
    search_attrs = attributes(search)
    assert search_attrs["cognee.search.type"] == "GRAPH_COMPLETION"
    assert search_attrs["memory.query.type"] == "GRAPH_COMPLETION"
    assert search_attrs["memory.operation"] == "retrieve"


def assert_default_content(spans: list[dict[str, Any]]) -> None:
    """Content is exported by default, under exactly the documented keys."""
    for marker, expected_keys in CONTENT_KEYS.items():
        found = {
            key
            for span in spans
            for key, value in attributes(span).items()
            if marker in str(value)
        }
        assert found == expected_keys, marker
    for span in spans_named(spans, EMBEDDING_SPAN):
        assert DOC_MARKER not in json.dumps(span)


def assert_no_secrets(payload: Any) -> None:
    text = json.dumps(payload)
    for secret in (FI_API_KEY, FI_SECRET_KEY, LLM_API_KEY):
        assert secret not in text


def assert_one_trace_under_app_span(spans: list[dict[str, Any]]) -> None:
    assert len({span["traceId"] for span in spans}) == 1
    roots = [span for span in spans if not span.get("parentSpanId")]
    assert [root["name"] for root in roots] == [APP_SPAN]
    root_id = roots[0]["spanId"]
    for name in COGNEE_ROOTS:
        (span,) = spans_named(spans, name)
        assert span["parentSpanId"] == root_id, name
    by_id = {span["spanId"]: span for span in spans}
    assert len(by_id) == len(spans)
    for span in spans:
        if span.get("parentSpanId"):
            assert span["parentSpanId"] in by_id, span["name"]


# --------------------------------------------------------------------------
# Live runs
# --------------------------------------------------------------------------


class Scenario:
    """One guarded subprocess with its own Receiver, fake model and data dir."""

    def __init__(self, name: str, script: Path, args: list[str], work: Path) -> None:
        self.name = name
        self.script = script
        self.args = args
        self.work = work
        self.guard_log = work / "guard.jsonl"
        self.result: Any = None
        self.spans: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.fake_requests: list[dict[str, Any]] = []

    def env(self, receiver: Receiver, fake: FakeOpenAI) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.work),
            "PYTHONPATH": str(PYTHON_DIR),
            "LOOPBACK_GUARD_LOG": str(self.guard_log),
            # Cognee's product telemetry is separate from OTel tracing.
            "TELEMETRY_DISABLED": "1",
            # Keep LiteLLM from fetching its model cost map at import.
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "LLM_PROVIDER": "openai",
            "LLM_MODEL": LLM_MODEL,
            "LLM_ENDPOINT": fake.base_url,
            "LLM_API_KEY": LLM_API_KEY,
            "EMBEDDING_PROVIDER": "openai",
            "EMBEDDING_MODEL": EMBEDDING_MODEL,
            "EMBEDDING_ENDPOINT": fake.base_url,
            "EMBEDDING_API_KEY": LLM_API_KEY,
            "EMBEDDING_DIMENSIONS": str(fake.dimensions),
            "DATA_ROOT_DIRECTORY": str(self.work / "data"),
            "SYSTEM_ROOT_DIRECTORY": str(self.work / "system"),
            "CACHE_ROOT_DIRECTORY": str(self.work / "cache"),
            "COGNEE_LOGS_DIR": str(self.work / "logs"),
        }
        if self.name != "tracing_off":
            env["COGNEE_TRACING_ENABLED"] = "true"
        if self.name == "otlp_env":
            # The no-register() option: Cognee's own exporter, configured
            # exactly as README.md says (with the Receiver's origin).
            env["OTEL_EXPORTER_OTLP_ENDPOINT"] = receiver.collector_endpoint
            env["OTEL_EXPORTER_OTLP_HEADERS"] = "X-Api-Key={0},X-Secret-Key={1}".format(
                FI_API_KEY, FI_SECRET_KEY
            )
            env["OTEL_RESOURCE_ATTRIBUTES"] = "project_name={0},project_type=observe".format(
                PROJECT
            )
        else:
            env.update(
                {
                    "FI_BASE_URL": receiver.origin,
                    "FI_API_KEY": FI_API_KEY,
                    "FI_SECRET_KEY": FI_SECRET_KEY,
                    "FI_PROJECT_NAME": PROJECT,
                }
            )
        return env

    def execute(self) -> "Scenario":
        with Receiver() as receiver:
            fake = FakeOpenAI(dimensions=16)
            try:
                self.result = run(
                    [sys.executable, str(GUARD), str(self.script), *self.args],
                    env=self.env(receiver, fake),
                    stdin=None,
                    timeout=RUN_TIMEOUT_SECONDS,
                )
                self.spans = receiver.spans()
                self.requests = receiver.requests()
                self.fake_requests = fake.requests()
            finally:
                fake.close()
        return self

    @property
    def stdout(self) -> str:
        return self.result.stdout.decode("utf-8", "replace")

    @property
    def stderr(self) -> str:
        return self.result.stderr.decode("utf-8", "replace")

    def blocked(self) -> list[str]:
        if not self.guard_log.exists():
            return []
        return self.guard_log.read_text(encoding="utf-8").splitlines()

    def assert_ran_offline(self) -> None:
        assert not self.result.timed_out, self.stderr[-4000:]
        assert self.result.returncode == 0, self.stderr[-4000:]
        assert self.blocked() == []

    def json_line(self) -> dict[str, Any]:
        lines = [line for line in self.stdout.splitlines() if line.startswith("{")]
        return json.loads(lines[-1])


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Scenario]:
    if not COGNEE_INSTALLED:
        pytest.skip("cognee==1.6.2 is not installed")
    scenarios = [
        Scenario("recipe", APP, [QUESTION, DOCUMENT], tmp_path_factory.mktemp("recipe")),
        Scenario("tracing_off", APP, [QUESTION, DOCUMENT], tmp_path_factory.mktemp("off")),
        Scenario("no_readd", ADD_SCRIPT, ["no-readd"], tmp_path_factory.mktemp("noreadd")),
        Scenario(
            "otlp_env", ADD_SCRIPT, ["otlp-env", *OTLP_URL_FORMS], tmp_path_factory.mktemp("env")
        ),
    ]
    # Importing cognee alone takes about a minute, so the four processes
    # run side by side.
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(scenarios)) as pool:
        done = list(pool.map(Scenario.execute, scenarios))
    by_name = {scenario.name: scenario for scenario in done}
    recipe = by_name["recipe"]
    if os.environ.get("COGNEE_RECORD_FIXTURE") == "1" and recipe.result.returncode == 0:
        FIXTURE.write_text(json.dumps(scrub(recipe.spans), indent=1) + "\n", encoding="utf-8")
    return by_name


def test_recipe_runs_offline_against_the_fakes(runs: dict[str, Scenario]) -> None:
    recipe = runs["recipe"]
    recipe.assert_ran_offline()
    assert ANSWER_MARKER in recipe.stdout
    paths = {request["path"] for request in recipe.fake_requests}
    assert paths == {"/v1/chat/completions", "/v1/embeddings"}


def test_recipe_exports_to_collector_path_with_both_auth_headers(
    runs: dict[str, Scenario],
) -> None:
    requests = runs["recipe"].requests
    assert requests
    for request in requests:
        assert request["path"] == "/tracer/v1/traces"
        assert request["headers"]["x-api-key"] == FI_API_KEY
        assert request["headers"]["x-secret-key"] == FI_SECRET_KEY
        assert request["headers"]["content-type"] == "application/x-protobuf"
        assert LLM_API_KEY not in json.dumps(request["headers"])


def test_recipe_resource_is_an_observe_project(runs: dict[str, Scenario]) -> None:
    resources = [
        resource for request in runs["recipe"].requests for resource in request["resource_attributes"]
    ]
    assert resources
    for resource in resources:
        assert resource["project_name"] == PROJECT
        assert resource["project_type"] == "observe"
    # One provider: every export carries the same register() resource.
    assert len({json.dumps(resource, sort_keys=True) for resource in resources}) == 1


def test_recipe_spans_match_documented_names_and_kinds(runs: dict[str, Scenario]) -> None:
    assert_span_names_and_kinds(runs["recipe"].spans)


def test_recipe_puts_add_cognify_search_in_one_trace(runs: dict[str, Scenario]) -> None:
    assert_one_trace_under_app_span(runs["recipe"].spans)


def test_recipe_does_not_duplicate_spans(runs: dict[str, Scenario]) -> None:
    recipe = runs["recipe"]
    chat_calls = [r for r in recipe.fake_requests if r["path"] == "/v1/chat/completions"]
    embedding_calls = [r for r in recipe.fake_requests if r["path"] == "/v1/embeddings"]
    assert len(spans_named(recipe.spans, GENERATION_SPAN)) == len(chat_calls)
    assert len(spans_named(recipe.spans, EMBEDDING_SPAN)) == len(embedding_calls)
    assert len({span["spanId"] for span in recipe.spans}) == len(recipe.spans)


def test_recipe_spans_carry_the_keys_the_collector_maps(runs: dict[str, Scenario]) -> None:
    assert_collector_mapping_inputs(runs["recipe"].spans)


def test_recipe_exports_content_by_default(runs: dict[str, Scenario]) -> None:
    assert_default_content(runs["recipe"].spans)


def test_recipe_exports_no_secrets(runs: dict[str, Scenario]) -> None:
    recipe = runs["recipe"]
    assert_no_secrets(recipe.spans)
    assert_no_secrets([request["resource_attributes"] for request in recipe.requests])


def test_recipe_run_matches_recorded_fixture(runs: dict[str, Scenario]) -> None:
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert shape(runs["recipe"].spans) == shape(recorded)


def test_tracing_off_exports_no_cognee_spans(runs: dict[str, Scenario]) -> None:
    off = runs["tracing_off"]
    off.assert_ran_offline()
    assert ANSWER_MARKER in off.stdout
    # Only the app's own parent span is left.
    assert [span["name"] for span in off.spans] == [APP_SPAN]


def test_register_without_the_readd_line_exports_nothing(runs: dict[str, Scenario]) -> None:
    no_readd = runs["no_readd"]
    no_readd.assert_ran_offline()
    # Cognee made spans (its in-memory buffer holds them) ...
    assert no_readd.json_line()["buffered_spans"] > 0
    # ... but register()'s exporter was replaced, so none left the process.
    assert no_readd.requests == []
    assert no_readd.spans == []


def test_otlp_env_option_posts_to_the_configured_path(runs: dict[str, Scenario]) -> None:
    otlp = runs["otlp_env"]
    otlp.assert_ran_offline()
    assert otlp.requests
    for request in otlp.requests:
        # Cognee passes the endpoint through unchanged: no second /v1/traces.
        assert request["path"] == "/tracer/v1/traces"
        assert request["headers"]["x-api-key"] == FI_API_KEY
        assert request["headers"]["x-secret-key"] == FI_SECRET_KEY
        for resource in request["resource_attributes"]:
            assert resource["project_name"] == PROJECT
            assert resource["project_type"] == "observe"
            assert resource["service.name"] == "cognee"
    names = [span["name"] for span in otlp.spans]
    assert "memory.store" in names
    assert len(names) == otlp.json_line()["buffered_spans"]
    assert_no_secrets(otlp.spans)


def test_otlp_env_option_also_sends_logs_the_collector_rejects(
    runs: dict[str, Scenario],
) -> None:
    # Cognee derives <endpoint>/../v1/logs from the traces URL; fi-collector
    # (and the Receiver) answer 404 there.
    assert "Failed to export logs batch code: 404" in runs["otlp_env"].stderr


def test_otlp_env_transport_choice_for_documented_urls(runs: dict[str, Scenario]) -> None:
    assert runs["otlp_env"].json_line()["http_only"] == OTLP_URL_FORMS


# --------------------------------------------------------------------------
# Recorded fixture (no Cognee needed)
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def recorded() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def otlp_body(spans: list[dict[str, Any]]) -> dict[str, Any]:
    resource = {"project_name": PROJECT, "project_type": "observe"}
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": key, "value": {"stringValue": value}}
                        for key, value in resource.items()
                    ]
                },
                "scopeSpans": [{"spans": spans}],
            }
        ]
    }


def test_recorded_fixture_round_trips_through_the_harness(recorded: list[dict[str, Any]]) -> None:
    with Receiver() as receiver:
        assert post_otlp(otlp_body(recorded), receiver.collector_endpoint) == 200
        compare(receiver.spans(), FIXTURE)
        (request,) = receiver.requests()
    assert request["path"] == "/tracer/v1/traces"
    assert request["resource_attributes"] == [{"project_name": PROJECT, "project_type": "observe"}]


def test_recorded_fixture_matches_documented_mapping(recorded: list[dict[str, Any]]) -> None:
    assert_span_names_and_kinds(recorded)
    assert_one_trace_under_app_span(recorded)
    assert_collector_mapping_inputs(recorded)
    assert_default_content(recorded)
    assert_no_secrets(recorded)
    assert "/Users/" not in json.dumps(recorded)


def test_readme_states_what_the_tests_check() -> None:
    readme = README.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    line = "provider.add_span_processor(BatchSpanProcessor())"
    assert line in app and line in readme
    for fact in (
        "cognee==1.6.2",
        "COGNEE_TRACING_ENABLED",
        "/tracer/v1/traces",
        "X-Api-Key",
        "X-Secret-Key",
        "project_type=observe",
        "https://api.futureagi.com:443/tracer/v1/traces",
        *CONTENT_KEYS[QUERY_MARKER],
        *CONTENT_KEYS[ANSWER_MARKER],
        *SPAN_KINDS,
    ):
        assert fact in readme, fact
