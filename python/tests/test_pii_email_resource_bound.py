"""Deterministic email-scan controls; large cases fail fast on the old scanner."""

import ast
import importlib
import inspect
import re

import pytest

# Reuse the protected helper's cited call contract without modifying its file.
from test_sdk_cap_pii_class_fix import sdk as sdk, surfaces


TOKEN = "<EMAIL_ADDRESS>"
LEGACY_EMAIL = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)
SIZES = (1, 16, 256, 4096, 65536)
RESOURCE_KINDS = (
    "dotted-terminal",
    "hyphen-terminal",
    "percent-plus-terminal",
    "no-at-prose",
    "empty-domain",
    "short-tld",
    "domain-boundaries",
    "many-at-signs",
    "overlapping-runs",
    "late-positive",
    "long-positive",
    "greedy-domain",
    "positive-after-failures",
    "retained-prefix",
    "many-positives",
)
EMAIL_CASES = (
    ("plain", "alice@example.com", "<EMAIL_ADDRESS>"),
    ("tagged", "contact Alice.Smith+tag@example.co.uk.", "contact <EMAIL_ADDRESS>."),
    ("two", "x@a.co x@y.com", "<EMAIL_ADDRESS> <EMAIL_ADDRESS>"),
    ("surrounding", "prefix alice@example.com suffix", "prefix <EMAIL_ADDRESS> suffix"),
    ("underscore", "user_name@sub-domain.example", "<EMAIL_ADDRESS>"),
    ("leading-dot", ".alice@example.com", ".<EMAIL_ADDRESS>"),
    ("unicode-punctuation-start", "é.alice@example.com", "é<EMAIL_ADDRESS>"),
    ("unicode-word-start", "éabc@example.com", "éabc@example.com"),
    ("unicode-later-boundary", "éabc.def@example.com", "éabc<EMAIL_ADDRESS>"),
    ("unicode-underscore", "é_abc@example.com", "é_abc@example.com"),
    ("leading-hyphen", "-a@example.com", "-<EMAIL_ADDRESS>"),
    ("unicode-word-end", "a@example.comé", "a@example.comé"),
    ("underscore-end", "a@example.com_", "a@example.com_"),
    ("combining-end", "a@example.coḿ", "<EMAIL_ADDRESS>́"),
    ("hyphen-tail", "a@example.co-zzz", "<EMAIL_ADDRESS>-zzz"),
    ("digit-tail", "a@example.co9", "a@example.co9"),
    ("earlier-domain-match", "a@example.co.x1", "<EMAIL_ADDRESS>.x1"),
    ("empty-prefix-domain", "a@.com", "a@.com"),
    ("dot-prefix-domain", "a@..com", "<EMAIL_ADDRESS>"),
    ("hyphen-prefix-domain", "a@-x.co", "<EMAIL_ADDRESS>"),
    ("one-letter-tld", "a@x.c", "a@x.c"),
    ("consumed-local", "a@x.co@b.eu", "<EMAIL_ADDRESS>@b.eu"),
    ("failed-then-valid", "a@x@b.co", "a@<EMAIL_ADDRESS>"),
    ("original-boundary-after-match", "a@x.co+z@y.io", "<EMAIL_ADDRESS><EMAIL_ADDRESS>"),
    ("resume-before-domain-end", "a@x.co.z@y.io", "<EMAIL_ADDRESS><EMAIL_ADDRESS>"),
    ("greedy-before-at", "a@x.co.zz@y.io", "<EMAIL_ADDRESS>@y.io"),
    ("double-domain-dot", "a@x..co", "<EMAIL_ADDRESS>"),
    ("no-local-boundary", "!%@x.co", "!%@x.co"),
    ("punctuation-only-local", "é%@x.co", "é<EMAIL_ADDRESS>"),
    ("later-valid-tld", "x@foo.a1.zz", "<EMAIL_ADDRESS>"),
    ("all-tld-boundaries-invalid", "x@foo.ab9.cd_", "x@foo.ab9.cd_"),
    ("fallback-before-underscore", "x@foo.ab.cd_", "<EMAIL_ADDRESS>.cd_"),
    ("fallback-before-unicode", "x@foo.ab.cd中", "<EMAIL_ADDRESS>.cd中"),
    ("non-bmp-word", "𐐀.a@b.co", "𐐀<EMAIL_ADDRESS>"),
    ("unicode-numeric-word", "².a@b.co", "²<EMAIL_ADDRESS>"),
    ("newline", "a@b.co\nnext", "<EMAIL_ADDRESS>\nnext"),
    ("empty", "", ""),
    ("dotted-prose", "ordinary.dotted.prose", "ordinary.dotted.prose"),
    ("punctuation-negative", ".,%+-@", ".,%+-@"),
    ("terminal-at", "a.a.a.@", "a.a.a.@"),
)
MIXED_INPUT = (
    "alice@example.com | 123-45-6789 | 4111 1111 1111 1111 | "
    "sk_live_abcdefghijklmnopqrst | 192.168.1.1 | 212-555-0199"
)
MIXED_EXPECTED = (
    "<EMAIL_ADDRESS> | <SSN> | <CREDIT_CARD> | "
    "<API_KEY> | <IP_ADDRESS> | <PHONE_NUMBER>"
)
EXPORT_CASES = (
    ("positive", "prefix alice@example.com suffix", "prefix <EMAIL_ADDRESS> suffix"),
    ("mixed", MIXED_INPUT, MIXED_EXPECTED),
    ("terminal", "a." * 16 + "@", "a." * 16 + "@"),
    ("dotted-prose", "ordinary.dotted.prose " * 16, "ordinary.dotted.prose " * 16),
    ("after-failures", "a@" * 16 + "alice@example.com", "a@" * 16 + "<EMAIL_ADDRESS>"),
    ("uncapped-local", "a" * 4096 + "@example.com", "<EMAIL_ADDRESS>"),
)


