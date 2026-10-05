"""Contract test for the Cognee tracing recipe.

Runs ``src/app.py`` as written with the real ``cognee`` 1.6.2 and traceAI's
``register()``, plus two one-call scenario scripts, each in a subprocess whose
network, and that of the worker processes Cognee spawns, is limited to
127.0.0.1 (``loopback_guard/sitecustomize.py``). Cognee's LLM and
embedding endpoints are a loopback fake of the OpenAI API
(``_fake_openai.py``). Spans go to the shared harness ``Receiver``, which
serves ``/v1/traces`` and ``/tracer/v1/traces`` like fi-collector's HTTP mux
but does not authenticate, stamp projects or store anything.

The recorded fixture (``fixtures/cognee-1.6.2-recipe-spans.json``) is
Cognee's own spans from the recipe run, before the export filter
(``src/cognee_filter.py``) changes them. The fixture tests post it with
``post_otlp()`` and check it with ``compare()``, run the filter over it, and
compare the live runs with the filtered result, without Cognee installed.
Regenerate it with ``COGNEE_RECORD_FIXTURE=1``.

All keys are placeholders. Nothing here contacts an LLM provider, Cognee's
telemetry host or Future AGI.
"""

from __future__ import annotations

import base64
import concurrent.futures
import copy
import importlib.util
import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags

from harness import Receiver, compare, post_otlp, run

TESTS_DIR = Path(__file__).resolve().parent
RECIPE_DIR = TESTS_DIR.parent
SRC_DIR = RECIPE_DIR / "src"
PYTHON_DIR = RECIPE_DIR.parents[1]
REPO_ROOT = PYTHON_DIR.parent
APP = SRC_DIR / "app.py"
README = RECIPE_DIR / "README.md"
REQUIREMENTS = RECIPE_DIR / "requirements.txt"
ADD_SCRIPT = TESTS_DIR / "cognee_add.py"
# Put first on a scenario's PYTHONPATH, so every interpreter it starts loads the guard.
GUARD_DIR = TESTS_DIR / "loopback_guard"
FIXTURE = TESTS_DIR / "fixtures" / "cognee-1.6.2-recipe-spans.json"

sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(SRC_DIR))
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
MARKERS = (DOC_MARKER, QUERY_MARKER, ANSWER_MARKER)

# Which attribute keys carry each kind of content in Cognee's own spans
# (README.md, "Content"). With capture_content=True the filter keeps them.
CONTENT_KEYS = {
    DOC_MARKER: {"langfuse.observation.input"},
    QUERY_MARKER: {"langfuse.observation.input", "cognee.search.query", "memory.query.text"},
    ANSWER_MARKER: {"langfuse.observation.output"},
}
# What the filter removes by default: every key above, plus the graph query
# text (cognee.db.query, which held no marker in the recorded run).
FILTERED_KEYS = set().union(*CONTENT_KEYS.values()) | {"cognee.db.query"}
# What the filter puts in place of error text by default.
DETAIL_REMOVED = "__REDACTED__ (content capture off)"
TYPE_ONLY = " (detail removed: content capture off)"
TRACEBACK_HEADER = "Traceback (most recent call last):"

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
# The fi.span.kind the filter sets, by span name, on the recipe run's spans.
FILTER_KINDS = {
    "memory.retrieve": "RETRIEVER",
    "cognee.search.authorize": "RETRIEVER",
    "cognee.search.dataset": "RETRIEVER",
    GENERATION_SPAN: "LLM",
}
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


def host_paths() -> tuple[str, ...]:
    """Machine-local path fragments that must not reach a recorded fixture."""
    return (str(Path.home()), "/Users/", "/home/", "/tmp/pytest-of-", "/var/folders", "/private/")


def scrub(spans: list[dict[str, Any]], tmp_base: Path) -> list[dict[str, Any]]:
    """Replace machine-local paths (stack traces, data dirs) before recording."""
    text = json.dumps(spans)
    for path, label in (
        (os.path.realpath(tmp_base), "<tmp>"),
        (str(tmp_base), "<tmp>"),
        (str(REPO_ROOT), "<repo>"),
    ):
        text = text.replace(path, label)
    text = re.sub(r'File \\"[^"\\]*?/lib/python3\.\d+/', r'File \\"<python-lib>/', text)
    text = text.replace(str(Path.home()), "<home>")
    return json.loads(text)


