"""Contract test for the OpenInference active-ingestion fixture (TH-8336).

Posts the hand-built OTLP/JSON body in ``fixtures/openinference-trace.otlp.json``
with the shared harness ``post_otlp()`` and checks what the harness
``Receiver`` decoded with ``compare()`` against
``fixtures/openinference-spans.golden.json``. The Receiver serves
``/v1/traces`` and ``/tracer/v1/traces`` on 127.0.0.1 like fi-collector's
HTTP mux, but it does not authenticate, stamp projects, resolve span kinds or
store anything. Every always-on assertion here is therefore about the posted
body (what a collector receives), not about a stored row or a rendered trace.

Nothing imports ``openinference-instrumentation`` or the backend's Python
adapter (``tracer.utils.adapters.openinference``). The harness sends no auth
header, so no key, real or placeholder, is involved.

Four opt-in tests read fi-collector's Go source (``FI_COLLECTOR_SRC``) and match
the tables and statements behind the README's source-reading facts. They read
the source; they do not run the collector.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

import harness
from harness import Receiver, compare, post_otlp

TESTS_DIR = Path(__file__).resolve().parent
RECIPE_DIR = TESTS_DIR.parent
README = RECIPE_DIR / "README.md"
BODY_PATH = TESTS_DIR / "fixtures" / "openinference-trace.otlp.json"
GOLDEN = TESTS_DIR / "fixtures" / "openinference-spans.golden.json"

PROJECT = "openinference-fixture-contract"
SERVICE_NAME = "openinference-fixture"
MODEL = "gpt-4o-mini"
PROMPT_TOKENS = "120"  # OTLP/JSON encodes int64 as a decimal string
COMPLETION_TOKENS = "38"
QUERY = "What is the refund window?"

RETRIEVER, LLM, TOOL = "retrieve", "ChatCompletion", "lookup_refund_policy"
# The exact strings posted; the data contract records them as set, not
# uppercased or lowercased by this test.
KINDS = {RETRIEVER: "RETRIEVER", LLM: "LLM", TOOL: "TOOL"}
KIND_KEY = "openinference.span.kind"
# The other keys fi-collector reads a span kind from, in its order
# (exporter/clickhouse25exporter/converter.go:79-90). The fixture sets none of
# them, so the collector can only take the kind from openinference.span.kind.
OTHER_KIND_KEYS = ("fi.span.kind", "gen_ai.span.kind", "llm.request.type", "gen_ai.operation.name")

# Kind values fi-collector 4af5338 does not list as an observation type
# (converter.go:64-68). PROMPT and DECISION are real values in
# openinference-semantic-conventions 0.1.41; "" is an empty kind.
UNKNOWN_KINDS = ("PROMPT", "DECISION", "", "not-a-kind")

HEX_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
HEX_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")


def load_body() -> dict[str, Any]:
    return json.loads(BODY_PATH.read_text(encoding="utf-8"))


def body_spans(body: dict[str, Any]) -> list[dict[str, Any]]:
    (resource_spans,) = body["resourceSpans"]
    (scope_spans,) = resource_spans["scopeSpans"]
    return scope_spans["spans"]


def attributes(span: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Attribute key -> OTLP AnyValue, typed value kept (e.g. ``{"intValue": "120"}``)."""
    return {item["key"]: item["value"] for item in span.get("attributes", [])}