@pytest.fixture
def pii_module():
    return importlib.import_module("fi_instrumentation.instrumentation.pii_redaction")


def _scanner(module):
    scanner = getattr(module, "_redact_email_addresses", None)
    assert callable(scanner), "dedicated linear email scanner is required before large inputs"
    return scanner


class _CountedText(str):
    """Count actual input character access and output copying, not elapsed time."""

    def __new__(cls, value):
        instance = super().__new__(cls, value)
        instance.index_reads = 0
        instance.slice_volume = 0
        return instance

    def __getitem__(self, key):
        if isinstance(key, slice):
            self.slice_volume += len(range(*key.indices(len(self))))
        else:
            self.index_reads += 1
        return super().__getitem__(key)


def _resource_case(kind, n):
    if kind == "dotted-terminal":
        text = "a." * n + "@"
    elif kind == "hyphen-terminal":
        text = "a-" * n + "@"
    elif kind == "percent-plus-terminal":
        text = "a%+" * n + "@"
    elif kind == "no-at-prose":
        text = "ordinary " + "a." * n
    elif kind == "empty-domain":
        text = "a." * n + "@ "
    elif kind == "short-tld":
        text = "a." * n + "@x.c"
    elif kind == "domain-boundaries":
        text = "x@" + "a." * n + "a"
    elif kind == "many-at-signs":
        text = "a@" * n
    elif kind == "overlapping-runs":
        text = "a.a.@" * n
    elif kind == "late-positive":
        prefix = "a." * n + "@"
        return prefix + "tail@example.com", prefix + TOKEN
    elif kind == "long-positive":
        return "a." * n + "z@example.com", TOKEN
    elif kind == "greedy-domain":
        return "x@" + "a." * n + "zz", TOKEN
    elif kind == "positive-after-failures":
        prefix = "a@" * n
        return prefix + "alice@example.com", prefix + TOKEN
    elif kind == "retained-prefix":
        prefix = "prefix " + "a." * n + "@ suffix "
        return prefix + "alice@example.com", prefix + TOKEN
    elif kind == "many-positives":
        return "a@b.co;" * n, (TOKEN + ";") * n
    else:
        raise AssertionError(f"unknown resource kind: {kind}")
    return text, text


