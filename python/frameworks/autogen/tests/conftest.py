"""Use real OpenTelemetry/FITracer and fail any non-loopback network access."""

import ipaddress
import socket
import sys

# The upstream legacy SDK cannot be installed on 3.13; AgentChat still can.
collect_ignore = ["test_sdk_legacy.py"] if sys.version_info >= (3, 13) else []

import pytest


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def check(host):
        if host in (None, "", "localhost"):
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
        raise AssertionError(f"AutoGen tests cannot access external host {host!r}")

    for method in ("connect", "connect_ex"):
        original = getattr(socket.socket, method)
        def guarded(sock, address, *args, _original=original, **kwargs):
            if isinstance(address, tuple):
                check(address[0])
            return _original(sock, address, *args, **kwargs)
        monkeypatch.setattr(socket.socket, method, guarded)
    original_sendto = socket.socket.sendto
    def sendto(sock, *args, **kwargs):
        address = args[-1]
        if isinstance(address, tuple):
            check(address[0])
        return original_sendto(sock, *args, **kwargs)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    original_dns = socket.getaddrinfo
    def dns(host, *args, **kwargs):
        check(host)
        return original_dns(host, *args, **kwargs)
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FI_GRPC_URL", "http://127.0.0.1:9")
    yield
    # Tests reload this module to exercise discovery. Restore the latest
    # instrumentor before the next test instead of leaving real SDK hooks behind.
    import traceai_autogen
    instrumentor = traceai_autogen.AutogenInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
