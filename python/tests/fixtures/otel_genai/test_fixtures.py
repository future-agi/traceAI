"""OTel GenAI semantic-convention fixtures: era A keys, era B keys, dual-emit.

Each fixture file is an OTLP/JSON export request. The tests post it with the
shared harness ``post_otlp()`` to a loopback ``Receiver`` on fi-collector's
``/tracer/v1/traces`` path and check the stored attributes against a golden
file with ``compare()``. The Receiver stores what it receives; it does not
authenticate, stamp projects or derive columns.

The model, provider and token columns are fi-collector's
(``DeriveHotKeys``, ``fi-collector/pkg/adapter/adapter.go``, future-agi main
4af5338). ``columns.golden.json`` records the expected columns and the alias
order they come from. ``derive_columns`` below applies that recorded order to
the stored attributes; ``FI_COLLECTOR_ADAPTER_GO`` checks the recorded order
against a real ``adapter.go``. These tests never run the Go collector.

These are fixtures of these keys, not a claim that Future AGI implements a
named spec version. No kind is asserted for ``retrieval`` or
``invoke_agent``. No keys, no live model, loopback only.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent
TESTS_DIR = FIXTURES_DIR.parents[1]
README = FIXTURES_DIR / "README.md"
COLUMNS_GOLDEN = FIXTURES_DIR / "columns.golden.json"

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from harness import Receiver, compare, post_otlp  # noqa: E402

PROJECT = "otel-genai-conformance"
# Span names each fixture must store, in file order.
SPAN_NAMES = {
    "era_a.json": ["chat"],
    "era_b.json": ["invoke_agent", "retrieval", "chat gpt-test"],
    "dual_emit.json": ["dual_emit"],
}
# fi-collector reads a span kind from these keys before it falls back to
# gen_ai.operation.name (exporter/clickhouse25exporter/converter.go:79-90).
SPAN_KIND_KEYS = ("fi.span.kind", "gen_ai.span.kind", "llm.request.type", "openinference.span.kind")
ALIAS_LISTS = ("modelNameKeys", "providerKeys", "inputTokenKeys", "outputTokenKeys", "totalTokenKeys")


def load(name: str) -> Any:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def golden_path(fixture: str) -> Path:
    return FIXTURES_DIR / fixture.replace(".json", ".golden.json")


def post_fixture(fixture: str) -> dict[str, Any]:
    """Post one fixture file as-is; return what the Receiver stored."""
    with Receiver() as receiver:
        status = post_otlp(load(fixture), receiver.collector_endpoint)
        record = {"status": status, "spans": receiver.spans(), "requests": receiver.requests()}
    assert record["status"] == 200
    assert record["spans"], "no span reached the receiver"
    return record


def stored_values(span: dict[str, Any]) -> dict[str, Any]:
    """OTLP/JSON attribute values as Python scalars (int64 arrives as a string)."""
    values: dict[str, Any] = {}
    for attribute in span.get("attributes", []):
        value = attribute["value"]
        if "intValue" in value:
            values[attribute["key"]] = int(value["intValue"])
        else:
            values[attribute["key"]] = next(iter(value.values()))
    return values


def derive_columns(span: dict[str, Any], aliases: dict[str, list[str]]) -> dict[str, Any]:
    """fi-collector's DeriveHotKeys (adapter.go:212-235) for the value types in these fixtures.

    Split (:62-107) puts string values in attrs_string and int/double values in
    attrs_number. firstString (:316-324) returns the first alias with a
    non-empty string; firstNumber (:330-346) the first alias with a number or
    a numeric string. Total falls back to prompt + completion when no total
    alias is set and that sum is positive (:230-235). Nothing is ever added
    across aliases.
    """
    strings: dict[str, str] = {}
    numbers: dict[str, float] = {}
    for attribute in span.get("attributes", []):
        key, value = attribute["key"], attribute["value"]
        if "stringValue" in value:
            strings[key] = value["stringValue"]
        elif "intValue" in value:
            numbers[key] = float(int(value["intValue"]))
        elif "doubleValue" in value:
            numbers[key] = float(value["doubleValue"])

    def first_string(keys: list[str]) -> str:
        return next((strings[key] for key in keys if strings.get(key)), "")

    def first_number(keys: list[str]) -> Optional[float]:
        for key in keys:
            if key in numbers:
                return numbers[key]
            if key in strings:
                try:
                    return float(strings[key].strip())
                except ValueError:
                    continue
        return None

    prompt = int(first_number(aliases["inputTokenKeys"]) or 0)
    completion = int(first_number(aliases["outputTokenKeys"]) or 0)
    total = first_number(aliases["totalTokenKeys"])
    if total is None:
        total = prompt + completion if prompt + completion > 0 else 0
    return {
        "model": first_string(aliases["modelNameKeys"]),
        "provider": first_string(aliases["providerKeys"]),
        "gen_ai_system": strings.get("gen_ai.system", ""),
        "gen_ai_operation": strings.get("gen_ai.operation.name", ""),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": int(total),
    }


@pytest.fixture(scope="module")
def columns_golden() -> dict[str, Any]:
    return json.loads(COLUMNS_GOLDEN.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def aliases(columns_golden: dict[str, Any]) -> dict[str, list[str]]:
    return {name: entry["keys"] for name, entry in columns_golden["aliases"].items()}


# --------------------------------------------------------------------------
# Loopback guard: every test runs with non-loopback connections refused.
# --------------------------------------------------------------------------

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@contextlib.contextmanager
def loopback_only() -> Iterator[list[tuple[str, str]]]:
    """Refuse and record any connect or DNS lookup to a non-loopback host."""
    attempts: list[tuple[str, str]] = []
    original = (socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo)

    def blocked(sock: socket.socket, address: Any) -> bool:
        host = address[0] if isinstance(address, tuple) else address
        return sock.family in (socket.AF_INET, socket.AF_INET6) and str(host) not in _LOOPBACK

    def connect(self: socket.socket, address: Any) -> None:
        if blocked(self, address):
            attempts.append(("connect", repr(address)))
            raise OSError("blocked by loopback guard: {0!r}".format(address))
        return original[0](self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        if blocked(self, address):
            attempts.append(("connect_ex", repr(address)))
            raise OSError("blocked by loopback guard: {0!r}".format(address))
        return original[1](self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None and str(host) not in _LOOPBACK:
            attempts.append(("getaddrinfo", repr(host)))
            raise socket.gaierror("blocked by loopback guard: {0!r}".format(host))
        return original[2](host, *args, **kwargs)

    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = (  # type: ignore[method-assign,assignment]
        connect,
        connect_ex,
        getaddrinfo,
    )
    try:
        yield attempts
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = original  # type: ignore[method-assign,assignment]


@pytest.fixture(autouse=True)
def no_egress() -> Iterator[None]:
    with loopback_only() as attempts:
        yield
    assert attempts == [], attempts


def test_guard_refuses_and_records_non_loopback_connections() -> None:
    """Positive control for the autouse guard. Targets are reserved and unreachable."""
    with loopback_only() as attempts:
        for attempt in (
            lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("192.0.2.1", 80)),
            lambda: socket.socket(socket.AF_INET6, socket.SOCK_STREAM).connect_ex(("2001:db8::1", 80, 0, 0)),
            lambda: socket.getaddrinfo("example.invalid", 443),
        ):
            with pytest.raises(OSError, match="blocked by loopback guard"):
                attempt()
    assert [kind for kind, _target in attempts] == ["connect", "connect_ex", "getaddrinfo"]
    assert "192.0.2.1" in attempts[0][1]
    assert "2001:db8::1" in attempts[1][1]
    assert "example.invalid" in attempts[2][1]


def test_post_otlp_refuses_a_non_loopback_endpoint() -> None:
    """The harness poster is the only sender here; it never leaves 127.0.0.1."""
    for endpoint in ("http://192.0.2.1:4318/tracer/v1/traces", "https://127.0.0.1/tracer/v1/traces"):
        with pytest.raises(ValueError, match="loopback"):
            post_otlp(load("era_b.json"), endpoint)


# --------------------------------------------------------------------------
# Stored attributes: post each file, compare() against its golden.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", sorted(SPAN_NAMES))
def test_fixture_is_stored_and_matches_its_golden(fixture: str) -> None:
    record = post_fixture(fixture)
    assert [span["name"] for span in record["spans"]] == SPAN_NAMES[fixture]
    (request,) = record["requests"]
    assert request["path"] == "/tracer/v1/traces"
    # fi-collector fails a batch whose resource has no project_name
    # (pkg/auth/stamp.go:31-46, answered 400 at pkg/server/server.go:461-464).
    assert request["resource_attributes"] == [{"project_name": PROJECT, "project_type": "observe"}]
    compare(record["spans"], golden_path(fixture))


def test_compare_fails_when_a_stored_attribute_differs() -> None:
    """Control: the goldens are not vacuous. A changed token value fails compare()."""
    spans = post_fixture("era_b.json")["spans"]
    for attribute in spans[2]["attributes"]:
        if attribute["key"] == "gen_ai.usage.input_tokens":
            attribute["value"] = {"intValue": "6"}
    with pytest.raises(AssertionError, match="spans do not match golden"):
        compare(spans, golden_path("era_b.json"))


def test_no_fixture_span_carries_a_span_kind_key() -> None:
    """The fixtures do not force a kind: gen_ai.operation.name is the only kind signal."""
    for fixture in SPAN_NAMES:
        for span in post_fixture(fixture)["spans"]:
            assert not set(SPAN_KIND_KEYS) & set(stored_values(span)), (fixture, span["name"])


def test_retrieval_and_invoke_agent_spans_are_stored_with_only_the_operation_name() -> None:
    """Stored, with no kind asserted. A model key would be a kind signal too
    (the backend's otel_genai.py:142-143 types any span with a model as LLM)."""
    spans = {span["name"]: span for span in post_fixture("era_b.json")["spans"]}
    assert stored_values(spans["retrieval"]) == {"gen_ai.operation.name": "retrieval"}
    assert stored_values(spans["invoke_agent"]) == {"gen_ai.operation.name": "invoke_agent"}


def test_era_b_spans_form_one_agent_tree() -> None:
    spans = {span["name"]: span for span in post_fixture("era_b.json")["spans"]}
    assert len({span["traceId"] for span in spans.values()}) == 1
    agent = spans["invoke_agent"]
    assert not agent.get("parentSpanId")
    assert spans["retrieval"]["parentSpanId"] == agent["spanId"]
    assert spans["chat gpt-test"]["parentSpanId"] == agent["spanId"]


# --------------------------------------------------------------------------
# Collector columns: adapter.go alias order applied to the stored attributes.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", sorted(SPAN_NAMES))
def test_columns_follow_the_adapter_alias_order(
    fixture: str, aliases: dict[str, list[str]], columns_golden: dict[str, Any]
) -> None:
    spans = post_fixture(fixture)["spans"]
    derived = {span["name"]: derive_columns(span, aliases) for span in spans}
    assert derived == columns_golden["columns"][fixture]


def test_era_b_stores_the_request_model_not_the_response_model(
    aliases: dict[str, list[str]],
) -> None:
    (chat,) = [s for s in post_fixture("era_b.json")["spans"] if s["name"] == "chat gpt-test"]
    assert stored_values(chat)["gen_ai.response.model"] == "gpt-test-actual"
    assert derive_columns(chat, aliases)["model"] == "gpt-test"
    models = aliases["modelNameKeys"]
    assert models.index("gen_ai.request.model") < models.index("gen_ai.response.model")


def test_era_a_provider_comes_from_gen_ai_system_and_tokens_are_a_recorded_miss(
    aliases: dict[str, list[str]],
) -> None:
    (chat,) = post_fixture("era_a.json")["spans"]
    columns = derive_columns(chat, aliases)
    assert columns["provider"] == "openai"
    assert "gen_ai.system" in aliases["providerKeys"]
    # Recorded as returned: era A token keys are not aliases, so 0, not 3 and 4.
    assert (columns["prompt_tokens"], columns["completion_tokens"]) == (0, 0)
    assert "gen_ai.usage.prompt_tokens" not in aliases["inputTokenKeys"]
    assert "gen_ai.usage.completion_tokens" not in aliases["outputTokenKeys"]


def test_dual_emit_prompt_tokens_are_3_not_6(
    aliases: dict[str, list[str]], columns_golden: dict[str, Any]
) -> None:
    (span,) = post_fixture("dual_emit.json")["spans"]
    stored = stored_values(span)
    assert stored == {"gen_ai.usage.input_tokens": 3, "gen_ai.usage.prompt_tokens": 3}
    columns = derive_columns(span, aliases)
    assert columns["prompt_tokens"] == 3
    assert columns["total_tokens"] == 3
    assert columns_golden["columns"]["dual_emit.json"]["dual_emit"]["prompt_tokens"] == 3

    # Control: a summed value (6) fails both the stored-attribute golden and
    # the column golden.
    summed = copy.deepcopy(span)
    for attribute in summed["attributes"]:
        if attribute["key"] == "gen_ai.usage.input_tokens":
            attribute["value"] = {"intValue": "6"}
    with pytest.raises(AssertionError, match="spans do not match golden"):
        compare([summed], golden_path("dual_emit.json"))
    assert derive_columns(summed, aliases) != columns_golden["columns"]["dual_emit.json"]["dual_emit"]


# --------------------------------------------------------------------------
# Opt-in: check the recorded alias order against fi-collector's adapter.go.
# --------------------------------------------------------------------------


def read_alias_lists(source: str) -> dict[str, dict[str, Any]]:
    """Each ``<name> = []string{...}`` list in adapter.go: its keys in order and its line span."""
    lists = {}
    for name in ALIAS_LISTS:
        match = re.search(
            r"^[ \t]*" + name + r"\s*=\s*\[\]string\{(.*?)^[ \t]*\}",
            source,
            flags=re.MULTILINE | re.DOTALL,
        )
        assert match, name
        body = re.sub(r"//[^\n]*", "", match.group(1))
        lists[name] = {
            "lines": [source.count("\n", 0, match.start()) + 1, source.count("\n", 0, match.end()) + 1],
            "keys": re.findall(r'"([^"]*)"', body),
        }
    return lists


def test_recorded_alias_order_matches_adapter_go(columns_golden: dict[str, Any]) -> None:
    path = os.environ.get("FI_COLLECTOR_ADAPTER_GO")
    if not path:
        pytest.skip("set FI_COLLECTOR_ADAPTER_GO to fi-collector/pkg/adapter/adapter.go")
    source = Path(path).read_text(encoding="utf-8")
    assert read_alias_lists(source) == columns_golden["aliases"]
    # derive_columns reads the lists in the roles DeriveHotKeys gives them.
    for line in (
        "hk.Model = firstString(attrsString, modelNameKeys)",
        "hk.Provider = firstString(attrsString, providerKeys)",
        'hk.GenAISystem = attrsString["gen_ai.system"]',
        'if v, ok := attrsString["gen_ai.operation.name"]; ok {',
        "if v, ok := firstNumber(attrsString, attrsNumber, inputTokenKeys); ok {",
        "if v, ok := firstNumber(attrsString, attrsNumber, outputTokenKeys); ok {",
        "if v, ok := firstNumber(attrsString, attrsNumber, totalTokenKeys); ok {",
        "} else if hk.PromptTokens+hk.CompletionTokens > 0 {",
        "hk.TotalTokens = hk.PromptTokens + hk.CompletionTokens",
    ):
        assert line in source, line


def test_readme_states_what_the_fixtures_are() -> None:
    readme = " ".join(README.read_text(encoding="utf-8").split())
    for fact in (
        "fixtures of these keys",
        "not a claim that Future AGI implements a named spec version",
        "e07f4ebacb08f56db8c4c882d117720333fbca04",
        "4af5338",
        "FI_COLLECTOR_ADAPTER_GO",
        *SPAN_NAMES,
    ):
        assert fact in readme, fact