def without_filter_kinds(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the fi.span.kind the filter adds, leaving Cognee's own spans."""
    spans = copy.deepcopy(spans)
    for span in spans:
        span["attributes"] = [a for a in span.get("attributes", []) if a["key"] != "fi.span.kind"]
    return spans


def assert_span_names_and_kinds(spans: list[dict[str, Any]]) -> None:
    assert {span["name"]: span["kind"] for span in spans} == SPAN_KINDS
    for span in spans:
        assert span["kind"] == SPAN_KINDS[span["name"]], span["name"]


def assert_collector_mapping_inputs(spans: list[dict[str, Any]], kinds: dict[str, str]) -> None:
    """Assert the keys fi-collector maps from, as README.md's table states.

    ``kinds`` is the fi.span.kind expected per span name: empty for Cognee's
    own spans, FILTER_KINDS after the recipe's export filter.
    """
    for span in spans:
        attrs = attributes(span)
        expected_kind = {"fi.span.kind": kinds[span["name"]]} if span["name"] in kinds else {}
        assert {key: attrs[key] for key in SPAN_KIND_KEYS if key in attrs} == expected_kind
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
    """Among span attributes, Cognee's content is under exactly the documented keys.

    Error text (exception events, status) is not checked here: with content
    capture on it is exported as Cognee recorded it (README.md, "Content").
    """
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


def found_in(payload: Any, needles: Any) -> list[str]:
    """The needles that occur in payload's JSON (a list keeps pytest's diff of it small)."""
    text = json.dumps(payload)
    return [needle for needle in needles if needle in text]


def assert_no_content(spans: list[dict[str, Any]]) -> None:
    """No marker anywhere on the wire (attributes, status, events), no content key."""
    assert found_in(spans, MARKERS) == []
    for span in spans:
        keys = set(attributes(span))
        for event in span.get("events", []):
            keys |= set(attributes(event))
        assert not keys & FILTERED_KEYS, span["name"]


def assert_no_secrets(payload: Any) -> None:
    assert found_in(payload, (FI_API_KEY, FI_SECRET_KEY, LLM_API_KEY)) == []


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
# Recorded spans as SDK spans, sent through the export filter
# --------------------------------------------------------------------------


def otlp_value(value: dict[str, Any]) -> Any:
    ((kind, raw),) = value.items()
    if kind == "intValue":
        return int(raw)
    if kind == "arrayValue":
        return [otlp_value(item) for item in raw.get("values", [])]
    return raw


def sdk_span(span: dict[str, Any]) -> ReadableSpan:
    """Rebuild an SDK span from its Receiver-decoded (OTLP JSON) form."""

    def span_id(key: str) -> int:
        return int.from_bytes(base64.b64decode(span[key]), "big")

    def flat(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {item["key"]: otlp_value(item["value"]) for item in items}

    trace_id = span_id("traceId")
    parent = None
    if span.get("parentSpanId"):
        parent = SpanContext(trace_id, span_id("parentSpanId"), is_remote=False)
    status = span.get("status", {})
    return ReadableSpan(
        name=span["name"],
        context=SpanContext(
            trace_id, span_id("spanId"), is_remote=False, trace_flags=TraceFlags(TraceFlags.SAMPLED)
        ),
        parent=parent,
        resource=Resource({"project_name": PROJECT, "project_type": "observe"}),
        attributes=flat(span.get("attributes", [])),
        events=[
            Event(event["name"], flat(event.get("attributes", [])), int(event["timeUnixNano"]))
            for event in span.get("events", [])
        ],
        kind=SpanKind[span["kind"].replace("SPAN_KIND_", "")],
        status=Status(
            StatusCode[status.get("code", "STATUS_CODE_UNSET").replace("STATUS_CODE_", "")],
            status.get("message"),
        ),
        start_time=int(span["startTimeUnixNano"]),
        end_time=int(span["endTimeUnixNano"]),
    )


def export_spans(spans: list[dict[str, Any]], wrap: Any = None) -> list[dict[str, Any]]:
    """Export recorded spans over OTLP/HTTP, optionally through ``wrap(exporter)``."""
    with Receiver() as receiver:
        exporter = OTLPSpanExporter(endpoint=receiver.collector_endpoint)
        if wrap is not None:
            exporter = wrap(exporter)
        assert exporter.export([sdk_span(span) for span in spans]) == SpanExportResult.SUCCESS
        exporter.shutdown()
        return receiver.spans()


def filtered(
    cognee_filter: Any, spans: list[dict[str, Any]], **options: Any
) -> list[dict[str, Any]]:
    """Export recorded spans through src/cognee_filter.py; return what arrives."""
    return export_spans(
        spans, lambda exporter: cognee_filter.CogneeExportFilter(exporter, **options)
    )


def without_error_detail(span: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """A recorded span's events and status as the filter should export them by default.

    Default content-off mode keeps the fixed marker, exception type and
    exception.escaped, but omits serialized exception stack traces entirely.
    """
    events = copy.deepcopy(span.get("events", []))
    for event in events:
        event["attributes"] = [
            attribute
            for attribute in event["attributes"]
            if attribute["key"] != "exception.stacktrace"
        ]
        for attribute in event["attributes"]:
            if attribute["key"] == "exception.message":
                attribute["value"] = {"stringValue": DETAIL_REMOVED}
    status = dict(span.get("status", {}))
    if "message" in status:
        status["message"] = attributes(events[-1])["exception.type"] + TYPE_ONLY
    return events, status


def filtered_attributes(span: dict[str, Any], capture_content: bool) -> dict[str, Any]:
    """A recorded span's attributes as the filter should export them."""
    expected = {
        key: value
        for key, value in attributes(span).items()
        if capture_content or key not in FILTERED_KEYS
    }
    if span["name"] in FILTER_KINDS:
        expected["fi.span.kind"] = FILTER_KINDS[span["name"]]
    return expected


@pytest.fixture(scope="module")
def cognee_filter() -> Any:
    """src/cognee_filter.py. Imported here so a missing filter fails only its tests."""
    import cognee_filter as module

    return module


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
            "PYTHONPATH": os.pathsep.join((str(GUARD_DIR), str(PYTHON_DIR))),
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
        if self.name == "capture":
            env["COGNEE_FI_CAPTURE_CONTENT"] = "true"
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
                    [sys.executable, str(self.script), *self.args],
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

    def guard_records(self) -> list[dict[str, Any]]:
        if not self.guard_log.exists():
            return []
        lines = self.guard_log.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def blocked(self) -> list[dict[str, Any]]:
        return [record for record in self.guard_records() if record["kind"] != "installed"]

    def guarded_workers(self) -> list[dict[str, Any]]:
        """Processes spawned by multiprocessing that loaded the guard."""
        return [
            record
            for record in self.guard_records()
            if record["kind"] == "installed" and "--multiprocessing-fork" in record["argv"]
        ]

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
        Scenario("capture", APP, [QUESTION, DOCUMENT], tmp_path_factory.mktemp("capture")),
        Scenario("tracing_off", APP, [QUESTION, DOCUMENT], tmp_path_factory.mktemp("off")),
        Scenario("no_readd", ADD_SCRIPT, ["no-readd"], tmp_path_factory.mktemp("noreadd")),
        Scenario(
            "otlp_env", ADD_SCRIPT, ["otlp-env", *OTLP_URL_FORMS], tmp_path_factory.mktemp("env")
        ),
    ]
    # Importing cognee alone takes about a minute, so the processes run side
    # by side.
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(scenarios)) as pool:
        done = list(pool.map(Scenario.execute, scenarios))
    by_name = {scenario.name: scenario for scenario in done}
    capture = by_name["capture"]
    if os.environ.get("COGNEE_RECORD_FIXTURE") == "1" and capture.result.returncode == 0:
        # The fixture is Cognee's own output: the capture run (all content
        # kept) without the fi.span.kind the filter adds.
        spans = scrub(without_filter_kinds(capture.spans), tmp_path_factory.getbasetemp())
        FIXTURE.write_text(json.dumps(spans, indent=1) + "\n", encoding="utf-8")
    return by_name


def test_recipe_runs_offline_against_the_fakes(runs: dict[str, Scenario]) -> None:
    recipe = runs["recipe"]
    recipe.assert_ran_offline()
    assert ANSWER_MARKER in recipe.stdout
    paths = {request["path"] for request in recipe.fake_requests}
    assert paths == {"/v1/chat/completions", "/v1/embeddings"}


def test_loopback_guard_also_runs_in_cognee_worker_processes(runs: dict[str, Scenario]) -> None:
    # Cognee 1.6.2 runs LanceDB and Ladybug in multiprocessing "spawn"
    # workers; the guard must be installed there too, not only in the
    # scenario's main process (each scenario's assert_ran_offline checks
    # that nothing was refused).
    for name, scenario in runs.items():
        assert scenario.guarded_workers(), name


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


def test_recipe_marks_search_and_llm_spans_for_the_collector(runs: dict[str, Scenario]) -> None:
    # memory.retrieve and cognee.search.* arrive as RETRIEVER, LLM calls as
    # LLM; embedding spans keep gen_ai.operation.name only.
    assert_collector_mapping_inputs(runs["recipe"].spans, FILTER_KINDS)


def test_recipe_exports_no_content_by_default(runs: dict[str, Scenario]) -> None:
    assert_no_content(runs["recipe"].spans)


def test_recipe_exports_error_type_without_stacktrace_by_default(
    runs: dict[str, Scenario],
) -> None:
    # Cognee's vector-search probes fail in every run (CollectionNotFoundError,
    # naming the collection). With capture off, only fixed error markers and
    # the exception type arrive; with capture on, the error is unchanged.
    for name, detail_kept in (("recipe", False), ("capture", True)):
        errors = [
            span
            for span in runs[name].spans
            if span.get("status", {}).get("code") == "STATUS_CODE_ERROR"
        ]
        assert errors, name
        for span in errors:
            assert span["events"], span["name"]
            for event in span["events"]:
                event_attrs = attributes(event)
                exception_type = event_attrs["exception.type"]
                if detail_kept:
                    assert "not found" in event_attrs["exception.message"]
                    assert "not found" in span["status"]["message"]
                    assert "not found" in event_attrs["exception.stacktrace"].splitlines()[-1]
                    continue
                assert event_attrs["exception.message"] == DETAIL_REMOVED
                assert span["status"]["message"] == exception_type + TYPE_ONLY
                assert "exception.stacktrace" not in event_attrs


def test_recipe_exports_no_secrets(runs: dict[str, Scenario]) -> None:
    recipe = runs["recipe"]
    assert_no_secrets(recipe.spans)
    assert_no_secrets([request["resource_attributes"] for request in recipe.requests])


def test_recipe_run_matches_recorded_fixture(
    runs: dict[str, Scenario], recorded: list[dict[str, Any]], cognee_filter: Any
) -> None:
    assert shape(runs["recipe"].spans) == shape(filtered(cognee_filter, recorded))


def test_capture_content_exports_content_under_documented_keys(
    runs: dict[str, Scenario],
) -> None:
    capture = runs["capture"]
    capture.assert_ran_offline()
    assert_default_content(capture.spans)
    assert_no_secrets(capture.spans)


def test_capture_run_matches_recorded_fixture(
    runs: dict[str, Scenario], recorded: list[dict[str, Any]], cognee_filter: Any
) -> None:
    capture = runs["capture"]
    assert_collector_mapping_inputs(capture.spans, FILTER_KINDS)
    assert shape(capture.spans) == shape(filtered(cognee_filter, recorded, capture_content=True))


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
    # Cognee's own spans: no span-kind key, content under the documented keys.
    assert_span_names_and_kinds(recorded)
    assert_one_trace_under_app_span(recorded)
    assert_collector_mapping_inputs(recorded, {})
    assert_default_content(recorded)
    assert_no_secrets(recorded)
    assert found_in(recorded, host_paths()) == []


def test_recorded_error_spans_are_vector_search_probes(recorded: list[dict[str, Any]]) -> None:
    errors = [s for s in recorded if s.get("status", {}).get("code") == "STATUS_CODE_ERROR"]
    searches = spans_named(recorded, "cognee.db.vector.search")
    assert {span["name"] for span in errors} == {"cognee.db.vector.search"}
    for span in errors:
        assert [event["name"] for event in span["events"]] == ["exception"]
    assert "{0} of {1}".format(len(errors), len(searches)) in README.read_text(encoding="utf-8")


def test_scrub_removes_host_and_tmp_paths(tmp_path_factory: pytest.TempPathFactory) -> None:
    base = tmp_path_factory.getbasetemp()
    planted = [
        str(base / "recipe0" / "data" / "file.txt"),
        os.path.realpath(base) + "/recipe0/system",
        str(REPO_ROOT / "python" / "x.py"),
        str(Path.home() / ".cache" / "uv" / "x.py"),
        'File "{0}/.venv/lib/python3.11/site-packages/x.py"'.format(Path.home()),
    ]
    scrubbed = scrub([{"name": "x", "attributes": [], "planted": planted}], base)
    assert found_in(scrubbed, host_paths()) == []


def test_fixture_spans_rebuild_as_sdk_spans(recorded: list[dict[str, Any]]) -> None:
    # The helper the filter tests rely on: rebuilt spans export back to the
    # same names, attributes, statuses and events.
    wire = export_spans(recorded)
    compare(wire, FIXTURE)
    for before, after in zip(recorded, wire):
        assert after.get("events", []) == before.get("events", []), before["name"]


# --------------------------------------------------------------------------
# The export filter (src/cognee_filter.py), on the recorded spans
# --------------------------------------------------------------------------


def test_filter_lists_the_tested_content_keys_and_span_names(cognee_filter: Any) -> None:
    assert cognee_filter.CONTENT_KEYS == FILTERED_KEYS
    assert cognee_filter.SPAN_KIND_KEYS == SPAN_KIND_KEYS
    for name in SPAN_KINDS:
        assert cognee_filter.span_kind(name) == FILTER_KINDS.get(name), name


def test_filter_keeps_content_off_the_wire_by_default(
    cognee_filter: Any, recorded: list[dict[str, Any]]
) -> None:
    wire = filtered(cognee_filter, recorded)
    assert len(wire) == len(recorded)
    assert_no_content(wire)
    # Error text is replaced and serialized exception stack traces are omitted;
    # everything else is unchanged.
    for before, after in zip(recorded, wire):
        assert attributes(after) == filtered_attributes(before, capture_content=False)
        events, status = without_error_detail(before)
        assert after.get("events", []) == events
        assert after.get("status") == status


def test_filter_sets_span_kinds_only_where_none_is_set(
    cognee_filter: Any, recorded: list[dict[str, Any]]
) -> None:
    assert_collector_mapping_inputs(filtered(cognee_filter, recorded), FILTER_KINDS)
    (search,) = copy.deepcopy(spans_named(recorded, "memory.retrieve"))
    search["attributes"].append({"key": "gen_ai.span.kind", "value": {"stringValue": "CHAIN"}})
    (wire,) = filtered(cognee_filter, [search])
    assert attributes(wire)["gen_ai.span.kind"] == "CHAIN"
    assert "fi.span.kind" not in attributes(wire)


def test_filter_removes_error_text_quoting_content(
    cognee_filter: Any, recorded: list[dict[str, Any]]
) -> None:
    # Cognee did not quote content in errors in the recorded run; plant it.
    (search,) = copy.deepcopy(spans_named(recorded, "memory.retrieve"))
    generation = copy.deepcopy(
        next(s for s in spans_named(recorded, GENERATION_SPAN) if DOC_MARKER in json.dumps(s))
    )
    for span, quoted in ((search, QUESTION), (generation, DOCUMENT)):
        span["status"] = {"code": "STATUS_CODE_ERROR", "message": "ValueError: bad " + quoted}
        span["events"] = [
            exception_event(
                "ValueError",
                "bad " + quoted,
                '{0}\n  File "/app/x.py", line 3, in run\nValueError: bad {1}\n'.format(
                    TRACEBACK_HEADER, quoted
                ),
                span["endTimeUnixNano"],
            )
        ]
    wire = filtered(cognee_filter, [search, generation])
    assert_no_content(wire)
    for span in wire:
        assert span["status"]["message"] == "ValueError" + TYPE_ONLY
        assert attributes(span["events"][0]) == {
            "exception.type": "ValueError",
            "exception.message": DETAIL_REMOVED,
            "exception.escaped": "False",
        }


# A content-policy rejection as Cognee 1.6.2 raises it (litellm_native/
# native_adapter.py:512-518): the message quotes the whole prompt. The payload
# is longer than Cognee's 8000-character attribute cap (get_observe.py:12, :46)
# and has several lines; the marker is on every line, so also past the cap.
PAYLOAD_MARKER = "PMARK2c6d"
PAYLOAD = "\n".join([PAYLOAD_MARKER + " Ada works on the Lighthouse project."] * 220)
POLICY_ERROR = "cognee.infrastructure.llm.exceptions.ContentPolicyFilterError"
TASK_SPAN = "cognee.pipeline.task.extract_graph_and_summarize"
CHAIN_FRAME_MARKER = "PROMPT_SECRET_FIXTURE_ONLY"
# This is the exact separator, blank line, header and fake frame from
# tickets/TH-8328/fix-r3/red-probe.py. It is quoted by the exception message
# below, so it also occurs in the serialized exception.stacktrace.
FORGED_CHAIN_FRAME = "\n".join(
    (
        "The above exception was the direct cause of the following exception:",
        "",
        TRACEBACK_HEADER,
        '  File "/private/PROMPT_SECRET_FIXTURE_ONLY.py", line 1, in '
        "PROMPT_SECRET_FIXTURE_ONLY",
        "end of quoted prompt",
    )
)
FORGED_CHAIN_PAYLOAD = ("ordinary oversized prompt text " * 320) + "\n" + FORGED_CHAIN_FRAME


class ContentPolicyViolationError(Exception):
    """Stands in for litellm's exception of that name."""


class ContentPolicyFilterError(Exception):
    """Stands in for Cognee's ContentPolicyFilterError."""


def raise_policy_rejection(payload: str = PAYLOAD) -> None:
    try:
        raise ContentPolicyViolationError("litellm.ContentPolicyViolationError: content_filter")
    except ContentPolicyViolationError as error:
        # str() of Cognee's error: CogneeApiError.__str__ (cognee/exceptions/exceptions.py:62-66).
        raise ContentPolicyFilterError(
            "CogneeValidationError: The provided input contains content that is not aligned "
            "with our content policy: {0} (Status code: 422)".format(payload)
        ) from error


def exception_event(type_: str, message: str, stacktrace: str, time: str) -> dict[str, Any]:
    """The event OpenTelemetry's record_exception() adds, in OTLP JSON form."""
    return {
        "timeUnixNano": time,
        "name": "exception",
        "attributes": [
            {"key": "exception.type", "value": {"stringValue": type_}},
            {"key": "exception.message", "value": {"stringValue": message}},
            {"key": "exception.stacktrace", "value": {"stringValue": stacktrace}},
            {"key": "exception.escaped", "value": {"stringValue": "False"}},
        ],
    }


def policy_rejection_spans(
    recorded: list[dict[str, Any]], payload: str = PAYLOAD
) -> list[dict[str, Any]]:
    """A recorded LLM span and its pipeline task span, as a content-policy rejection ends them.

    The LLM span's input attribute is the prompt JSON cut at 8000 characters,
    which does not parse. The task span has no content attribute and records
    the error twice: run_tasks_base.py:243-245, then new_span's
    start_as_current_span (observability/__init__.py:110-118).
    """
    llm = copy.deepcopy(
        next(
            span
            for span in spans_named(recorded, GENERATION_SPAN)
            for parent in recorded
            if parent["spanId"] == span["parentSpanId"] and parent["name"] == TASK_SPAN
        )
    )
    (task,) = [copy.deepcopy(s) for s in recorded if s["spanId"] == llm["parentSpanId"]]
    try:
        raise_policy_rejection(payload)
    except ContentPolicyFilterError as raised:
        error = raised
    stacktrace = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    prompt = json.dumps({"text_input": payload, "system_prompt": "Extract a graph."})[:8000]
    with pytest.raises(ValueError):
        json.loads(prompt)
    for attribute in llm["attributes"]:
        if attribute["key"] == "langfuse.observation.input":
            attribute["value"] = {"stringValue": prompt}
    for span, recorded_times in ((llm, 1), (task, 2)):
        span["status"] = {
            "code": "STATUS_CODE_ERROR",
            "message": "ContentPolicyFilterError: " + str(error),
        }
        span["events"] = [
            exception_event(POLICY_ERROR, str(error), stacktrace, span["endTimeUnixNano"])
        ] * recorded_times
    return [llm, task]


def test_filter_removes_error_detail_on_every_span_by_default(
    cognee_filter: Any, recorded: list[dict[str, Any]]
) -> None:
    # N1: the rejection quotes the whole prompt in the exception message, the
    # stack trace and the status, on the LLM span (whose cut-off input does not
    # parse) and again on its pipeline task span (which has no content key).
    spans = policy_rejection_spans(recorded)
    assert found_in(spans, [PAYLOAD_MARKER]) == [PAYLOAD_MARKER]
    wire = filtered(cognee_filter, spans)
    assert [span["name"] for span in wire] == [GENERATION_SPAN, TASK_SPAN]
    assert found_in(wire, [PAYLOAD_MARKER]) == []
    assert_no_content(wire)
    for before, after in zip(spans, wire):
        assert after["status"] == {
            "code": "STATUS_CODE_ERROR",
            "message": POLICY_ERROR + TYPE_ONLY,
        }
        assert len(after["events"]) == len(before["events"])
        for event_after in after["events"]:
            event_attrs = attributes(event_after)
            assert event_attrs["exception.type"] == POLICY_ERROR
            assert event_attrs["exception.escaped"] == "False"
            assert event_attrs["exception.message"] == DETAIL_REMOVED
            assert "exception.stacktrace" not in event_attrs


def test_filter_capture_content_keeps_error_detail(
    cognee_filter: Any, recorded: list[dict[str, Any]]
) -> None:
    spans = policy_rejection_spans(recorded)
    wire = filtered(cognee_filter, spans, capture_content=True)
    for before, after in zip(spans, wire):
        assert attributes(after) == filtered_attributes(before, capture_content=True)
        assert after["events"] == before["events"]
        assert after["status"] == before["status"]


def test_filter_omits_forged_chained_traceback_on_every_span_by_default(
    cognee_filter: Any, recorded: list[dict[str, Any]]
) -> None:
    """Serialized exception text has no trustworthy frame provenance.

    The content-policy exception quotes an oversized prompt on the recorded
    LLM span and its pipeline parent. Its quoted text contains the exact
    traceback-shaped sequence from the red probe, including the forged frame.
    """
    assert len(FORGED_CHAIN_PAYLOAD) > 8000
    spans = policy_rejection_spans(recorded, FORGED_CHAIN_PAYLOAD)
    assert found_in(spans, [CHAIN_FRAME_MARKER]) == [CHAIN_FRAME_MARKER]

    wire = filtered(cognee_filter, spans)
    assert [span["name"] for span in wire] == [GENERATION_SPAN, TASK_SPAN]
    assert found_in(wire, [CHAIN_FRAME_MARKER]) == []
    for span in wire:
        assert span["status"] == {
            "code": "STATUS_CODE_ERROR",
            "message": POLICY_ERROR + TYPE_ONLY,
        }
        for event in span["events"]:
            event_attrs = attributes(event)
            assert event_attrs["exception.type"] == POLICY_ERROR
            assert event_attrs["exception.escaped"] == "False"
            assert event_attrs["exception.message"] == DETAIL_REMOVED
            assert "exception.stacktrace" not in event_attrs

    captured = filtered(cognee_filter, spans, capture_content=True)
    for before, after in zip(spans, captured):
        assert attributes(after) == filtered_attributes(before, capture_content=True)
        assert after["events"] == before["events"]
        assert after["status"] == before["status"]
        assert found_in(after, [CHAIN_FRAME_MARKER]) == [CHAIN_FRAME_MARKER]


def test_filter_removes_detail_of_a_raised_chained_exception(cognee_filter: Any) -> None:
    # The real record_exception() and use_span() output on this Python version,
    # for a chained exception with a multi-line message.
    filtered_out, captured_out, raw_out = (
        InMemorySpanExporter(),
        InMemorySpanExporter(),
        InMemorySpanExporter(),
    )
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(cognee_filter.CogneeExportFilter(filtered_out)))
    provider.add_span_processor(
        SimpleSpanProcessor(cognee_filter.CogneeExportFilter(captured_out, capture_content=True))
    )
    provider.add_span_processor(SimpleSpanProcessor(raw_out))
    with pytest.raises(ContentPolicyFilterError):
        with provider.get_tracer("test").start_as_current_span(GENERATION_SPAN):
            raise_policy_rejection()
    (raw,) = raw_out.get_finished_spans()
    (clean,) = filtered_out.get_finished_spans()
    (captured,) = captured_out.get_finished_spans()
    assert PAYLOAD_MARKER in raw.status.description
    exported = {
        "attributes": dict(clean.attributes),
        "status": clean.status.description,
        "events": [[event.name, dict(event.attributes)] for event in clean.events],
    }
    assert found_in(exported, [PAYLOAD_MARKER]) == []
    (event,) = clean.events
    exception_type = event.attributes["exception.type"]
    assert exception_type.endswith(".ContentPolicyFilterError")
    assert clean.status.description == exception_type + TYPE_ONLY
    assert "exception.stacktrace" not in event.attributes
    # Capture mode keeps all content and error detail; only the span-kind
    # mapping (applied in both modes) is added.
    assert cognee_filter.span_kind(GENERATION_SPAN) == "LLM"
    assert dict(captured.attributes) == {**dict(raw.attributes), "fi.span.kind": "LLM"}
    assert captured.events == raw.events
    assert captured.status == raw.status