def test_email_regex_removed_from_public_pass(pii_module):
    _scanner(pii_module)
    assert all(token != TOKEN for _, token in pii_module._PII_PATTERNS)
    assert pii_module.redact_pii_in_string("alice@example.com") == TOKEN
    assert pii_module.redact_pii_in_string("a.a.@") == "a.a.@"


def test_scanner_helpers_do_not_call_regex(pii_module):
    scanner = _scanner(pii_module)
    domain_scan = getattr(pii_module, "_email_match_end", None)
    assert callable(domain_scan)
    for function in (scanner, domain_scan):
        tree = ast.parse(inspect.getsource(function))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {
                    "sub", "subn", "search", "match", "fullmatch", "findall", "finditer", "compile"
                }, "a regex call bypasses indexed-work accounting"


@pytest.mark.parametrize("case_id,text,expected", EMAIL_CASES, ids=[case[0] for case in EMAIL_CASES])
def test_email_literal_compatibility(pii_module, case_id, text, expected):
    scanner = _scanner(pii_module)
    # Only these small literal cases use the old regex as an independent oracle.
    assert LEGACY_EMAIL.sub(TOKEN, text) == expected, case_id
    assert scanner(text) == expected, case_id
    assert pii_module.redact_pii_in_string(text) == expected, case_id


@pytest.mark.parametrize("kind", RESOURCE_KINDS)
@pytest.mark.parametrize("n", SIZES, ids=lambda n: f"n{n}")
def test_linear_indexed_work(pii_module, kind, n):
    scanner = _scanner(pii_module)
    text, expected = _resource_case(kind, n)
    counted = _CountedText(text)
    result = scanner(counted)
    assert result == expected
    assert len(text) <= counted.index_reads <= 8 * len(text) + 32
    assert counted.slice_volume <= len(text)
    if expected == text:
        assert result is counted


@pytest.mark.parametrize("local_length", (63, 64, 65, 254, 255, 4096))
def test_local_part_has_no_new_length_cap(pii_module, local_length):
    _scanner(pii_module)
    text = "a" * local_length + "@example.com"
    assert pii_module.redact_pii_in_string(text) == TOKEN


def test_other_pii_patterns_keep_order_and_complete_output(pii_module):
    _scanner(pii_module)
    assert pii_module.redact_pii_in_string(MIXED_INPUT) == MIXED_EXPECTED
    assert pii_module.redact_pii_in_string(MIXED_INPUT.split(" | ", 1)[1]) == MIXED_EXPECTED.split(" | ", 1)[1]
    assert [token for _, token in pii_module._PII_PATTERNS] == [
        "<SSN>", "<CREDIT_CARD>", "<API_KEY>", "<IP_ADDRESS>", "<PHONE_NUMBER>"
    ]


@pytest.mark.parametrize("container", (list, tuple), ids=("list", "tuple"))
def test_value_container_preservation(pii_module, container):
    _scanner(pii_module)
    original = container(["alice@example.com", "a.a.@", 7, None])
    expected = container([TOKEN, "a.a.@", 7, None])
    result = pii_module.redact_pii_in_value(original)
    assert type(result) is container
    assert result == expected
    unchanged = container(["a.a.@", "ordinary.dotted.prose", 7, None])
    assert pii_module.redact_pii_in_value(unchanged) is unchanged
    assert original == container(["alice@example.com", "a.a.@", 7, None])


def test_non_string_values_stay_identical(pii_module):
    mapping = {"email": "alice@example.com"}
    assert pii_module.redact_pii_in_value(mapping) is mapping
    assert pii_module.redact_pii_in_value(None) is None
    assert pii_module.redact_pii_in_value(7) == 7


