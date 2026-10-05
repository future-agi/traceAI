"""Contract test for the EverOS tracing recipe.

EverOS (EverMind-AI/EverOS, PyPI ``everos``) exports its own OpenTelemetry
spans; the recipe only configures that export. Two kinds of test:

- Fixture tests (no EverOS needed). ``fixtures/everos-1.4.1-capture-off.json``
  and ``-capture-on.json`` are the spans EverOS 1.4.1 exported in the live
  scenario below, as the harness ``Receiver`` decoded them, with content
  capture off and on; ``everos-1.4.1-resource.json`` is the resource of the
  capture-off run. The tests post each with the harness ``post_otlp()``,
  check it with ``compare()``, and assert the content, cost, kind and tree
  facts that README.md states.
- Live tests (skipped unless ``everos`` is importable). ``everos_session.py``
  drives EverOS's own app in-process (add, flush, hybrid search) and
  ``everos_config_probe.py`` exports one span through EverOS's settings and
  tracer, each in a subprocess whose network is limited to 127.0.0.1
  (``loopback_guard/sitecustomize.py``). EverOS's LLM and embedding
  endpoints are a loopback fake of the OpenAI API (``_fake_openai.py``);
  spans go to the shared harness ``Receiver``, which serves ``/v1/traces``
  and ``/tracer/v1/traces`` like fi-collector but does not authenticate,
  stamp projects or store anything. The live runs must match the fixtures'
  shape. Re-record the fixtures with ``EVEROS_RECORD_FIXTURE=1``.

All keys are placeholders. Nothing here contacts an LLM or embedding
provider, Langfuse or Future AGI, and no EverOS server is started.
"""

from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import pytest

from harness import Receiver, compare, post_otlp, run

TESTS_DIR = Path(__file__).resolve().parent
RECIPE_DIR = TESTS_DIR.parent
REPO_ROOT = RECIPE_DIR.parents[2]
README = RECIPE_DIR / "README.md"
REQUIREMENTS = RECIPE_DIR / "requirements.txt"
SESSION = TESTS_DIR / "everos_session.py"
PROBE = TESTS_DIR / "everos_config_probe.py"
GUARD_DIR = TESTS_DIR / "loopback_guard"
GUARD_PROBE = TESTS_DIR / "_guard_probe.py"
FIXTURES_DIR = TESTS_DIR / "fixtures"
FIXTURES = {
    "off": FIXTURES_DIR / "everos-1.4.1-capture-off.json",
    "on": FIXTURES_DIR / "everos-1.4.1-capture-on.json",
}
RESOURCE_FIXTURE = FIXTURES_DIR / "everos-1.4.1-resource.json"

sys.path.insert(0, str(TESTS_DIR))
from _fake_openai import (  # noqa: E402
    EMBEDDING_USAGE,
    EPISODE_MARKER,
    EPISODE_TEXT,
    LLM_ERROR_MARKER,
    USAGE,
    FakeOpenAI,
)
from _scenario import MESSAGE_MARKER, SESSION_ID, USER_ID  # noqa: E402

EVEROS_VERSION = "1.4.1"
PROJECT = "everos-recipe-contract"
FI_API_KEY = "fi-api-placeholder-0000"
# A comma and an equals sign: the JSON headers value must carry them intact.
FI_SECRET_KEY = "fi-secret-placeholder,part=0000"
LLM_API_KEY = "llm-placeholder-key-0000"
LLM_MODEL = "openai/gpt-4o-mini"
EMBEDDING_MODEL = "text-embedding-3-small"
QUERY_MARKER = "QMARK-everos-5d1e"
QUESTION = QUERY_MARKER + " where does Ada work?"
SECRETS = (FI_API_KEY, FI_SECRET_KEY, LLM_API_KEY)
# Every content marker: the question, the user's message, the extracted episode.
CONTENT_MARKERS = (QUERY_MARKER, MESSAGE_MARKER, EPISODE_MARKER)
CONTENT_KEYS = ("langfuse.observation.input", "langfuse.observation.output")

