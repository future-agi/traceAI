"""Endpoint binding regressions; no real credentials or external collector calls."""
import inspect
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fi_instrumentation import otel


def _emulate_parent(exporter, captured):
    """Set what the real OpenTelemetry parent sets: the resolved endpoint and a header layout.

    The tests stub the parent's __init__ to capture its arguments; the subclass then checks
    the resolved endpoint and pins headers, so the stub mirrors the parent's attributes.
    """
    given = captured.get("headers") or {}
    headers = {str(k).lower(): v for k, v in (given.items() if hasattr(given, "items") else given)}
    exporter._endpoint = captured.get("endpoint")
    exporter._client = SimpleNamespace(_headers=dict(headers))
    exporter._headers = tuple(headers.items()) or None


def _sent_headers(exporter, transport):
    """The headers the exporter will send, apart from the OTLP defaults; None when there are none."""
    if transport == "grpc":
        return dict(exporter._headers) if exporter._headers else None
    try:
        from opentelemetry.exporter.otlp.proto.http import _OTLP_HTTP_HEADERS
    except ImportError:  # pragma: no cover
        _OTLP_HTTP_HEADERS = {}
    defaults = {k.lower() for k in _OTLP_HTTP_HEADERS}
    sent = {k: v for k, v in exporter._client._headers.items() if k.lower() not in defaults}
    return sent or None


@pytest.mark.parametrize("transport", ["grpc", "http"])
@pytest.mark.parametrize("form", ["positional", "keyword", "empty", "none", "omitted"])
def test_endpoint_precedence_and_headers(transport, form, monkeypatch):
    monkeypatch.setenv("FI_GRPC_URL", "http://env.invalid:4317")
    monkeypatch.setenv("FI_BASE_URL", "https://env.invalid")
    monkeypatch.setattr(otel, "get_env_fi_auth_header", lambda: {"X-Api-Key": "synthetic-key", "X-Secret-Key": "synthetic-secret"})
    cls, base = (otel.GRPCSpanExporter, otel._GRPCSpanExporter) if transport == "grpc" else (otel.HTTPSpanExporter, otel._HTTPSpanExporter)
    explicit = "http://explicit.invalid:4317/traces"
    endpoint = explicit if form in ("positional", "keyword") else ("" if form == "empty" else None)
    kwargs = {"headers": {"Authorization": "Bearer synthetic", "X-Custom": "value"}}
    args = (endpoint,) if form == "positional" else ()
    if form in ("keyword", "empty", "none"):
        kwargs["endpoint"] = endpoint
    captured = {}
    signature = inspect.signature(base)
    init_signature = inspect.signature(base.__init__)
    def capture(self, *args, **kwargs):
        captured.update(signature.bind_partial(*args, **kwargs).arguments)
        _emulate_parent(self, captured)
    capture.__signature__ = init_signature
    with patch.object(base, "__init__", capture):
        cls(*args, **kwargs)
    expected = endpoint if endpoint is not None else ("http://env.invalid:4317" if transport == "grpc" else "https://env.invalid/tracer/v1/traces")
    assert captured["endpoint"] == expected
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers == {"authorization": "Bearer synthetic", "x-custom": "value"}


@pytest.mark.parametrize("transport", ["grpc", "http"])
def test_default_endpoint_and_fi_headers(transport, monkeypatch):
    monkeypatch.delenv("FI_GRPC_URL", raising=False)
    monkeypatch.delenv("FI_BASE_URL", raising=False)
    monkeypatch.setattr(otel, "get_env_fi_auth_header", lambda: {"X-Api-Key": "synthetic-key", "X-Secret-Key": "synthetic-secret"})
    cls, base = (otel.GRPCSpanExporter, otel._GRPCSpanExporter) if transport == "grpc" else (otel.HTTPSpanExporter, otel._HTTPSpanExporter)
    captured = {}
    signature = inspect.signature(base)
    init_signature = inspect.signature(base.__init__)
    def capture(self, *args, **kwargs):
        captured.update(signature.bind_partial(*args, **kwargs).arguments)
        _emulate_parent(self, captured)
    capture.__signature__ = init_signature
    with patch.object(base, "__init__", capture):
        exporter = cls(headers={"X-Custom": "value"})
    assert captured["endpoint"] == ("https://grpc.futureagi.com" if transport == "grpc" else "https://api.futureagi.com/tracer/v1/traces")
    assert _sent_headers(exporter, transport) == {"x-api-key": "synthetic-key", "x-secret-key": "synthetic-secret", "x-custom": "value"}


def test_positional_http_endpoint_and_headers_with_other_options(monkeypatch):
    monkeypatch.setattr(otel, "get_env_fi_auth_header", lambda: None)
    captured = {}
    signature = inspect.signature(otel._HTTPSpanExporter)
    init_signature = inspect.signature(otel._HTTPSpanExporter.__init__)
    def capture(self, *args, **kwargs):
        captured.update(signature.bind_partial(*args, **kwargs).arguments)
        _emulate_parent(self, captured)
    capture.__signature__ = init_signature
    with patch.object(otel._HTTPSpanExporter, "__init__", capture):
        otel.HTTPSpanExporter("http://local.invalid/path", "ca.pem", "key.pem", "cert.pem", {"Authorization": "synthetic"}, 4)
    assert captured["endpoint"] == "http://local.invalid/path"
    assert {k.lower(): v for k, v in captured["headers"].items()} == {"authorization": "synthetic"}
    assert (captured["certificate_file"], captured["client_key_file"], captured["client_certificate_file"], captured["timeout"]) == ("ca.pem", "key.pem", "cert.pem", 4)


