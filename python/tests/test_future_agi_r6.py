"""Host-only matching, header helpers, session copies, the plain-text warning and the public wording."""
import ast
import logging
import pathlib
import re

import pytest
import requests

from fi_instrumentation import otel

FI_KEY, FI_SECRET = "synthetic-env-key", "synthetic-env-secret"
ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "FI_TRACES_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FI_API_KEY", FI_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", FI_SECRET)
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FI_GRPC_URL", "127.0.0.1:9")


def _sent_headers(exporter):
    names = set()
    client = getattr(exporter, "_client", None)
    if isinstance(getattr(client, "_headers", None), dict):
        names |= {k.lower() for k in client._headers}
    session = getattr(exporter, "_session", None)
    if session is not None:
        names |= {k.lower() for k in session.headers}
    names |= {k.lower() for k, _ in (getattr(exporter, "_headers", None) or ()) if isinstance(exporter._headers, (tuple, list))}
    return names


OWN_HTTP = [
    "http://otel-collector:4318/futureagi/v1/traces",
    "http://otel-collector:4318/v1/traces?tenant=futureagi",
    "http://otel-collector:4318/v1/traces#futureagi",
    "https://futureagi-collector.corp/v1/traces",
    "https://myfutureagi.corp/v1/traces",
    "https://futureagi.com.example/v1/traces",
    "https://collector.example/api.futureagi.com/v1/traces",
]


@pytest.mark.parametrize("endpoint", OWN_HTTP)
def test_an_own_http_collector_is_built_and_gets_no_future_agi_keys(endpoint):
    assert otel._future_agi_decision(endpoint) == "other"
    exporter = otel.HTTPSpanExporter(endpoint=endpoint, timeout=2)
    try:
        assert {"x-api-key", "x-secret-key"}.isdisjoint(_sent_headers(exporter))
    finally:
        exporter.shutdown()


OWN_GRPC = ["futureagi-collector.corp:4317", "myfutureagi.corp:4317", "futureagi.com.example:4317"]


@pytest.mark.parametrize("endpoint", OWN_GRPC)
def test_an_own_grpc_collector_is_built_and_gets_no_future_agi_keys(endpoint):
    pytest.importorskip("grpc")
    assert otel._future_agi_decision(endpoint, grpc=True) == "other"
    exporter = otel.GRPCSpanExporter(endpoint=endpoint, timeout=2)
    try:
        assert {"x-api-key", "x-secret-key"}.isdisjoint(_sent_headers(exporter))
    finally:
        exporter.shutdown()


LOOK_ALIKE_HOSTS = [
    "https://futureagi.com/v1/traces",
    "https://API.FUTUREAGI.COM:8443/v1/traces",
    "https://api.futureagi.com.:8443/v1/traces",
    "https://ａｐｉ.futureagi.com/v1/traces",
    "https://xn--" + "ａｐｉ".encode("punycode").decode() + ".futureagi.com/v1/traces",
    "http://api.futureagi.com/v1/traces",
]


@pytest.mark.parametrize("endpoint", LOOK_ALIKE_HOSTS)
def test_a_futureagi_host_that_is_not_an_exact_origin_is_still_refused(endpoint):
    assert otel._future_agi_decision(endpoint) == "refuse"
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        otel.HTTPSpanExporter(endpoint=endpoint, timeout=2)


def test_caller_headers_follow_the_same_rules_as_materialized_headers():
    pairs = [("X-Api-Key", "kept"), ("x-api-key", ""), ("X-Other", None), (b"X-Bytes", "b")]
    assert otel._caller_headers(pairs) == {"x-api-key": "kept", "x-bytes": "b"}
    assert otel._caller_headers(iter(pairs)) == {"x-api-key": "kept", "x-bytes": "b"}
    assert otel._caller_headers(None) == {}
    assert otel._caller_headers({"A": "1", "a": ""}) == {"a": "1"}
    assert otel._caller_headers("x-a=1,X-A=2") == {"x-a": "2"}


class _Tagged(requests.Session):
    def __init__(self):
        super().__init__()
        self.tag = "tagged"


class _NeedsArgument(requests.Session):
    def __init__(self, required):
        super().__init__()
        self.required = required


def test_a_session_subclass_is_kept_on_the_copy():
    session = _Tagged()
    session.headers["X-Mine"] = "1"
    exporter = otel.HTTPSpanExporter(endpoint="https://api.futureagi.com/tracer/v1/traces", session=session, timeout=2)
    try:
        assert exporter._session is not session
        assert isinstance(exporter._session, _Tagged)
        assert exporter._session.headers["X-Mine"] == "1"
        assert {"x-api-key", "x-secret-key"}.isdisjoint({k.lower() for k in session.headers})
    finally:
        exporter.shutdown()


def test_a_session_subclass_that_needs_arguments_falls_back_to_a_plain_session():
    session = _NeedsArgument("x")
    exporter = otel.HTTPSpanExporter(endpoint="https://api.futureagi.com/tracer/v1/traces", session=session, timeout=2)
    try:
        assert exporter._session is not session
        assert isinstance(exporter._session, requests.Session)
    finally:
        exporter.shutdown()