# The span names the recorded scenario produces, with the
# langfuse.observation.type EverOS 1.4.1 stamps on each (README.md table).
OBSERVATION_TYPES = {
    "everos.memory.add": "span",
    "everos.memory.flush": "span",
    "everos.memcell.boundary": "generation",
    "everos.extract": "generation",
    "everos.persist.markdown": "span",
    "everos.ome.extract_agent_case": "agent",
    "everos.ome.extract_atomic_facts": "agent",
    "everos.ome.trigger_profile_clustering": "agent",
    "everos.ome.extract_user_profile": "agent",
    "everos.embedding": "embedding",
    "everos.memory.search": "retriever",
    "everos.search.recall": "retriever",
    "everos.search.rank": "span",
}
# (child, parent) span-name pairs of the recorded scenario.
EDGES = {
    ("everos.memcell.boundary", "everos.memory.add"),
    ("everos.memcell.boundary", "everos.memory.flush"),
    ("everos.extract", "everos.memory.flush"),
    ("everos.persist.markdown", "everos.memory.flush"),
    ("everos.ome.extract_agent_case", "everos.memory.flush"),
    ("everos.ome.extract_atomic_facts", "everos.memory.flush"),
    ("everos.ome.trigger_profile_clustering", "everos.memory.flush"),
    ("everos.ome.extract_user_profile", "everos.ome.trigger_profile_clustering"),
    ("everos.embedding", "everos.ome.trigger_profile_clustering"),
    ("everos.search.recall", "everos.memory.search"),
    ("everos.search.rank", "everos.memory.search"),
    ("everos.embedding", "everos.search.recall"),
}
ROOTS = ("everos.memory.add", "everos.memory.flush", "everos.memory.search")
# The architecture's span names (E2.20); 1.4.1 renamed none of them.
ARCHITECTURE_NAMES = (
    "everos.memory.search",
    "everos.search.recall",
    "everos.search.rank",
    "everos.extract",
    "everos.memcell.boundary",
    "everos.embedding",
    "everos.memory.add",
)
# fi-collector reads a span kind from these keys, then falls back to
# gen_ai.operation.name (exporter/clickhouse25exporter/converter.go:79-131).
SPAN_KIND_KEYS = ("fi.span.kind", "gen_ai.span.kind", "llm.request.type", "openinference.span.kind")
OPERATION_KEYS = ("gen_ai.operation.name",)
# fi-collector's user-cost keys (pkg/adapter/adapter.go:311-313).
COST_KEYS = (
    "gen_ai.cost.total",
    "llm.cost.total",
    "gen_ai.cost.input",
    "llm.cost.prompt",
    "gen_ai.cost.output",
    "llm.cost.completion",
)
# Spans that carry gen_ai.* model and token keys, and the model on each.
GENERATION_MODELS = {
    "everos.memcell.boundary": LLM_MODEL,
    "everos.extract": LLM_MODEL,
    "everos.ome.extract_atomic_facts": LLM_MODEL,
    "everos.embedding": EMBEDDING_MODEL,
}
# With capture on, exactly these (span, key) pairs carry content.
CAPTURED = {
    ("everos.memory.search", "langfuse.observation.input"),
    ("everos.memory.search", "langfuse.observation.output"),
    ("everos.extract", "langfuse.observation.output"),
    ("everos.persist.markdown", "langfuse.observation.output"),
}
# Spans the background strategy scheduler opens; how many finish before
# shutdown depends on timing, so the live comparison tolerates missing ones.
BACKGROUND_PREFIX = "everos.ome."
RUN_TIMEOUT_SECONDS = 600
PROBE_TIMEOUT_SECONDS = 180

EVEROS_INSTALLED = importlib.util.find_spec("everos") is not None


# --------------------------------------------------------------------------
# Helpers shared by the live runs and the recorded fixtures
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


def attributes(span: dict[str, Any]) -> dict[str, Any]:
    return {item["key"]: _value(item.get("value", {})) for item in span.get("attributes", [])}


def spans_named(spans: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [span for span in spans if span["name"] == name]


def edges(spans: list[dict[str, Any]]) -> set[tuple[str, str]]:
    names = {span["spanId"]: span["name"] for span in spans}
    return {
        (span["name"], names.get(span["parentSpanId"], "<missing parent>"))
        for span in spans
        if span.get("parentSpanId")
    }


def shape(spans: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Per (span name, parent name): count, attribute keys, kinds and status codes."""
    names = {span["spanId"]: span["name"] for span in spans}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for span in spans:
        parent_id = span.get("parentSpanId")
        parent = names.get(parent_id, "<missing parent>") if parent_id else ""
        entry = out.setdefault(
            (span["name"], parent), {"count": 0, "keys": set(), "kinds": set(), "status": set()}
        )
        entry["count"] += 1
        entry["keys"] |= set(attributes(span))
        entry["kinds"].add(span.get("kind"))
        entry["status"].add(span.get("status", {}).get("code", "STATUS_CODE_UNSET"))
    return out


def is_background(key: tuple[str, str]) -> bool:
    return key[0].startswith(BACKGROUND_PREFIX) or key[1].startswith(BACKGROUND_PREFIX)


def assert_live_matches_fixture(live: list[dict[str, Any]], recorded: list[dict[str, Any]]) -> None:
    """Request-path spans must match exactly; background ones where they finished."""
    live_shape, recorded_shape = shape(live), shape(recorded)
    foreground = {k: v for k, v in recorded_shape.items() if not is_background(k)}
    assert {k: v for k, v in live_shape.items() if not is_background(k)} == foreground
    background = {k for k in live_shape if is_background(k)}
    assert background <= set(recorded_shape), background - set(recorded_shape)
    for key in background:
        assert live_shape[key]["keys"] == recorded_shape[key]["keys"], key


def found_in(payload: Any, needles: Any) -> list[str]:
    """The needles that occur in payload's JSON."""
    text = json.dumps(payload)
    return [needle for needle in needles if needle in text]


def host_paths() -> tuple[str, ...]:
    """Machine-local path fragments that must not reach a recorded fixture."""
    return (str(Path.home()), "/Users/", "/home/", "/tmp/", "/var/folders", "/private/")


def scrub(spans: list[dict[str, Any]], tmp_base: Path) -> list[dict[str, Any]]:
    """Replace machine-local paths before recording (EverOS 1.4.1 exported none)."""
    text = json.dumps(spans)
    for path, label in (
        (os.path.realpath(tmp_base), "<tmp>"),
        (str(tmp_base), "<tmp>"),
        (str(REPO_ROOT), "<repo>"),
        (str(Path.home()), "<home>"),
    ):
        text = text.replace(path, label)
    return json.loads(text)


def otlp_body(spans: list[dict[str, Any]], resource: dict[str, str]) -> dict[str, Any]:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": key, "value": {"stringValue": value}}
                        for key, value in resource.items()
                    ]
                },
                # EverOS opens every span on its "everos" tracer (attributes.py).
                "scopeSpans": [{"scope": {"name": "everos"}, "spans": spans}],
            }
        ]
    }