@pytest.mark.parametrize("n", SIZES, ids=lambda n: f"n{n}")
def test_config_pii_on_off_and_plain_text_identity(pii_module, n):
    _scanner(pii_module)
    from fi_instrumentation.instrumentation.config import TraceConfig

    off = TraceConfig(pii_redaction=False)
    on = TraceConfig(pii_redaction=True)
    positive = "a." * n + "z@example.com"
    terminal = "a." * n + "@"
    prose = "ordinary " + "a." * n
    assert off.redact_value(positive) is positive
    assert off.redact_value(terminal) is terminal
    assert off.redact_value(prose) is prose
    assert on.redact_value(positive) == TOKEN
    assert on.redact_value(terminal) == terminal
    assert on.redact_value(prose) == prose


@pytest.mark.parametrize("case_id,text,on_expected", EXPORT_CASES, ids=[case[0] for case in EXPORT_CASES])
@pytest.mark.parametrize("enabled", (False, True), ids=("off", "on"))
def test_complete_exported_values(sdk, pii_module, monkeypatch, case_id, text, on_expected, enabled):
    _scanner(pii_module)
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from fi_instrumentation.instrumentation.config import TraceConfig

    # Record complete pre-redaction strings, including dynamically formatted
    # tracebacks. The old expression is used only on these bounded export cases;
    # no large malformed resource case is ever passed to this regex oracle.
    expected_complete_values = set()
    original_redact_value = TraceConfig.redact_value

    def observe_redaction(config, value, *args, **kwargs):
        result = original_redact_value(config, value, *args, **kwargs)
        if isinstance(value, str) and text in value:
            oracle = value
            if enabled:
                oracle = LEGACY_EMAIL.sub(TOKEN, oracle)
                for pattern, replacement in pii_module._PII_PATTERNS:
                    oracle = pattern.sub(replacement, oracle)
            assert result == oracle, "complete pre-redaction value changed unexpectedly"
            expected_complete_values.add(oracle)
        return result

    monkeypatch.setattr(TraceConfig, "redact_value", observe_redaction)
    captured = []
    original_export = InMemorySpanExporter.export

    def capture_export(exporter, spans):
        captured.extend(spans)
        return original_export(exporter, spans)

    monkeypatch.setattr(InMemorySpanExporter, "export", capture_export)
    expected = on_expected if enabled else text
    surfaces(sdk, text, expected, pii_redaction=enabled)
    assert captured, "full-path helper must actually export spans"

    def strings(values):
        result = []
        for value in values:
            if isinstance(value, str):
                result.append(value)
            elif isinstance(value, (list, tuple)):
                result.extend(item for item in value if isinstance(item, str))
        return result

    attribute_values = strings(value for span in captured for value in (span.attributes or {}).values())
    event_values = strings(
        value for span in captured for event in span.events for value in (event.attributes or {}).values()
    )
    exception_messages = strings(
        (event.attributes or {}).get("exception.message")
        for span in captured for event in span.events if event.name == "exception"
    )
    stacktraces = strings(
        (event.attributes or {}).get("exception.stacktrace")
        for span in captured for event in span.events if event.name == "exception"
    )
    descriptions = strings(span.status.description for span in captured)
    # These are complete scalar comparisons, never substring acceptance.
    for values in (attribute_values, event_values, exception_messages, descriptions):
        assert expected in values, (case_id, values)
    assert stacktraces, "exception stacktrace export must not be omitted"
    assert expected_complete_values, "observe actual configuration redaction inputs"
    for stacktrace in stacktraces:
        assert stacktrace in expected_complete_values, "complete exported stacktrace must match its oracle"
    if enabled and expected != text:
        for value in attribute_values + event_values + descriptions:
            assert text not in value, "original complete PII value survived export"
        for stacktrace in stacktraces:
            assert text not in stacktrace, "original PII survived stacktrace redaction"