MARK = "TMARK5e0a"


@pytest.mark.parametrize(
    ("trace", "exception_type"),
    [
        pytest.param(
            [
                TRACEBACK_HEADER,
                '  File "/app/client.py", line 12, in post',
                "    raise ContentPolicyViolationError(body)  # " + MARK,
                "    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^",
                "litellm.ContentPolicyViolationError: rejected " + MARK,
                "a second message line " + MARK,
                '  File "/forged.py", line 1, in ' + MARK,
                "",
                "The above exception was the direct cause of the following exception:",
                "",
                TRACEBACK_HEADER,
                '  File "/app/adapter.py", line 515, in acreate_structured_output',
                "    raise ContentPolicyFilterError(message) from error",
                "cognee.ContentPolicyFilterError: policy " + MARK,
                MARK + " on a second line",
            ],
            "cognee.ContentPolicyFilterError",
            id="direct-cause-multi-line-messages",
        ),
        pytest.param(
            [
                TRACEBACK_HEADER,
                '  File "/app/a.py", line 3, in run',
                "    step(" + MARK + ")",
                '  File "/app/a.py", line 7, in step',
                "    step(" + MARK + ")",
                "  [Previous line repeated 2 more times]",
                "KeyError: '" + MARK + "'",
                "",
                "During handling of the above exception, another exception occurred:",
                "",
                TRACEBACK_HEADER,
                '  File "/app/a.py", line 5, in run',
                "    raise ValueError(" + MARK + ")",
                "ValueError: " + MARK,
                "a note added with add_note() " + MARK,
            ],
            "ValueError",
            id="during-handling-with-repeats-and-notes",
        ),
        pytest.param(
            [
                "ValueError: " + MARK,
                "",
                "The above exception was the direct cause of the following exception:",
                "",
                TRACEBACK_HEADER,
                '  File "/app/a.py", line 3, in run',
                "RuntimeError: " + MARK,
            ],
            "RuntimeError",
            id="cause-without-traceback",
        ),
        pytest.param(
            [
                TRACEBACK_HEADER,
                '  File "/app/a.py", line 3, in run',
                "ValueError: " + MARK,
                TRACEBACK_HEADER,
                '  File "/app/' + MARK + '.py", line 1, in ' + MARK,
                MARK,
            ],
            "ValueError",
            id="message-that-looks-like-a-traceback",
        ),
        pytest.param(
            [
                "  + Exception Group Traceback (most recent call last):",
                '  |   File "/app/a.py", line 3, in run',
                '  |     raise ExceptionGroup("' + MARK + '", errors)',
                "  | ExceptionGroup: " + MARK + " (1 sub-exception)",
                "  +-+---------------- 1 ----------------",
                "    | ValueError: " + MARK,
                "    +------------------------------------",
            ],
            "ExceptionGroup",
            id="unrecognised-layout",
        ),
    ],
)