def assert_no_secrets(payload: Any) -> None:
    assert found_in(payload, SECRETS) == []


def assert_names_tree_and_types(spans: list[dict[str, Any]]) -> None:
    assert spans, "no spans"
    assert {span["name"] for span in spans} == set(OBSERVATION_TYPES)
    assert edges(spans) == EDGES
    roots = [span for span in spans if not span.get("parentSpanId")]
    assert sorted(root["name"] for root in roots) == sorted(ROOTS)
    # One trace per API call: add, flush (with its background work), search.
    assert len({span["traceId"] for span in spans}) == len(ROOTS)
    for span in spans:
        assert span["kind"] == "SPAN_KIND_INTERNAL", span["name"]
        assert attributes(span)["langfuse.observation.type"] == OBSERVATION_TYPES[span["name"]]
        assert attributes(span)["langfuse.trace.tags"] == ["everos", "memory"]


def assert_collector_mapping_inputs(spans: list[dict[str, Any]]) -> None:
    """The keys fi-collector derives kind, model, tokens and cost from (README.md)."""
    assert spans, "no spans"
    for span in spans:
        attrs = attributes(span)
        # No kind key and no operation name: fi-collector stores "unknown".
        assert not set(SPAN_KIND_KEYS + OPERATION_KEYS) & set(attrs), span["name"]
        # No user cost: the recipe adds none and converts no tokens to a price.
        assert not set(COST_KEYS) & set(attrs), span["name"]
        assert not [k for k in attrs if "cost" in k], span["name"]
        gen_ai = {k: v for k, v in attrs.items() if k.startswith("gen_ai.")}
        if span["name"] not in GENERATION_MODELS:
            assert gen_ai == {}, span["name"]
        elif span["name"] == "everos.embedding":
            assert gen_ai == {
                "gen_ai.request.model": EMBEDDING_MODEL,
                "gen_ai.usage.input_tokens": EMBEDDING_USAGE["prompt_tokens"],
            }
        else:
            # One LLM call per span with the fake, so no accumulation.
            assert gen_ai == {
                "gen_ai.request.model": GENERATION_MODELS[span["name"]],
                "gen_ai.usage.input_tokens": USAGE["prompt_tokens"],
                "gen_ai.usage.output_tokens": USAGE["completion_tokens"],
            }, span["name"]


def assert_no_content(spans: list[dict[str, Any]]) -> None:
    assert spans, "no spans"
    assert found_in(spans, CONTENT_MARKERS) == []
    for span in spans:
        assert not set(CONTENT_KEYS) & set(attributes(span)), span["name"]


def assert_captured_content(spans: list[dict[str, Any]]) -> None:
    captured = {
        (span["name"], key) for span in spans for key in attributes(span) if key in CONTENT_KEYS
    }
    assert captured == CAPTURED
    (search,) = spans_named(spans, "everos.memory.search")
    search_input = json.loads(attributes(search)["langfuse.observation.input"])
    assert search_input == {"query": QUESTION, "top_k": -1, "method": "hybrid"}
    search_output = json.loads(attributes(search)["langfuse.observation.output"])
    assert len(search_output["episodes"]) == 1
    (extract,) = spans_named(spans, "everos.extract")
    assert attributes(extract)["langfuse.observation.output"] == EPISODE_TEXT
    (persist,) = spans_named(spans, "everos.persist.markdown")
    # A memory-root-relative path, never the host path.
    path = attributes(persist)["langfuse.observation.output"]
    assert path.startswith("default_app/default_project/users/{0}/episodes/".format(USER_ID))
    # The question only on the search span; the user's own messages nowhere.
    for span in spans:
        if span["name"] != "everos.memory.search":
            assert found_in(span, [QUERY_MARKER]) == [], span["name"]
    assert found_in(spans, [MESSAGE_MARKER]) == []