def by_name(spans: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    named = {span["name"]: span for span in spans}
    assert len(named) == len(spans), sorted(span["name"] for span in spans)
    return named


@pytest.fixture(scope="module")
def posted() -> dict[str, Any]:
    """Post the fixture once; return the status and what the Receiver decoded."""
    with Receiver() as receiver:
        status = post_otlp(load_body(), receiver.endpoint)
        record = {"status": status, "spans": receiver.spans(), "requests": receiver.requests()}
    # Every test below reads this record: fail here, not vacuously later.
    assert record["status"] == 200
    assert record["requests"], "no export reached the receiver"
    assert record["spans"], "the export carried no spans"
    return record


# --------------------------------------------------------------------------
# The posted body
# --------------------------------------------------------------------------


def test_fixture_posts_once_to_v1_traces_without_keys(posted: dict[str, Any]) -> None:
    (request,) = posted["requests"]
    assert request["path"] == "/v1/traces"
    assert request["headers"]["content-type"] == "application/json"
    # The fixture is not authenticated: no key of any kind travels with it.
    for header in ("x-api-key", "x-secret-key", "authorization"):
        assert header not in request["headers"]


def test_posted_spans_match_the_golden(posted: dict[str, Any]) -> None:
    compare(posted["spans"], GOLDEN)
    # compare() checks names, attributes and statuses. The golden is the
    # posted body, so ids, parentage, kinds and times must match too.
    assert posted["spans"] == json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert posted["spans"] == body_spans(load_body())


def test_golden_fails_when_a_kind_value_changes(
    posted: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """Control: the golden pins the kind string exactly, case included."""
    spans = copy.deepcopy(posted["spans"])
    for span in spans:
        for item in span["attributes"]:
            if item["key"] == KIND_KEY:
                item["value"] = {"stringValue": item["value"]["stringValue"].lower()}
    with pytest.raises(AssertionError, match="spans do not match golden"):
        compare(spans, GOLDEN)
    assert '"stringValue": "llm"' in capsys.readouterr().out


def test_three_spans_in_one_trace_under_the_retriever(posted: dict[str, Any]) -> None:
    spans = by_name(posted["spans"])
    assert set(spans) == set(KINDS)
    assert len({span["traceId"] for span in spans.values()}) == 1
    assert len({span["spanId"] for span in spans.values()}) == 3
    root = spans[RETRIEVER]
    assert not root.get("parentSpanId")
    assert spans[LLM]["parentSpanId"] == root["spanId"]
    assert spans[TOOL]["parentSpanId"] == root["spanId"]


def test_ids_are_hex_as_the_collector_json_decoder_needs(posted: dict[str, Any]) -> None:
    """fi-collector decodes OTLP/JSON with pdata (pkg/server/server.go:439),
    which reads trace and span ids as hex. Base64 ids are a decode error,
    and the handler answers 400 (server.go:440)."""
    for span in posted["spans"]:
        assert HEX_TRACE_ID.match(span["traceId"]) and set(span["traceId"]) != {"0"}
        assert HEX_SPAN_ID.match(span["spanId"]) and set(span["spanId"]) != {"0"}
        if span.get("parentSpanId"):
            assert HEX_SPAN_ID.match(span["parentSpanId"])


def test_kind_values_are_posted_verbatim(posted: dict[str, Any]) -> None:
    for name, span in by_name(posted["spans"]).items():
        attrs = attributes(span)
        assert attrs[KIND_KEY] == {"stringValue": KINDS[name]}, name
        assert not set(OTHER_KIND_KEYS) & set(attrs), name


def test_llm_span_carries_model_and_token_keys(posted: dict[str, Any]) -> None:
    attrs = attributes(by_name(posted["spans"])[LLM])
    assert attrs == {
        KIND_KEY: {"stringValue": "LLM"},
        "llm.model_name": {"stringValue": MODEL},
        # intValue, not stringValue: the collector puts ints in attrs_number
        # (pkg/adapter/adapter.go:80-84).
        "llm.token_count.prompt": {"intValue": PROMPT_TOKENS},
        "llm.token_count.completion": {"intValue": COMPLETION_TOKENS},
    }


def test_retriever_query_is_readable_in_input_value(posted: dict[str, Any]) -> None:
    attrs = attributes(by_name(posted["spans"])[RETRIEVER])
    assert attrs == {
        KIND_KEY: {"stringValue": "RETRIEVER"},
        "input.value": {"stringValue": QUERY},
    }


def test_tool_span_carries_only_the_kind(posted: dict[str, Any]) -> None:
    assert attributes(by_name(posted["spans"])[TOOL]) == {KIND_KEY: {"stringValue": "TOOL"}}


def test_resource_carries_the_project(posted: dict[str, Any]) -> None:
    """On an authenticated request, fi-collector fails a batch whose resource
    has no project_name (pkg/auth/stamp.go:31-46; the handler answers 400,
    server.go:458-465)."""
    (request,) = posted["requests"]
    assert request["resource_attributes"] == [
        {"service.name": SERVICE_NAME, "project_name": PROJECT, "project_type": "observe"}
    ]


@pytest.mark.parametrize("kind", UNKNOWN_KINDS)
def test_unknown_kind_value_is_posted_without_error(kind: str) -> None:
    """The Receiver never reads a kind, so this shows only that the body
    stays well-formed and carries the value unchanged. That fi-collector
    stores such a span as ``unknown`` rather than rejecting it is read from
    its source (converter.go:123-129; see the opt-in tests)."""
    body = load_body()
    tool = copy.deepcopy(by_name(body_spans(body))[TOOL])
    tool["attributes"] = [{"key": KIND_KEY, "value": {"stringValue": kind}}]
    body["resourceSpans"][0]["scopeSpans"][0]["spans"] = [tool]
    with Receiver() as receiver:
        assert post_otlp(body, receiver.endpoint) == 200
        (span,) = receiver.spans()
    assert attributes(span) == {KIND_KEY: {"stringValue": kind}}


def test_post_otlp_refuses_non_loopback_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control for the only network path in this file: post_otlp()
    refuses anything but http://127.0.0.1 before it opens a connection."""

    def no_network(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("post_otlp opened a connection")

    monkeypatch.setattr(harness, "urlopen", no_network)
    # The patch is on the real path: a loopback endpoint reaches it.
    with pytest.raises(AssertionError, match="opened a connection"):
        post_otlp(load_body(), "http://127.0.0.1:9/v1/traces")
    for endpoint in (
        "http://192.0.2.1:4318/v1/traces",
        "http://localhost:4318/v1/traces",
        "https://127.0.0.1:4318/v1/traces",
    ):
        with pytest.raises(ValueError, match="loopback"):
            post_otlp(load_body(), endpoint)


def _adapter_imports(source: str) -> list[str]:
    """Modules under ``openinference`` or ``tracer`` that Python source imports,
    read with ``ast``: ``import``, ``from ... import``, and
    ``importlib.import_module()`` or ``__import__()`` of a string literal."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
        elif (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", None)) in ("import_module", "__import__")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names = [node.args[0].value]
        else:
            continue
        found += [name for name in names if name.split(".")[0] in ("openinference", "tracer")]
    return found


def test_no_file_here_imports_the_instrumentor_or_the_python_adapter() -> None:
    """Reads the source of every .py file in this example, and the harness.
    This catches an import that never runs here: one inside a function or a
    skipped test, or one of a package that is not installed."""
    # Control: the scan finds each import form it looks for, and only those.
    assert _adapter_imports(
        "import harness, opentelemetry\n"
        "import openinference.instrumentation\n"
        "from tracer.utils.adapters import openinference\n"
        "def later():\n"
        "    importlib.import_module('openinference.semconv')\n"
    ) == ["openinference.instrumentation", "tracer.utils.adapters", "openinference.semconv"]
    files = [*sorted(RECIPE_DIR.rglob("*.py")), Path(harness.__file__).resolve()]
    assert Path(__file__).resolve() in files
    for path in files:
        assert _adapter_imports(path.read_text(encoding="utf-8")) == [], path


def test_no_instrumentor_or_adapter_module_is_loaded(posted: dict[str, Any]) -> None:
    """Run-time complement to the source scan above: no ``openinference`` or
    ``tracer`` module is loaded after a post, including one imported
    indirectly by a dependency. With the README command neither package is
    installed, so this can fail only where one is (e.g. a round-trip run
    with the instrumentor)."""
    assert posted["spans"]
    loaded = [
        name
        for name in sys.modules
        if name in ("openinference", "tracer") or name.startswith(("openinference.", "tracer."))
    ]
    assert loaded == []


def test_fixture_contains_no_key_material() -> None:
    text = BODY_PATH.read_text(encoding="utf-8") + GOLDEN.read_text(encoding="utf-8")
    for pattern in (r"sk-", r"(?i)api[_-]?key", r"(?i)secret", r"(?i)bearer", r"(?i)password"):
        assert not re.search(pattern, text), pattern


def test_readme_states_what_the_tests_check() -> None:
    readme = README.read_text(encoding="utf-8")
    for fact in (
        "openinference-instrumentation 0.1.70",
        "otel_compat_urls.py",
        "is not proof",
        "post_otlp()",
        "compare()",
        BODY_PATH.name,
        GOLDEN.name,
        KIND_KEY,
        "llm.model_name",
        "llm.token_count.prompt",
        "llm.token_count.completion",
        "input.value",
        "attributes_extra",
        "project_name",
        *UNKNOWN_KINDS[:2],
    ):
        assert fact in readme, fact


def _readme_text() -> str:
    """The README with line breaks and runs of spaces collapsed."""
    return " ".join(README.read_text(encoding="utf-8").split())


def test_readme_qualifies_the_project_name_rules() -> None:
    """The 400 for a missing project_name needs an authenticated request, and
    a project_name that does not resolve is dropped with a 200, not rejected.
    The opt-in test_collector_project_name_rules reads the source lines."""
    readme = _readme_text()
    for fact in (
        "On an authenticated request, fi-collector fails a batch whose resource has no `project_name` with HTTP 400",
        "A collector with auth disabled skips this check",
        "is created",
        "the request is still answered 200",
    ):
        assert fact in readme, fact


# --------------------------------------------------------------------------
# Opt-in: read fi-collector's source instead of copying its tables.
# --------------------------------------------------------------------------


def _collector_source() -> dict[str, str]:
    root = os.environ.get("FI_COLLECTOR_SRC")
    if not root:
        pytest.skip("set FI_COLLECTOR_SRC to a fi-collector checkout")
    files = {
        "adapter": "pkg/adapter/adapter.go",
        "converter": "exporter/clickhouse25exporter/converter.go",
        "stamp": "pkg/auth/stamp.go",
        "auth": "pkg/auth/auth.go",
        "server": "pkg/server/server.go",
    }
    return {name: (Path(root) / path).read_text(encoding="utf-8") for name, path in files.items()}


_GO_STRING_OR_COMMENT = re.compile(r'"(?:\\.|[^"\\\n])*"|`[^`]*`|//[^\n]*|/\*.*?\*/', flags=re.DOTALL)


def _go_code(source: str) -> str:
    """Go source with comments removed and whitespace collapsed to one space,
    so a commented-out or reflowed statement does not match as live code."""
    source = _GO_STRING_OR_COMMENT.sub(lambda m: " " if m.group().startswith("/") else m.group(), source)
    return " ".join(source.split())


def _go_func(source: str, name: str) -> str:
    """The body of top-level function or method ``name``, as ``_go_code``.
    gofmt puts a function's closing brace, and only that, in column 0."""
    match = re.search(r"^func (?:\([^)]*\) )?" + re.escape(name) + r"\(", source, flags=re.MULTILINE)
    assert match, name
    start = source.index(" {\n", match.end()) + 3
    return _go_code(source[start : source.index("\n}\n", start)])


def _has_statements(code: str, *statements: str) -> None:
    for statement in statements:
        assert statement in code, statement


def _go_literal(source: str, name: str) -> str:
    """The body of ``name = []string{...}`` or ``name = map[...]...{...}``, comments removed."""
    match = re.search(
        r"\b" + re.escape(name) + r"\s*=\s*(?:\[\]string|map\[string\][\w{}]+?)\{\n(.*?)\n\s*\}\n",
        source,
        flags=re.DOTALL,
    )
    assert match, name
    return re.sub(r"//[^\n]*", "", match.group(1))


def _go_strings(source: str, name: str) -> list[str]:
    return re.findall(r'"([^"]*)"', _go_literal(source, name))


def _go_map_keys(source: str, name: str) -> set[str]:
    return set(re.findall(r'"([^"]*)"\s*:', _go_literal(source, name)))


def test_collector_reads_the_keys_the_fixture_sets() -> None:
    source = _collector_source()
    kind_keys = _go_strings(source["converter"], "spanKindAttrKeys")
    operation_keys = _go_strings(source["converter"], "operationNameAttrKeys")
    # KIND_KEY is the fourth kind key. Every other key the collector would
    # read a kind from is absent from the fixture, so the stored type comes
    # from KIND_KEY alone.
    assert kind_keys + operation_keys == [*OTHER_KIND_KEYS[:3], KIND_KEY, OTHER_KIND_KEYS[3]]
    # The fixture's model and token keys are the first alias of each list,
    # and it sets no total key, so total_tokens is derived.
    assert _go_strings(source["adapter"], "modelNameKeys")[0] == "llm.model_name"
    assert _go_strings(source["adapter"], "inputTokenKeys")[0] == "llm.token_count.prompt"
    assert _go_strings(source["adapter"], "outputTokenKeys")[0] == "llm.token_count.completion"
    llm_keys = set(attributes(by_name(body_spans(load_body()))[LLM]))
    assert not llm_keys & set(_go_strings(source["adapter"], "totalTokenKeys"))
    # input.value goes to the JSON overflow; the kind, model and token keys
    # do not, so they stay readable as strings and numbers.
    prefixes = _go_strings(source["adapter"], "overflowKeyPrefixes")
    assert "input.value" in prefixes
    for key in llm_keys:
        assert not any(key.startswith(prefix) for prefix in prefixes), key
    _has_statements(
        _go_func(source["adapter"], "hasOverflowPrefix"),
        "for _, p := range overflowKeyPrefixes { if strings.HasPrefix(key, p) { return true } }",
    )


def test_collector_type_list_has_the_fixture_kinds_but_not_the_unknown_ones() -> None:
    """converter.go takes the first non-empty kind key, lowercases it, applies
    spanKindSynonyms and returns ``unknown`` for anything not in
    knownObservationTypes. This reads those tables and statements; it does
    not run resolveObservationType."""
    source = _collector_source()
    _has_statements(
        _go_func(source["converter"], "resolveObservationType"),
        'raw := "" for _, k := range spanKindAttrKeys { if v := attrsStr[k]; v != "" { raw = v break } }',
        "t := strings.ToLower(strings.TrimSpace(raw)) if syn, ok := spanKindSynonyms[t]; ok { t = syn }",
        "if _, ok := knownObservationTypes[t]; !ok { return observationTypeUnknown } return t",
    )
    assert 'observationTypeUnknown = "unknown"' in _go_code(source["converter"])
    known = _go_map_keys(source["converter"], "knownObservationTypes")
    synonyms = _go_map_keys(source["converter"], "spanKindSynonyms")
    assert "unknown" in known
    for kind in KINDS.values():
        assert kind.lower() in known, kind
    for kind in UNKNOWN_KINDS:
        assert kind.lower() not in known | synonyms, kind


def test_collector_stores_the_kind_model_tokens_and_input() -> None:
    """The statements between the span's attributes and the stored row:
    Split routes strings and ints, DeriveHotKeys picks the model and tokens
    (deriving the total), and spanToRow stores what they and the kind
    resolver return. Matched as source text; nothing is run."""
    source = _collector_source()
    _has_statements(
        _go_func(source["adapter"], "Split"),
        "if hasOverflowPrefix(k) { overflow[k] = otelValueToJSON(v) return true }",
        "case pcommon.ValueTypeStr: attrsString[k] = v.Str()",
        "n := v.Int() if n >= minSafeInt && n <= maxSafeInt { attrsNumber[k] = float64(n) }",
    )
    _has_statements(
        _go_func(source["adapter"], "DeriveHotKeys"),
        "hk.Model = firstString(attrsString, modelNameKeys)",
        "if v, ok := firstNumber(attrsString, attrsNumber, inputTokenKeys); ok { hk.PromptTokens = int32(v) }",
        "if v, ok := firstNumber(attrsString, attrsNumber, outputTokenKeys); ok { hk.CompletionTokens = int32(v) }",
        "if v, ok := firstNumber(attrsString, attrsNumber, totalTokenKeys); ok { hk.TotalTokens = int32(v) }"
        " else if hk.PromptTokens+hk.CompletionTokens > 0 { hk.TotalTokens = hk.PromptTokens + hk.CompletionTokens }",
    )
    row = _go_func(source["converter"], "spanToRow")
    _has_statements(
        row,
        "adapter.Split(span.Attributes(), attrsStr, attrsNum, attrsBool, overflow)",
        "hot := adapter.DeriveHotKeys(attrsStr, attrsNum)",
        "observationType := resolveObservationType(attrsStr)",
        'input := overflowAsString(overflow, "input.value")',
        '"observation_type": observationType,',
        '"model": hot.Model,',
        '"prompt_tokens": hot.PromptTokens,',
        '"completion_tokens": hot.CompletionTokens,',
        '"total_tokens": hot.TotalTokens,',
        '"attributes_extra": overflow,',
        '"input": input,',
    )
    # One return, with a nil error: no kind value can fail the export.
    assert row.count("return ") == row.count("return row, identity, nil") == 1


def test_collector_project_name_rules() -> None:
    """A missing project_name is a 400 only on an authenticated request; a
    name not yet in the workspace is created; one that still does not
    resolve is dropped and the request answered 200 (handleHTTPTraces,
    StampResourceAttrs, ResolveProjectsForKey). Matched as source text."""
    source = _collector_source()
    handler = _go_func(source["server"], "handleHTTPTraces")
    assert handler.count("auth.StampResourceAttrs(") == 1
    # Stamping runs only inside the authenticated branch. Its error is the
    # 400; a drop is only logged, and the handler goes on to its 200.
    assert re.search(
        r"if result := auth\.FromContext\(r\.Context\(\)\); result != nil \{[^}]*"
        r"dropped, err := auth\.StampResourceAttrs\(.*?\) "
        r'if err != nil \{ http\.Error\(w, "auth stamp: "\+err\.Error\(\), http\.StatusBadRequest\) return \} '
        r"if dropped > 0 \{ s\.log\.Warn\([^{}]*\) \}",
        handler,
    )
    assert handler.endswith("w.WriteHeader(http.StatusOK) _, _ = w.Write(out)")
    _has_statements(
        _go_func(source["stamp"], "StampResourceAttrs"),
        "if result == nil { return 0, nil }",
        'pn := getStrAttr(attrs, "project_name") if pn == "" { missing = append(missing, i) continue }',
        "if len(missing) > 0 { return 0, fmt.Errorf(",
        'if !ok || projectID == "" { unresolvable[projectName] = struct{}{} continue }',
        "rss.RemoveIf(",
        "return before - rss.Len(), nil",
    )
    assert re.search(
        r'return resolver\.GetOrCreateProject\(ctx, result\.OrgID, result\.WorkspaceID, name, "observe"\) \}\) '
        r'if err != nil \{ a\.log\.Warn\("project auto-create failed"[^{}]*\) continue \}',
        _go_func(source["auth"], "ResolveProjectsForKey"),
    )