def test_filter_omits_serialized_stacktraces_but_capture_preserves_them(
    cognee_filter: Any, trace: list[str], exception_type: str
) -> None:
    exporter, captured_exporter = InMemorySpanExporter(), InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(cognee_filter.CogneeExportFilter(exporter)))
    provider.add_span_processor(
        SimpleSpanProcessor(cognee_filter.CogneeExportFilter(captured_exporter, capture_content=True))
    )
    with provider.get_tracer("test").start_as_current_span(TASK_SPAN) as span:
        span.add_event(
            "exception",
            {
                "exception.type": exception_type,
                "exception.message": MARK,
                "exception.stacktrace": "\n".join(trace) + "\n",
                "exception.escaped": "False",
            },
        )
    (exported,) = exporter.get_finished_spans()
    (captured,) = captured_exporter.get_finished_spans()
    assert dict(exported.events[0].attributes) == {
        "exception.type": exception_type,
        "exception.message": DETAIL_REMOVED,
        "exception.escaped": "False",
    }
    assert found_in(dict(exported.events[0].attributes), [MARK]) == []
    assert dict(captured.events[0].attributes) == {
        "exception.type": exception_type,
        "exception.message": MARK,
        "exception.stacktrace": "\n".join(trace) + "\n",
        "exception.escaped": "False",
    }


