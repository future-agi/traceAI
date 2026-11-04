"""Endpoint binding regressions; no real credentials or external collector calls."""
import inspect
from unittest.mock import patch

import pytest
from fi_instrumentation import otel


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
    capture.__signature__ = init_signature
    with patch.object(base, "__init__", capture):
        cls(headers={"X-Custom": "value"})
    assert captured["endpoint"] == ("https://grpc.futureagi.com" if transport == "grpc" else "https://api.futureagi.com/tracer/v1/traces")
    assert {k.lower(): v for k, v in captured["headers"].items()} == {"x-api-key": "synthetic-key", "x-secret-key": "synthetic-secret", "x-custom": "value"}


def test_positional_http_endpoint_and_headers_with_other_options(monkeypatch):
    monkeypatch.setattr(otel, "get_env_fi_auth_header", lambda: None)
    captured = {}
    signature = inspect.signature(otel._HTTPSpanExporter)
    init_signature = inspect.signature(otel._HTTPSpanExporter.__init__)
    def capture(self, *args, **kwargs):
        captured.update(signature.bind_partial(*args, **kwargs).arguments)
    capture.__signature__ = init_signature
    with patch.object(otel._HTTPSpanExporter, "__init__", capture):
        otel.HTTPSpanExporter("http://local.invalid/path", "ca.pem", "key.pem", "cert.pem", {"Authorization": "synthetic"}, 4)
    assert captured["endpoint"] == "http://local.invalid/path"
    assert captured["headers"] == {"authorization": "synthetic"}
    assert (captured["certificate_file"], captured["client_key_file"], captured["client_certificate_file"], captured["timeout"]) == ("ca.pem", "key.pem", "cert.pem", 4)


def test_grpc_transport_options_and_positional_headers(monkeypatch):
    monkeypatch.setattr(otel, "get_env_fi_auth_header", lambda: None)
    captured = {}
    signature = inspect.signature(otel._GRPCSpanExporter)
    init_signature = inspect.signature(otel._GRPCSpanExporter.__init__)
    def capture(self, *args, **kwargs):
        captured.update(signature.bind_partial(*args, **kwargs).arguments)
    capture.__signature__ = init_signature
    sentinel = object()
    opts = {"channel_options": (("grpc.test", "value"),)} if "channel_options" in inspect.signature(otel._GRPCSpanExporter).parameters else {}
    with patch.object(otel._GRPCSpanExporter, "__init__", capture):
        otel.GRPCSpanExporter("http://local.invalid:4317", True, sentinel, [("Authorization", "synthetic")], 7, **opts)
    assert captured["endpoint"] == "http://local.invalid:4317"
    assert captured["insecure"] is True and captured["credentials"] is sentinel
    assert captured["headers"] == {"authorization": "synthetic"} and captured["timeout"] == 7
    for key, value in opts.items():
        assert captured[key] == value


def test_binding_errors_are_preserved():
    with pytest.raises(TypeError):
        otel.GRPCSpanExporter("localhost:1", endpoint="localhost:2")
    with pytest.raises(TypeError):
        otel.HTTPSpanExporter("http://localhost:1", endpoint="http://localhost:2")
