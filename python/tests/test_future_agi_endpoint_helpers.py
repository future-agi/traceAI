"""Host-only matching, header helpers, session copies, and the plain-text warning."""
import logging

import pytest
import requests

from fi_instrumentation import otel

FI_KEY, FI_SECRET = "synthetic-env-key", "synthetic-env-secret"


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
    held = getattr(exporter, "_headers", None) or ()
    names |= {str(k).lower() for k in (held if isinstance(held, dict) else [pair[0] for pair in held])}
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


def _held_session(exporter):
    """The session an exporter holds, on the exporter itself or on its client, depending on the OpenTelemetry release."""
    client = getattr(exporter, "_client", None)
    transport = getattr(client, "_transport", None)
    return getattr(exporter, "_session", None) or getattr(client, "_session", None) or getattr(transport, "_session", None)


def _assert_session_rules(exporter, session, expected_class):
    held = _held_session(exporter)
    assert held is not None, "the exporter holds a session on every supported release"
    if hasattr(exporter, "_session"):
        # The Future AGI headers are set on a copy of the caller's session.
        assert held is not session
        assert isinstance(held, expected_class)
    else:
        # The release keeps the session in its client and sends the keys per request: the caller's session is used as built.
        assert held is session
    assert {"x-api-key", "x-secret-key"}.isdisjoint({k.lower() for k in session.headers})


def test_a_session_subclass_is_kept_on_the_copy():
    session = _Tagged()
    session.headers["X-Mine"] = "1"
    exporter = otel.HTTPSpanExporter(endpoint="https://api.futureagi.com/tracer/v1/traces", session=session, timeout=2)
    try:
        _assert_session_rules(exporter, session, _Tagged)
        if hasattr(exporter, "_session"):
            assert exporter._session.headers["X-Mine"] == "1"
    finally:
        exporter.shutdown()


def test_a_session_subclass_that_needs_arguments_falls_back_to_a_plain_session():
    session = _NeedsArgument("x")
    exporter = otel.HTTPSpanExporter(endpoint="https://api.futureagi.com/tracer/v1/traces", session=session, timeout=2)
    try:
        _assert_session_rules(exporter, session, requests.Session)
    finally:
        exporter.shutdown()


class _CountingAdapter(requests.adapters.HTTPAdapter):
    was_closed = False

    def close(self):
        self.was_closed = True
        super().close()


def test_closing_the_exporter_does_not_close_the_callers_adapters():
    session = requests.Session()
    caller_adapter = _CountingAdapter(pool_maxsize=3, max_retries=2)
    session.mount("https://", caller_adapter)
    exporter = otel.HTTPSpanExporter(endpoint="https://api.futureagi.com/tracer/v1/traces", session=session, timeout=2)
    if not hasattr(exporter, "_session"):
        exporter.shutdown()
        pytest.skip("this OpenTelemetry release keeps the caller's session in its client and does not copy it")
    copied = exporter._session.adapters["https://"]
    assert copied is not caller_adapter
    assert isinstance(copied, _CountingAdapter) and copied.max_retries.total == 2 and copied._pool_maxsize == 3
    exporter.shutdown()
    assert copied.was_closed and not caller_adapter.was_closed


@pytest.mark.parametrize("env, target, decision", [
    ("https://grpc.futureagi.com", "grpc.futureagi.com:443", "fi"),
    ("https://grpc.futureagi.com", "grpc.futureagi.com:4317", "refuse"),
    ("http://grpc.futureagi.com:4317", "grpc.futureagi.com:4317", "fi"),
    ("http://grpc.futureagi.com:4317", "grpc.futureagi.com:443", "refuse"),
    ("127.0.0.1:9", "grpc.futureagi.com:443", "fi"),
    ("127.0.0.1:9", "grpc.futureagi.com:4317", "refuse"),
])
def test_an_argument_and_fi_grpc_url_are_held_to_one_port_rule(monkeypatch, env, target, decision):
    monkeypatch.setenv("FI_GRPC_URL", env)
    assert otel._future_agi_decision(target, grpc=True) == decision
    assert otel._future_agi_decision(env, grpc=True) in ("fi", "other")  # the environment's own target is its own rule


@pytest.mark.parametrize("host, resolves_to, loopback", [
    ("localhost", None, True),
    ("127.0.0.1", None, True),
    ("[::1]", None, True),
    ("app.localhost", "127.0.0.1", True),
    ("app.localhost", "93.184.216.34", False),
    ("localhost.", "127.0.0.1", True),
    ("localhost.", "10.0.0.5", False),
    ("collector.example", "127.0.0.1", False),
])
def test_a_name_is_loopback_when_it_is_localhost_or_resolves_only_to_loopback(monkeypatch, host, resolves_to, loopback):
    import socket as socket_module

    def resolve(name, *args, **kwargs):
        if resolves_to is None:
            raise AssertionError("no lookup is needed")
        return [(socket_module.AF_INET, socket_module.SOCK_STREAM, 6, "", (resolves_to, 0))]

    monkeypatch.setattr(socket_module, "getaddrinfo", resolve)
    assert otel._is_loopback_host(host) is loopback


@pytest.fixture
def fresh_warnings(monkeypatch):
    warned = getattr(otel, "_PLAIN_TEXT_WARNED", None)
    if isinstance(warned, set):
        warned.clear()
    yield
    if isinstance(warned, set):
        warned.clear()


@pytest.mark.parametrize("base", ["http://127.0.0.1:4318", "http://localhost:4318", "http://[::1]:4318", "https://self-hosted.example"])
def test_no_plain_text_warning_for_loopback_or_https(monkeypatch, caplog, fresh_warnings, base):
    monkeypatch.setenv("FI_BASE_URL", base)
    with caplog.at_level(logging.WARNING, logger=otel.logger.name):
        otel.HTTPSpanExporter(timeout=2).shutdown()
    assert not [r for r in caplog.records if "FI_ALLOW_INSECURE_HTTP" in r.getMessage()], caplog.text


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
    probe = otel.GRPCSpanExporter(timeout=2)
    retains_channel = hasattr(probe, "_channel")
    probe.shutdown()
    opened.clear()
    closed.clear()
    real_check = otel._check_future_agi_endpoint

    def check(endpoint, is_grpc, caller_headers=None):
        if is_grpc and opened:  # the exporter has been built: this is the check on what it resolved
            raise otel.UnsupportedFutureAGIEndpointError("refused")
        return real_check(endpoint, is_grpc, caller_headers)

    monkeypatch.setattr(otel, "_check_future_agi_endpoint", check)
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        otel.GRPCSpanExporter(timeout=2)
    if not retains_channel:
        pytest.skip("this OpenTelemetry release does not keep the channel on the exporter, so none can be closed")
    assert opened and closed
