"""Offline contract helpers. Linux CI also isolates these tests in a network namespace."""
import ipaddress
import socket

import pytest

_REPORTS = []


def pytest_runtest_logreport(report):
    _REPORTS.append(report)


def pytest_sessionfinish(session, exitstatus):
    if not any(r.when == 'call' for r in _REPORTS) or any(r.skipped for r in _REPORTS):
        session.exitstatus = 1


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    original = socket.socket.connect

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            host = address[0]
            assert host == 'localhost' or ipaddress.ip_address(host).is_loopback, 'external network forbidden'
        return original(sock, address)

    monkeypatch.setattr(socket.socket, 'connect', connect)


@pytest.fixture
def tracing():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider, exporter
    provider.shutdown()