def test_filter_keeps_only_known_keys_and_non_text_values_in_events(cognee_filter: Any) -> None:
    # Cognee 1.6.2 adds no events other than "exception"; anything else gets
    # the same allowlist.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(cognee_filter.CogneeExportFilter(exporter)))
    with provider.get_tracer("test").start_as_current_span(APP_SPAN) as span:
        span.add_event(
            "retry",
            {
                "attempt": 2,
                "delay.seconds": 1.5,
                "final": False,
                "scores": [0.5, 1.0],
                "reason": "rejected " + MARK,
                "inputs": ["first " + MARK, "second"],
                "exception.type": "ValueError",
            },
        )
        span.set_status(Status(StatusCode.ERROR, "gave up on " + MARK))
    (exported,) = exporter.get_finished_spans()
    assert dict(exported.events[0].attributes) == {
        "attempt": 2,
        "delay.seconds": 1.5,
        "final": False,
        "scores": (0.5, 1.0),
        "exception.type": "ValueError",
    }
    # An event that is not named "exception" does not give the status a type.
    assert exported.status.description == DETAIL_REMOVED


def test_filter_capture_content_keeps_cognees_content(
    cognee_filter: Any, recorded: list[dict[str, Any]]
) -> None:
    wire = filtered(cognee_filter, recorded, capture_content=True)
    assert_default_content(wire)
    assert_collector_mapping_inputs(wire, FILTER_KINDS)
    for before, after in zip(recorded, wire):
        assert attributes(after) == filtered_attributes(before, capture_content=True)


