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
and the ``firstString``/``firstNumber`` source against a real ``adapter.go``.
These tests never run the Go collector.

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
from typing import Any, Callable, Iterator, Optional

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
    "dual_emit.json": ["dual_emit", "dual_emit prompt_tokens"],
}
# Prompt, completion and total token columns per dual-emit span.
DUAL_EMIT_TOKENS = {"dual_emit": (3, 4, 7), "dual_emit prompt_tokens": (3, 0, 3)}
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


def split(span: dict[str, Any]) -> tuple[dict[str, str], dict[str, float]]:
    """Split (adapter.go:62-107) for these value types: strings go to
    attrs_string, int and double values to attrs_number."""
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
    return strings, numbers


def first_string(strings: dict[str, str], keys: list[str]) -> str:
    """firstString (adapter.go:317-324): the first alias with a non-empty string."""
    return next((strings[key] for key in keys if strings.get(key)), "")


def first_number(strings: dict[str, str], numbers: dict[str, float], keys: list[str]) -> Optional[float]:
    """firstNumber (adapter.go:330-346): walk the aliases in order; for each,
    a number, else a numeric string. The first hit wins; nothing is added."""
    for key in keys:
        if key in numbers:
            return numbers[key]
        if key in strings:
            try:
                return float(strings[key].strip())
            except ValueError:
                continue
    return None


def sum_numbers(strings: dict[str, str], numbers: dict[str, float], keys: list[str]) -> Optional[float]:
    """Control only, not fi-collector: add every alias present."""
    found = [numbers[key] for key in keys if key in numbers]
    return sum(found) if found else None


NumberRule = Callable[[dict[str, str], dict[str, float], list[str]], Optional[float]]