def test_grpc_transport_options_and_positional_headers(monkeypatch):
    monkeypatch.setattr(otel, "get_env_fi_auth_header", lambda: None)
    captured = {}
    signature = inspect.signature(otel._GRPCSpanExporter)
    init_signature = inspect.signature(otel._GRPCSpanExporter.__init__)
    def capture(self, *args, **kwargs):
        captured.update(signature.bind_partial(*args, **kwargs).arguments)
        _emulate_parent(self, captured)
    capture.__signature__ = init_signature
    sentinel = object()
    opts = {"channel_options": (("grpc.test", "value"),)} if "channel_options" in inspect.signature(otel._GRPCSpanExporter).parameters else {}
    with patch.object(otel._GRPCSpanExporter, "__init__", capture):
        otel.GRPCSpanExporter("http://local.invalid:4317", True, sentinel, [("Authorization", "synthetic")], 7, **opts)
    assert captured["endpoint"] == "http://local.invalid:4317"
    assert captured["insecure"] is True and captured["credentials"] is sentinel
    assert {k.lower(): v for k, v in dict(captured["headers"]).items()} == {"authorization": "synthetic"} and captured["timeout"] == 7
    for key, value in opts.items():
        assert captured[key] == value


def test_binding_errors_are_preserved():
    with pytest.raises(TypeError):
        otel.GRPCSpanExporter("localhost:1", endpoint="localhost:2")
    with pytest.raises(TypeError):
        otel.HTTPSpanExporter("http://localhost:1", endpoint="http://localhost:2")


def _captured_headers(transport, monkeypatch, env, headers):
    monkeypatch.setattr(otel, "get_env_fi_auth_header", lambda: env)
    # These cases are about credentials sent to Future AGI, so the endpoint is the Future AGI one.
    monkeypatch.setenv("FI_BASE_URL", "http://local.invalid:4317")
    monkeypatch.setenv("FI_GRPC_URL", "http://local.invalid:4317")
    cls, base = (otel.GRPCSpanExporter, otel._GRPCSpanExporter) if transport == "grpc" else (otel.HTTPSpanExporter, otel._HTTPSpanExporter)
    captured = {}
    signature = inspect.signature(base)
    init_signature = inspect.signature(base.__init__)
    def capture(self, *args, **kwargs):
        captured.update(signature.bind_partial(*args, **kwargs).arguments)
        _emulate_parent(self, captured)
    capture.__signature__ = init_signature
    with patch.object(base, "__init__", capture):
        exporter = cls(endpoint="http://local.invalid:4317", headers=headers)
    return _sent_headers(exporter, transport)


ENV_PAIR = {"X-Api-Key": "synthetic-env-key", "X-Secret-Key": "synthetic-env-secret"}


@pytest.mark.parametrize("transport", ["grpc", "http"])
@pytest.mark.parametrize("empty", ["", "   ", None])
def test_empty_authorization_does_not_hide_env_credentials(transport, empty, monkeypatch):
    """Issue #155: an empty Authorization header is not a credential."""
    headers = _captured_headers(transport, monkeypatch, ENV_PAIR, {"Authorization": empty, "X-Custom": "value"})
    assert headers == {"x-api-key": "synthetic-env-key", "x-secret-key": "synthetic-env-secret", "x-custom": "value"}


@pytest.mark.parametrize("transport", ["grpc", "http"])
def test_explicit_fi_keys_win_without_case_variant_duplicates(transport, monkeypatch):
    """Issue #155: explicit X-Api-Key/X-Secret-Key are sent once and never mixed with the env pair."""
    headers = _captured_headers(transport, monkeypatch, ENV_PAIR, {"x-api-key": "synthetic-explicit-key", "X-Secret-Key": "synthetic-explicit-secret"})
    assert headers == {"x-api-key": "synthetic-explicit-key", "x-secret-key": "synthetic-explicit-secret"}
    assert len({k.lower() for k in headers}) == len(headers)


@pytest.mark.parametrize("transport", ["grpc", "http"])
def test_env_pair_is_lower_cased_and_added_once(transport, monkeypatch):
    headers = _captured_headers(transport, monkeypatch, ENV_PAIR, None)
    assert headers == {"x-api-key": "synthetic-env-key", "x-secret-key": "synthetic-env-secret"}


@pytest.mark.parametrize("transport", ["grpc", "http"])
def test_no_credentials_anywhere_sends_no_headers(transport, monkeypatch):
    assert _captured_headers(transport, monkeypatch, None, {"Authorization": ""}) is None


@pytest.mark.parametrize("transport", ["grpc", "http"])
def test_empty_case_variant_does_not_erase_an_explicit_credential(transport, monkeypatch):
    """A later empty duplicate must not drop an earlier non-empty Authorization and pull in env keys."""
    headers = _captured_headers(transport, monkeypatch, ENV_PAIR, {"Authorization": "Bearer synthetic", "authorization": ""})
    assert headers == {"authorization": "Bearer synthetic"}


@pytest.mark.parametrize("transport", ["grpc", "http"])
@pytest.mark.parametrize("empty", [b"", b"   "])
def test_empty_bytes_credential_does_not_hide_env_credentials(transport, empty, monkeypatch):
    headers = _captured_headers(transport, monkeypatch, ENV_PAIR, {"Authorization": empty})
    assert headers == {"x-api-key": "synthetic-env-key", "x-secret-key": "synthetic-env-secret"}


@pytest.mark.parametrize("transport", ["grpc", "http"])
def test_non_empty_bytes_credential_is_kept(transport, monkeypatch):
    headers = _captured_headers(transport, monkeypatch, ENV_PAIR, {"Authorization": b"Bearer synthetic"})
    assert headers == {"authorization": b"Bearer synthetic"}