@pytest.mark.parametrize(
    ("env_value", "argument", "kept"),
    [
        (None, None, False),
        ("true", None, True),
        ("TRUE", None, True),
        ("false", None, False),
        ("true", False, False),
        (None, True, True),
    ],
)
def test_filter_capture_switch(
    cognee_filter: Any, monkeypatch: pytest.MonkeyPatch, env_value: Any, argument: Any, kept: bool
) -> None:
    if env_value is None:
        monkeypatch.delenv("COGNEE_FI_CAPTURE_CONTENT", raising=False)
    else:
        monkeypatch.setenv("COGNEE_FI_CAPTURE_CONTENT", env_value)
    exporter = InMemorySpanExporter()
    options = {} if argument is None else {"capture_content": argument}
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(cognee_filter.CogneeExportFilter(exporter, **options))
    )
    with provider.get_tracer("test").start_as_current_span("memory.retrieve") as span:
        span.set_attribute("memory.query.text", QUESTION)
    (exported,) = exporter.get_finished_spans()
    assert ("memory.query.text" in exported.attributes) is kept


def test_filter_does_not_change_spans_other_processors_see(cognee_filter: Any) -> None:
    filtered_out, raw_out = InMemorySpanExporter(), InMemorySpanExporter()
    provider = TracerProvider()
    # The filter runs first; the plain exporter after it must see the original.
    provider.add_span_processor(SimpleSpanProcessor(cognee_filter.CogneeExportFilter(filtered_out)))
    provider.add_span_processor(SimpleSpanProcessor(raw_out))
    with provider.get_tracer("test").start_as_current_span("memory.retrieve") as span:
        span.set_attribute("memory.query.text", QUESTION)
        span.add_event("exception", {"exception.message": "bad " + QUESTION})
        span.set_status(Status(StatusCode.ERROR, "bad " + QUESTION))
    (raw,) = raw_out.get_finished_spans()
    (clean,) = filtered_out.get_finished_spans()
    assert dict(raw.attributes) == {"memory.query.text": QUESTION}
    assert dict(raw.events[0].attributes) == {"exception.message": "bad " + QUESTION}
    assert raw.status.description == "bad " + QUESTION
    assert dict(clean.attributes) == {"fi.span.kind": "RETRIEVER"}
    assert dict(clean.events[0].attributes) == {"exception.message": DETAIL_REMOVED}
    # The exception event names no type, so the status cannot either.
    assert clean.status.description == DETAIL_REMOVED
    assert clean.context == raw.context and clean.parent == raw.parent