def derive_columns(
    span: dict[str, Any], aliases: dict[str, list[str]], number: NumberRule = first_number
) -> dict[str, Any]:
    """fi-collector's DeriveHotKeys (adapter.go:212-235) for the value types in these fixtures.

    Total falls back to prompt + completion when no total alias is set and that
    sum is positive (:230-235). ``number`` is the firstNumber mirror; the
    dual-emit control passes ``sum_numbers`` to show the check catches a sum.
    """
    strings, numbers = split(span)
    prompt = int(number(strings, numbers, aliases["inputTokenKeys"]) or 0)
    completion = int(number(strings, numbers, aliases["outputTokenKeys"]) or 0)
    total = number(strings, numbers, aliases["totalTokenKeys"])
    if total is None:
        total = prompt + completion if prompt + completion > 0 else 0
    return {
        "model": first_string(strings, aliases["modelNameKeys"]),
        "provider": first_string(strings, aliases["providerKeys"]),
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


def test_first_number_and_first_string_follow_adapter_go(aliases: dict[str, list[str]]) -> None:
    """The mirror behaves as the source pinned in columns.golden.json["helpers"]."""
    prompt = aliases["inputTokenKeys"]
    fi_name, otel_name = prompt[0], prompt[1]
    # The first alias present wins and nothing is added.
    assert first_number({}, {fi_name: 3.0, otel_name: 5.0}, prompt) == 3.0
    assert first_number({}, {otel_name: 5.0}, prompt) == 5.0
    assert first_number({}, {}, prompt) is None
    # Alias order decides, not the value type: a numeric string on the first
    # alias beats a number on the second; a non-numeric string is skipped.
    assert first_number({fi_name: " 3 "}, {otel_name: 5.0}, prompt) == 3.0
    assert first_number({fi_name: "n/a"}, {otel_name: 5.0}, prompt) == 5.0
    # For one key, the number is read before a numeric string.
    assert first_number({fi_name: "5"}, {fi_name: 3.0}, prompt) == 3.0
    # An empty string is skipped; the first non-empty alias wins.
    models = aliases["modelNameKeys"]
    assert first_string({models[0]: "", models[1]: "gpt-test", models[2]: "gpt-test-actual"}, models) == "gpt-test"
    assert first_string({}, models) == ""


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


def tokens(columns: dict[str, Any]) -> tuple[int, int, int]:
    return columns["prompt_tokens"], columns["completion_tokens"], columns["total_tokens"]


def check_dual_emit(columns: dict[str, dict[str, Any]]) -> None:
    assert {name: tokens(span_columns) for name, span_columns in columns.items()} == DUAL_EMIT_TOKENS


def test_dual_emit_takes_the_first_alias_and_does_not_add(
    aliases: dict[str, list[str]], columns_golden: dict[str, Any]
) -> None:
    spans = {span["name"]: span for span in post_fixture("dual_emit.json")["spans"]}
    # Two names for each count, and both names are aliases, so a sum would show.
    assert stored_values(spans["dual_emit"]) == {
        "llm.token_count.prompt": 3,
        "gen_ai.usage.input_tokens": 3,
        "llm.token_count.completion": 4,
        "gen_ai.usage.output_tokens": 4,
    }
    assert {"llm.token_count.prompt", "gen_ai.usage.input_tokens"} <= set(aliases["inputTokenKeys"])
    assert {"llm.token_count.completion", "gen_ai.usage.output_tokens"} <= set(aliases["outputTokenKeys"])
    # The era A name next to the era B name: only the era B name is an alias.
    assert stored_values(spans["dual_emit prompt_tokens"]) == {
        "gen_ai.usage.input_tokens": 3,
        "gen_ai.usage.prompt_tokens": 3,
    }
    columns = {name: derive_columns(span, aliases) for name, span in spans.items()}
    check_dual_emit(columns)
    assert columns == columns_golden["columns"]["dual_emit.json"]

    # Control: a stored 6 on the first alias fails both goldens.
    stored_six = copy.deepcopy(spans["dual_emit"])
    for attribute in stored_six["attributes"]:
        if attribute["key"] == "llm.token_count.prompt":
            attribute["value"] = {"intValue": "6"}
    with pytest.raises(AssertionError, match="spans do not match golden"):
        compare([stored_six, spans["dual_emit prompt_tokens"]], golden_path("dual_emit.json"))
    assert derive_columns(stored_six, aliases) != columns_golden["columns"]["dual_emit.json"]["dual_emit"]


def test_a_summing_derivation_fails_the_dual_emit_check(aliases: dict[str, list[str]]) -> None:
    """Control: adding every alias present, instead of taking the first,
    gives 6 / 8 / 14 on ``dual_emit`` and fails the check above."""
    spans = {span["name"]: span for span in post_fixture("dual_emit.json")["spans"]}
    summed = {name: derive_columns(span, aliases, number=sum_numbers) for name, span in spans.items()}
    assert tokens(summed["dual_emit"]) == (6, 8, 14)
    with pytest.raises(AssertionError):
        check_dual_emit(summed)

    # ``dual_emit prompt_tokens`` cannot tell the two apart while
    # gen_ai.usage.prompt_tokens is not an alias. Appended as one (SF-1),
    # first-wins would still give 3 and a sum 6.
    old_name = spans["dual_emit prompt_tokens"]
    assert derive_columns(old_name, aliases, number=sum_numbers)["prompt_tokens"] == 3
    with_old_name = {**aliases, "inputTokenKeys": [*aliases["inputTokenKeys"], "gen_ai.usage.prompt_tokens"]}
    assert derive_columns(old_name, with_old_name)["prompt_tokens"] == 3
    assert derive_columns(old_name, with_old_name, number=sum_numbers)["prompt_tokens"] == 6


# --------------------------------------------------------------------------
# Opt-in: check the recorded alias order and the firstString/firstNumber
# source against fi-collector's adapter.go.
# --------------------------------------------------------------------------

HELPERS = ("firstString", "firstNumber")
# Changes to the helper bodies that keep every alias list, line span and
# DeriveHotKeys call site as they are, written against adapter.go @ 4af5338.
NUMBER_BLOCK = "\t\tif v, ok := attrsNumber[k]; ok {\n\t\t\treturn v, true\n\t\t}\n"
STRING_BLOCK = (
    "\t\tif s, ok := attrsString[k]; ok {\n"
    "\t\t\tif v, err := strconv.ParseFloat(strings.TrimSpace(s), 64); err == nil {\n"
    "\t\t\t\treturn v, true\n\t\t\t}\n\t\t}\n"
)
HELPER_MUTATIONS = {
    "firstNumber adds every alias present": (
        "firstNumber",
        [
            ("(float64, bool) {", "(sum float64, found bool) {"),
            ("return v, true", "sum, found = sum+v, true"),
            ("return 0, false", "return sum, found"),
        ],
    ),
    "firstNumber reads a numeric string before a number": (
        "firstNumber",
        [(NUMBER_BLOCK + STRING_BLOCK, STRING_BLOCK + NUMBER_BLOCK)],
    ),
    "firstNumber takes the last alias present": (
        "firstNumber",
        [("for _, k := range keys {", "for i := range keys { k := keys[len(keys)-1-i]")],
    ),
    "firstString accepts an empty string": ("firstString", [('ok && v != ""', "ok")]),
}


@pytest.fixture(scope="module")
def adapter_go() -> str:
    path = os.environ.get("FI_COLLECTOR_ADAPTER_GO")
    if not path:
        pytest.skip("set FI_COLLECTOR_ADAPTER_GO to fi-collector/pkg/adapter/adapter.go")
    return Path(path).read_text(encoding="utf-8")


def find_function(source: str, name: str) -> re.Match[str]:
    match = re.search(r"^func " + name + r"\(.*?^\}", source, flags=re.MULTILINE | re.DOTALL)
    assert match, name
    return match


def read_helpers(source: str) -> dict[str, dict[str, Any]]:
    """firstString and firstNumber in adapter.go: line span and source lines, indentation stripped."""
    helpers = {}
    for name in HELPERS:
        match = find_function(source, name)
        helpers[name] = {
            "lines": [source.count("\n", 0, match.start()) + 1, source.count("\n", 0, match.end()) + 1],
            "source": [line.strip() for line in match.group(0).splitlines()],
        }
    return helpers


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


def test_recorded_alias_order_matches_adapter_go(adapter_go: str, columns_golden: dict[str, Any]) -> None:
    assert read_alias_lists(adapter_go) == columns_golden["aliases"]
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
        assert line in adapter_go, line


def test_recorded_first_string_and_first_number_match_adapter_go(
    adapter_go: str, columns_golden: dict[str, Any]
) -> None:
    """first_string and first_number mirror this source: the first alias
    present wins, nothing is added, and a number is read before a numeric
    string. Any change to it fails here."""
    assert read_helpers(adapter_go) == columns_golden["helpers"]


@pytest.mark.parametrize("mutation", sorted(HELPER_MUTATIONS))
def test_a_changed_helper_body_fails_the_drift_check(
    adapter_go: str, columns_golden: dict[str, Any], mutation: str
) -> None:
    """Control: each change keeps the alias lists and line spans, so only the
    helper source check catches it. The edits are written against 4af5338;
    if adapter.go itself changes, this fails along with the check above."""
    name, replacements = HELPER_MUTATIONS[mutation]
    match = find_function(adapter_go, name)
    body = match.group(0)
    for old, new in replacements:
        assert old in body, (mutation, old)
        body = body.replace(old, new)
    mutated = adapter_go[: match.start()] + body + adapter_go[match.end() :]
    assert read_alias_lists(mutated) == columns_golden["aliases"]
    assert read_helpers(mutated) != columns_golden["helpers"]


def test_readme_states_what_the_fixtures_are() -> None:
    readme = " ".join(README.read_text(encoding="utf-8").split())
    for fact in (
        "fixtures of these keys",
        "not a claim that Future AGI implements a named spec version",
        "e07f4ebacb08f56db8c4c882d117720333fbca04",
        "4af5338",
        "FI_COLLECTOR_ADAPTER_GO",
        # otel_genai.py falls back to gen_ai.usage.prompt_tokens; fi-collector
        # has no such alias. That is absence, not the opposite order.
        "fi-collector does not read `gen_ai.usage.prompt_tokens` at all",
        *SPAN_NAMES,
    ):
        assert fact in readme, fact