def assert_identifiers(spans: list[dict[str, Any]]) -> None:
    """Identifiers EverOS exports with capture off (README.md, Privacy)."""
    for name in ("everos.memory.add", "everos.memory.flush", "everos.extract", "everos.persist.markdown"):
        for span in spans_named(spans, name):
            assert attributes(span)["langfuse.session.id"] == SESSION_ID, name
    (search,) = spans_named(spans, "everos.memory.search")
    assert attributes(search)["langfuse.user.id"] == USER_ID
    (persist,) = spans_named(spans, "everos.persist.markdown")
    assert attributes(persist)["langfuse.trace.metadata.owner_id"] == USER_ID


# --------------------------------------------------------------------------
# Recorded fixtures (no EverOS needed)
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def recorded() -> dict[str, list[dict[str, Any]]]:
    return {case: json.loads(path.read_text(encoding="utf-8")) for case, path in FIXTURES.items()}


@pytest.fixture(scope="module")
def recorded_resource() -> dict[str, str]:
    return json.loads(RESOURCE_FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", sorted(FIXTURES))
def test_recorded_fixture_round_trips_through_the_harness(
    case: str, recorded: dict[str, list[dict[str, Any]]], recorded_resource: dict[str, str]
) -> None:
    spans = recorded[case]
    assert spans, "empty fixture"
    with Receiver() as receiver:
        assert post_otlp(otlp_body(spans, recorded_resource), receiver.collector_endpoint) == 200
        received = receiver.spans()
        compare(received, FIXTURES[case])
        (request,) = receiver.requests()
    assert len(received) == len(spans)
    assert request["path"] == "/tracer/v1/traces"
    assert request["resource_attributes"] == [recorded_resource]


def test_recorded_resource_is_an_observe_project(recorded_resource: dict[str, str]) -> None:
    # From OTEL_RESOURCE_ATTRIBUTES; fi-collector rejects a batch without
    # project_name (pkg/auth/stamp.go:31-45).
    assert recorded_resource["project_name"] == PROJECT
    assert recorded_resource["project_type"] == "observe"
    # Set by EverOS itself (provider.py: Resource.create).
    assert recorded_resource["service.name"] == "everos"
    assert recorded_resource["service.version"] == EVEROS_VERSION


@pytest.mark.parametrize("case", sorted(FIXTURES))
def test_recorded_span_names_tree_and_observation_types(
    case: str, recorded: dict[str, list[dict[str, Any]]]
) -> None:
    assert_names_tree_and_types(recorded[case])
    # No architecture span name was renamed in 1.4.1.
    assert set(ARCHITECTURE_NAMES) <= {span["name"] for span in recorded[case]}


@pytest.mark.parametrize("case", sorted(FIXTURES))
def test_recorded_spans_carry_no_kind_or_cost_key(
    case: str, recorded: dict[str, list[dict[str, Any]]]
) -> None:
    assert_collector_mapping_inputs(recorded[case])


def test_capture_off_fixture_has_no_query_anywhere(
    recorded: dict[str, list[dict[str, Any]]], recorded_resource: dict[str, str]
) -> None:
    assert_no_content(recorded["off"])
    assert found_in(recorded_resource, CONTENT_MARKERS) == []
    assert_identifiers(recorded["off"])


def test_capture_on_fixture_has_the_query_on_the_search_span(
    recorded: dict[str, list[dict[str, Any]]],
) -> None:
    assert_captured_content(recorded["on"])
    assert_identifiers(recorded["on"])


def test_capture_on_adds_only_the_content_keys(recorded: dict[str, list[dict[str, Any]]]) -> None:
    def keys_without_content(spans: list[dict[str, Any]]) -> dict[tuple[str, str], Any]:
        return {k: v["keys"] - set(CONTENT_KEYS) for k, v in shape(spans).items()}

    assert keys_without_content(recorded["on"]) == keys_without_content(recorded["off"])


def test_recorded_fixtures_have_no_secrets_or_host_paths(
    recorded: dict[str, list[dict[str, Any]]], recorded_resource: dict[str, str]
) -> None:
    for spans in recorded.values():
        assert_no_secrets(spans)
        assert found_in(spans, host_paths()) == []
        for span in spans:
            assert span.get("status", {}).get("code", "STATUS_CODE_UNSET") != "STATUS_CODE_ERROR"
            assert not span.get("events"), span["name"]
    assert_no_secrets(recorded_resource)


def test_scrub_removes_host_and_tmp_paths(tmp_path_factory: pytest.TempPathFactory) -> None:
    base = tmp_path_factory.getbasetemp()
    planted = [
        str(base / "recipe0" / "everos-root" / "x.md"),
        os.path.realpath(base) + "/recipe0/home",
        str(REPO_ROOT / "python" / "x.py"),
        str(Path.home() / ".everos" / "everos.toml"),
    ]
    assert found_in(scrub([{"planted": planted}], base), host_paths()) == []


def test_guard_refuses_and_logs_non_loopback_connections(tmp_path: Path) -> None:
    """Positive control for every "nothing was refused" assertion in this file."""
    guard_log = tmp_path / "guard.jsonl"
    result = run(
        [sys.executable, str(GUARD_PROBE)],
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "PYTHONPATH": str(GUARD_DIR),
            "LOOPBACK_GUARD_LOG": str(guard_log),
        },
        stdin=None,
        timeout=60,
    )
    assert not result.timed_out
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert "refused: connect,connect_ex,getaddrinfo" in result.stdout.decode("utf-8", "replace")
    logged = [json.loads(line) for line in guard_log.read_text().splitlines()]
    assert logged[0]["kind"] == "installed"
    refused = logged[1:]
    assert [entry["kind"] for entry in refused] == ["connect", "connect_ex", "getaddrinfo"]
    assert "192.0.2.1" in refused[0]["target"]
    assert "2001:db8::1" in refused[1]["target"]
    assert "example.invalid" in refused[2]["target"]