def test_filter_that_raises_still_exports_spans_without_content(
    cognee_filter: Any, recorded: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("filter bug")

    monkeypatch.setattr(cognee_filter.CogneeExportFilter, "_filtered", broken)
    wire = filtered(cognee_filter, recorded)
    assert [span["name"] for span in wire] == [span["name"] for span in recorded]
    assert_no_content(wire)
    for span in wire:
        assert "events" not in span and "message" not in span.get("status", {})


def test_filter_drops_a_span_it_cannot_strip(
    cognee_filter: Any, recorded: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("filter bug")

    monkeypatch.setattr(cognee_filter.CogneeExportFilter, "_filtered", broken)
    monkeypatch.setattr(cognee_filter.CogneeExportFilter, "_stripped", broken)
    assert filtered(cognee_filter, recorded) == []


# --------------------------------------------------------------------------
# README and requirements
# --------------------------------------------------------------------------


def test_readme_states_what_the_tests_check() -> None:
    readme = README.read_text(encoding="utf-8")
    app = APP.read_text(encoding="utf-8")
    for line in (
        "exporter = CogneeExportFilter(HTTPSpanExporter())",
        "provider.add_span_processor(BatchSpanProcessor(exporter))",
    ):
        assert line in app and line in readme, line
    for fact in (
        "cognee==1.6.2",
        "COGNEE_TRACING_ENABLED",
        "/tracer/v1/traces",
        "X-Api-Key",
        "X-Secret-Key",
        "project_type=observe",
        "https://api.futureagi.com:443/tracer/v1/traces",
        "COGNEE_FI_CAPTURE_CONTENT=true",
        "capture_content=True",
        "fi.span.kind",
        DETAIL_REMOVED,
        TYPE_ONLY.strip(" ()"),
        *FILTERED_KEYS,
        *SPAN_KINDS,
    ):
        assert fact in readme, fact


@pytest.mark.parametrize(
    "fact",
    [
        pytest.param("TH-8394", id="R2-register-follow-up"),
        pytest.param("pass the same ones", id="R3-same-exporter-options"),
        pytest.param('pip install "cognee[tracing]==1.6.2"', id="R5-option-b-install"),
        pytest.param("cognee.disable_tracing()", id="R7-disable-tracing"),
        pytest.param("License-Expression: Apache-2.0", id="D9-license-metadata"),
        pytest.param("less useful for debugging", id="N1-error-detail-trade-off"),
        pytest.param("among span attributes", id="F1-capture-claim-scope"),
        pytest.param("`cognee.api.recall`", id="F2-recall-unmapped"),
        pytest.param("`cognee.agent_memory.retrieve`", id="F2-agent-memory-unmapped"),
    ],
)
def test_readme_covers_review_items(fact: str) -> None:
    assert fact in README.read_text(encoding="utf-8")


def test_requirements_pin_the_register_behaviour_the_recipe_relies_on() -> None:
    lines = REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    assert "fi-instrumentation-otel>=1.1.0,<1.2" in lines
    assert "cognee==1.6.2" in lines
