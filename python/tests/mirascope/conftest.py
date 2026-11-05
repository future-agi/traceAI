"""Offline-only tests, including the real OTLP encoder and loopback HTTP receiver."""

import socket

import pytest
from opentelemetry import trace
from opentelemetry.util._once import Once


@pytest.fixture(autouse=True)
def isolated_telemetry(monkeypatch):
    import os

    for name in tuple(os.environ):
        if name.startswith(("FI_", "OTEL_", "OPENAI_", "ANTHROPIC_", "GOOGLE_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FI_API_KEY", "synthetic-api-key")
    monkeypatch.setenv("FI_SECRET_KEY", "synthetic-secret-key")
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    original = socket.socket.connect

    def local_only(sock, address):
        if isinstance(address, tuple) and address[0] not in (
            "127.0.0.1",
            "::1",
            "localhost",
        ):
            raise AssertionError("Outbound network denied in offline tests")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)
    yield
    provider = trace.get_tracer_provider()
    if hasattr(provider, "shutdown"):
        provider.shutdown()