# --------------------------------------------------------------------------
# Live runs (EverOS 1.4.1 installed)
# --------------------------------------------------------------------------


def recipe_env(
    work: Path,
    guard_log: Path,
    receiver: Receiver,
    fake: FakeOpenAI,
    **overrides: Optional[str],
) -> dict[str, str]:
    """README.md's environment, with the Receiver as the endpoint and the fake as the model.

    Built from scratch: nothing is inherited but PATH.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(work / "home"),
        "PYTHONPATH": str(GUARD_DIR),
        "LOOPBACK_GUARD_LOG": str(guard_log),
        "EVEROS_ROOT": str(work / "everos-root"),
        # The recipe (README.md, "Set up").
        "EVEROS_OBSERVABILITY__ENABLED": "true",
        "EVEROS_OBSERVABILITY__ENDPOINT": receiver.collector_endpoint,
        "EVEROS_OBSERVABILITY__HEADERS": json.dumps(
            {"X-Api-Key": FI_API_KEY, "X-Secret-Key": FI_SECRET_KEY}
        ),
        "OTEL_RESOURCE_ATTRIBUTES": "project_name={0},project_type=observe".format(PROJECT),
        # EverOS's own model settings, pointed at the loopback fake.
        "EVEROS_LLM__MODEL": LLM_MODEL,
        "EVEROS_LLM__API_KEY": LLM_API_KEY,
        "EVEROS_LLM__BASE_URL": fake.base_url,
        "EVEROS_EMBEDDING__MODEL": EMBEDDING_MODEL,
        "EVEROS_EMBEDDING__API_KEY": LLM_API_KEY,
        "EVEROS_EMBEDDING__BASE_URL": fake.base_url,
        # No rerank or multimodal key is set, so neither is built; the base
        # URLs point at the fake anyway.
        "EVEROS_RERANK__BASE_URL": fake.base_url,
        "EVEROS_MULTIMODAL__BASE_URL": fake.base_url,
    }
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


class Scenario:
    """One guarded subprocess with its own Receiver, fake model and EverOS root."""

    def __init__(
        self,
        name: str,
        script: Path,
        work: Path,
        malformed_atomic_facts: bool = False,
        everos_toml: Optional[str] = None,
        **overrides: Optional[str],
    ) -> None:
        self.name = name
        self.script = script
        self.work = work
        self.malformed_atomic_facts = malformed_atomic_facts
        # Written to $EVEROS_ROOT/everos.toml; "{origin}" becomes the Receiver's.
        self.everos_toml = everos_toml
        # Environment changes; None removes a variable, "{origin}" as above.
        self.overrides = overrides
        self.guard_log = work / "guard.jsonl"
        self.result: Any = None
        self.spans: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.fake_requests: list[dict[str, Any]] = []

    def execute(self) -> "Scenario":
        self.work.mkdir(parents=True, exist_ok=True)
        with Receiver() as receiver, FakeOpenAI(self.malformed_atomic_facts) as fake:
            overrides = {
                key: (value.replace("{origin}", receiver.origin) if value else value)
                for key, value in self.overrides.items()
            }
            if self.everos_toml is not None:
                root = self.work / "everos-root"
                root.mkdir(parents=True, exist_ok=True)
                (root / "everos.toml").write_text(
                    self.everos_toml.replace("{origin}", receiver.origin), encoding="utf-8"
                )
            self.result = run(
                [sys.executable, str(self.script), QUESTION],
                env=recipe_env(self.work, self.guard_log, receiver, fake, **overrides),
                stdin=None,
                timeout=RUN_TIMEOUT_SECONDS
                if self.script == SESSION
                else PROBE_TIMEOUT_SECONDS,
            )
            self.spans = receiver.spans()
            self.requests = receiver.requests()
            self.fake_requests = fake.requests()
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
        return [json.loads(line) for line in self.guard_log.read_text(encoding="utf-8").splitlines()]

    def blocked(self) -> list[dict[str, Any]]:
        return [record for record in self.guard_records() if record["kind"] != "installed"]

    def assert_ran_offline(self) -> None:
        assert not self.result.timed_out, self.stderr[-4000:]
        assert self.result.returncode == 0, self.stderr[-4000:]
        # The guard loaded (in the scenario and in `everos init`) and refused nothing.
        assert self.guard_records(), "guard not installed"
        assert self.blocked() == []

    def json_line(self) -> dict[str, Any]:
        lines = [line for line in self.stdout.splitlines() if line.startswith("{")]
        return json.loads(lines[-1])


def require_everos() -> None:
    if not EVEROS_INSTALLED:
        pytest.skip("everos[otel]=={0} is not installed".format(EVEROS_VERSION))


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Scenario]:
    require_everos()
    scenarios = [
        Scenario("recipe", SESSION, tmp_path_factory.mktemp("recipe")),
        Scenario(
            "capture",
            SESSION,
            tmp_path_factory.mktemp("capture"),
            EVEROS_OBSERVABILITY__CAPTURE_CONTENT="true",
        ),
        Scenario("llm_error", SESSION, tmp_path_factory.mktemp("llmerror"), malformed_atomic_facts=True),
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(scenarios)) as pool:
        done = {scenario.name: scenario for scenario in pool.map(Scenario.execute, scenarios)}
    if os.environ.get("EVEROS_RECORD_FIXTURE") == "1":
        base = tmp_path_factory.getbasetemp()
        for case, name in (("off", "recipe"), ("on", "capture")):
            done[name].assert_ran_offline()
            FIXTURES[case].write_text(
                json.dumps(scrub(done[name].spans, base), indent=1) + "\n", encoding="utf-8"
            )
        (resource,) = {
            json.dumps(r, sort_keys=True)
            for request in done["recipe"].requests
            for r in request["resource_attributes"]
        }
        RESOURCE_FIXTURE.write_text(
            json.dumps(json.loads(resource), indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
    return done


def test_recipe_runs_offline_against_the_fakes(runs: dict[str, Scenario]) -> None:
    recipe = runs["recipe"]
    recipe.assert_ran_offline()
    outcome = recipe.json_line()
    assert outcome["add"] == [200, {"message_count": 3, "status": "accumulated"}]
    assert outcome["flush"] == [200, {"status": "extracted"}]
    assert outcome["indexed_episodes"] == 1
    assert outcome["search"][0] == 200 and len(outcome["search"][1]) == 1
    paths = {request["path"] for request in recipe.fake_requests}
    assert paths == {"/v1/chat/completions", "/v1/embeddings"}
    assert {r["authorization"] for r in recipe.fake_requests} == {"Bearer " + LLM_API_KEY}


def test_recipe_exports_to_the_configured_path_with_both_auth_headers(
    runs: dict[str, Scenario],
) -> None:
    requests = runs["recipe"].requests
    assert requests, "no export reached the receiver"
    for request in requests:
        # EVEROS_OBSERVABILITY__ENDPOINT is used as given: no /v1/traces appended.
        assert request["path"] == "/tracer/v1/traces"
        assert request["headers"]["x-api-key"] == FI_API_KEY
        assert request["headers"]["x-secret-key"] == FI_SECRET_KEY
        assert request["headers"]["content-type"] == "application/x-protobuf"
        # No Langfuse Basic auth: the langfuse_* settings are unset.
        assert "authorization" not in request["headers"]
        assert LLM_API_KEY not in json.dumps(request["headers"])


def test_recipe_resource_is_an_observe_project(
    runs: dict[str, Scenario], recorded_resource: dict[str, str]
) -> None:
    resources = [r for request in runs["recipe"].requests for r in request["resource_attributes"]]
    assert resources, "no export reached the receiver"
    for resource in resources:
        assert resource["project_name"] == PROJECT
        assert resource["project_type"] == "observe"
        assert resource["service.name"] == "everos"
        assert resource["service.version"] == EVEROS_VERSION
        # Same keys as the recorded resource (service.instance.id differs per run).
        assert set(resource) == set(recorded_resource)


def test_recipe_run_matches_the_capture_off_fixture(
    runs: dict[str, Scenario], recorded: dict[str, list[dict[str, Any]]]
) -> None:
    spans = runs["recipe"].spans
    assert spans, "no spans"
    for name in ROOTS + ARCHITECTURE_NAMES:
        assert spans_named(spans, name), name
    assert_live_matches_fixture(spans, recorded["off"])
    assert_collector_mapping_inputs(spans)


def test_recipe_exports_no_query_or_content(runs: dict[str, Scenario]) -> None:
    recipe = runs["recipe"]
    assert_no_content(recipe.spans)
    assert found_in([r["resource_attributes"] for r in recipe.requests], CONTENT_MARKERS) == []
    assert_identifiers(recipe.spans)


def test_recipe_exports_no_secrets(runs: dict[str, Scenario]) -> None:
    recipe = runs["recipe"]
    assert recipe.spans and recipe.requests, "no export reached the receiver"
    assert_no_secrets(recipe.spans)
    assert_no_secrets([request["resource_attributes"] for request in recipe.requests])
    assert found_in(recipe.stdout + recipe.stderr, SECRETS) == []


def test_capture_run_matches_the_capture_on_fixture(
    runs: dict[str, Scenario], recorded: dict[str, list[dict[str, Any]]]
) -> None:
    capture = runs["capture"]
    capture.assert_ran_offline()
    assert_live_matches_fixture(capture.spans, recorded["on"])
    assert_captured_content(capture.spans)
    assert_no_secrets(capture.spans)


def test_an_extraction_error_exports_the_model_reply_with_capture_off(
    runs: dict[str, Scenario],
) -> None:
    """What the content switch does not cover: error text (README.md, Privacy)."""
    error_run = runs["llm_error"]
    error_run.assert_ran_offline()
    failed = [
        span
        for span in spans_named(error_run.spans, "everos.ome.extract_atomic_facts")
        if span.get("status", {}).get("code") == "STATUS_CODE_ERROR"
    ]
    assert failed, "the atomic-fact strategy did not fail"
    for span in failed:
        assert LLM_ERROR_MARKER in span["status"]["message"]
        (event,) = span["events"]
        assert event["name"] == "exception"
        assert LLM_ERROR_MARKER in attributes(event)["exception.message"]
    # Content capture stayed off: no content key on any span.
    for span in error_run.spans:
        assert not set(CONTENT_KEYS) & set(attributes(span)), span["name"]


# --------------------------------------------------------------------------
# Each way of setting the export, through EverOS's own settings and tracer
# --------------------------------------------------------------------------

# The recipe's EVEROS_OBSERVABILITY__* variables, removed so a probe can set
# the export another way.
NO_EVEROS_EXPORT_ENV = {
    "EVEROS_OBSERVABILITY__ENABLED": None,
    "EVEROS_OBSERVABILITY__ENDPOINT": None,
    "EVEROS_OBSERVABILITY__HEADERS": None,
}


def readme_toml() -> str:
    (block,) = re.findall(r"```toml\n(.*?)```", README.read_text(encoding="utf-8"), flags=re.DOTALL)
    return block


def probe(tmp_path: Path, everos_toml: Optional[str] = None, **overrides: Optional[str]) -> Scenario:
    require_everos()
    scenario = Scenario("probe", PROBE, tmp_path, everos_toml=everos_toml, **overrides).execute()
    scenario.assert_ran_offline()
    return scenario


def single_span(scenario: Scenario) -> dict[str, Any]:
    (span,) = scenario.spans
    assert span["name"] == "everos.memory.search"
    return span


def test_readme_toml_block_exports_with_both_headers(tmp_path: Path) -> None:
    # README.md's toml block as written, with the Receiver's origin for the
    # endpoint and placeholder keys, and no EVEROS_OBSERVABILITY__* variable.
    text = readme_toml()
    assert "https://api.futureagi.com/tracer/v1/traces" in text
    text = (
        text.replace("https://api.futureagi.com", "{origin}")
        .replace("YOUR_FI_API_KEY", FI_API_KEY)
        .replace("YOUR_FI_SECRET_KEY", FI_SECRET_KEY)
    )
    scenario = probe(tmp_path, everos_toml=text, **NO_EVEROS_EXPORT_ENV)
    assert scenario.json_line() == {"installed": True, "capture_content": False}
    assert scenario.requests, "no export reached the receiver"
    for request in scenario.requests:
        assert request["path"] == "/tracer/v1/traces"
        assert request["headers"]["x-api-key"] == FI_API_KEY
        assert request["headers"]["x-secret-key"] == FI_SECRET_KEY
        assert request["resource_attributes"][0]["project_name"] == PROJECT
    assert "langfuse.observation.input" not in attributes(single_span(scenario))


def test_tracing_is_off_by_default(tmp_path: Path) -> None:
    scenario = probe(tmp_path, EVEROS_OBSERVABILITY__ENABLED=None)
    # The probe ran to the end and EverOS installed no tracer.
    assert scenario.json_line() == {"installed": False, "capture_content": False}
    assert scenario.requests == []
    assert scenario.spans == []


def test_capture_content_switch_puts_the_query_on_the_span(tmp_path: Path) -> None:
    scenario = probe(tmp_path, EVEROS_OBSERVABILITY__CAPTURE_CONTENT="true")
    assert scenario.json_line() == {"installed": True, "capture_content": True}
    captured = json.loads(attributes(single_span(scenario))["langfuse.observation.input"])
    assert captured["query"] == QUESTION


def test_otel_headers_variable_is_used_when_everos_sets_no_headers(tmp_path: Path) -> None:
    # OpenTelemetry's own variable: comma-separated name=value pairs, each
    # value percent-decoded, so the secret's comma must be encoded.
    scenario = probe(
        tmp_path,
        EVEROS_OBSERVABILITY__HEADERS=None,
        OTEL_EXPORTER_OTLP_TRACES_HEADERS="X-Api-Key={0},X-Secret-Key={1}".format(
            quote(FI_API_KEY, safe=""), quote(FI_SECRET_KEY, safe="")
        ),
    )
    assert scenario.requests, "no export reached the receiver"
    for request in scenario.requests:
        assert request["headers"]["x-api-key"] == FI_API_KEY
        assert request["headers"]["x-secret-key"] == FI_SECRET_KEY


def test_everos_headers_win_over_otel_headers_per_name(tmp_path: Path) -> None:
    # opentelemetry-exporter-otlp-proto-http 1.45.0 merges the two: the
    # variable's headers first, then EverOS's, name by name.
    scenario = probe(
        tmp_path,
        OTEL_EXPORTER_OTLP_TRACES_HEADERS="x-api-key=from-otel-variable,x-extra=kept",
    )
    assert scenario.requests, "no export reached the receiver"
    for request in scenario.requests:
        assert request["headers"]["x-api-key"] == FI_API_KEY
        assert request["headers"]["x-secret-key"] == FI_SECRET_KEY
        assert request["headers"]["x-extra"] == "kept"


def test_otel_endpoint_variable_gets_v1_traces_appended(tmp_path: Path) -> None:
    scenario = probe(
        tmp_path,
        EVEROS_OBSERVABILITY__ENDPOINT=None,
        OTEL_EXPORTER_OTLP_ENDPOINT="{origin}",
    )
    assert scenario.requests, "no export reached the receiver"
    assert {request["path"] for request in scenario.requests} == {"/v1/traces"}


def test_everos_endpoint_without_the_path_is_not_completed(tmp_path: Path) -> None:
    # EverOS passes `endpoint` to the exporter as given, so an origin alone
    # posts to "/", which fi-collector (and the Receiver) answer with 404.
    scenario = probe(tmp_path, EVEROS_OBSERVABILITY__ENDPOINT="{origin}")
    assert scenario.json_line() == {"installed": True, "capture_content": False}
    assert scenario.requests == []
    assert scenario.spans == []
    assert "404" in scenario.stdout + scenario.stderr


def test_without_otel_resource_attributes_there_is_no_project(tmp_path: Path) -> None:
    # fi-collector rejects such a batch (pkg/auth/stamp.go:31-45); EverOS has
    # no setting of its own for resource attributes.
    scenario = probe(tmp_path, OTEL_RESOURCE_ATTRIBUTES=None)
    assert scenario.requests, "no export reached the receiver"
    for request in scenario.requests:
        for resource in request["resource_attributes"]:
            assert "project_name" not in resource
            assert resource["service.name"] == "everos"


# --------------------------------------------------------------------------
# README and requirements
# --------------------------------------------------------------------------


def test_readme_first_paragraph_names_the_project_and_excludes_lookalikes() -> None:
    text = README.read_text(encoding="utf-8")
    first = " ".join(text.split("\n\n")[1].split())
    for fact in ("EverMind-AI/EverOS", "`everos`", "EverMemOS Cloud", "Claude Code plugin"):
        assert fact in first, fact


def test_readme_span_table_matches_the_fixture() -> None:
    rows = re.findall(
        r"^\| `(everos\.[a-z_.]+)` \| [^|]+ \| `([a-z]+)` \|",
        README.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    assert dict(rows) == OBSERVATION_TYPES
    assert len(rows) == len(OBSERVATION_TYPES)


def test_readme_states_what_the_tests_check() -> None:
    readme = README.read_text(encoding="utf-8")
    for fact in (
        "everos[otel]==1.4.1",
        "License: Apache-2.0",
        "EVEROS_OBSERVABILITY__ENABLED",
        "EVEROS_OBSERVABILITY__ENDPOINT",
        "EVEROS_OBSERVABILITY__HEADERS",
        "EVEROS_OBSERVABILITY__CAPTURE_CONTENT",
        "OTEL_RESOURCE_ATTRIBUTES",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "project_type=observe",
        "/tracer/v1/traces",
        "converter.go:79-131",
        "langfuse.observation.input",
        "langfuse.observation.output",
        *SPAN_KIND_KEYS,
        *ARCHITECTURE_NAMES,
    ):
        assert fact in readme, fact


def test_requirements_pin_the_tested_versions() -> None:
    lines = [
        line.split("#")[0].strip()
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    ]
    assert "everos[otel]==1.4.1" in lines
    assert "opentelemetry-sdk==1.45.0" in lines
    assert "opentelemetry-exporter-otlp-proto-http==1.45.0" in lines