@pytest.fixture
def fresh_warnings(monkeypatch):
    warned = getattr(otel, "_PLAIN_TEXT_WARNED", None)
    if isinstance(warned, set):
        warned.clear()
    yield
    if isinstance(warned, set):
        warned.clear()


def test_a_self_hosted_http_base_url_on_another_host_is_allowed_with_one_warning(monkeypatch, caplog, fresh_warnings):
    monkeypatch.setenv("FI_BASE_URL", "http://collector.internal:8080/path?token=secret-value")
    with caplog.at_level(logging.WARNING, logger=otel.logger.name):
        for _ in range(2):
            exporter = otel.HTTPSpanExporter(timeout=2)
            assert "x-api-key" in _sent_headers(exporter)
            exporter.shutdown()
    warnings = [r.getMessage() for r in caplog.records if "FI_BASE_URL" in r.getMessage()]
    assert len(warnings) == 1, caplog.text
    assert "http://collector.internal:8080" in warnings[0]
    for hidden in (FI_KEY, FI_SECRET, "secret-value", "/path"):
        assert hidden not in warnings[0]


@pytest.mark.parametrize("base", ["http://127.0.0.1:4318", "http://localhost:4318", "http://[::1]:4318", "https://self-hosted.example"])
def test_no_plain_text_warning_for_loopback_or_https(monkeypatch, caplog, fresh_warnings, base):
    monkeypatch.setenv("FI_BASE_URL", base)
    with caplog.at_level(logging.WARNING, logger=otel.logger.name):
        otel.HTTPSpanExporter(timeout=2).shutdown()
    assert not [r for r in caplog.records if "FI_BASE_URL" in r.getMessage()], caplog.text


def test_a_grpc_refusal_after_the_channel_is_built_closes_the_channel(monkeypatch):
    grpc = pytest.importorskip("grpc")
    closed, opened = [], []
    real_insecure, real_secure = grpc.insecure_channel, grpc.secure_channel

    def spy(real):
        def make(*args, **kwargs):
            channel = real(*args, **kwargs)
            original = channel.close
            channel.close = lambda: (closed.append(1), original())[1]
            opened.append(channel)
            return channel

        return make

    import opentelemetry.exporter.otlp.proto.grpc.exporter as base

    for module in (grpc, base):
        for name, real in (("insecure_channel", real_insecure), ("secure_channel", real_secure)):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, spy(real))
    real_check = otel._check_future_agi_endpoint
    calls = []

    def check(endpoint, is_grpc, caller_headers=None):
        calls.append(endpoint)
        if is_grpc and len(calls) == 2:
            raise otel.UnsupportedFutureAGIEndpointError("refused")
        return real_check(endpoint, is_grpc, caller_headers)

    monkeypatch.setattr(otel, "_check_future_agi_endpoint", check)
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        otel.GRPCSpanExporter(timeout=2)
    assert opened and len(closed) == len(opened)


# ---- public wording -------------------------------------------------------------------------------------------

BANNED = ("evil", "attacker", "attack", "leak", "forward your keys", "cleartext", "look like", "fixed", "now",
          "previously", "vulnerab", "exploit", "never", "just")
# Functions and classes of otel.py added or reworded since the 1.5.2 work began.
ADDED = (
    "_with_fi_auth_headers", "_ambient_headers", "_materialize_headers", "_caller_headers",
    "UnsupportedFutureAGIEndpointError", "_normalize_host", "_names_future_agi_host", "_http_origin", "_grpc_target",
    "_known_future_agi_origins", "_is_futureagi_host", "_future_agi_decision", "_is_future_agi_endpoint",
    "_fi_keys_configured", "_check_future_agi_endpoint", "UnsupportedOTLPExporterError", "_unsupported_exporter",
    "_copy_session", "_pin_http_headers", "_pin_grpc_headers", "GRPCSpanExporter", "HTTPSpanExporter",
    "_documented_init_signature", "_get_class_signature", "_warn_plain_text_once", "_is_loopback_host",
    "_force_tls_for_future_agi",
)


def _banned_in(text):
    lowered = text.lower()
    return [word for word in BANNED if re.search(rf"(?<![a-z0-9_]){re.escape(word)}(?![a-z0-9_])", lowered)]


def _strings(node):
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            yield child.value


def test_the_added_docstrings_and_messages_use_the_public_wording():
    tree = ast.parse((ROOT / "fi_instrumentation" / "otel.py").read_text())
    found = {node.name: node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in ADDED}
    assert {"_future_agi_decision", "GRPCSpanExporter", "_copy_session"} <= set(found)
    problems = {name: _banned_in(" ".join(_strings(node))) for name, node in found.items()}
    assert {name: words for name, words in problems.items() if words} == {}


def test_the_1_5_2_changelog_section_uses_the_public_wording():
    text = (ROOT / "CHANGELOG.md").read_text()
    start = text.index("## [1.5.2]")
    section = text[start : text.index("\n## [", start + 1)]
    assert "### Fixed" not in section
    assert "### Changed" in section
    assert _banned_in(section) == []
