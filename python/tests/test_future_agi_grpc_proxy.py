"""A plain gRPC channel is routed by what grpcio reads, not by the rule for HTTP: the measured rule, row by row."""
import json
import os
import subprocess
import sys

import pytest

pytest.importorskip("grpc")
pytest.importorskip("opentelemetry.exporter.otlp.proto.grpc.trace_exporter")

from fi_instrumentation import otel  # noqa: E402

OPT_IN = "FI_ALLOW_INSECURE_HTTP"
PROXY_NAMES = ("grpc_proxy", "GRPC_PROXY", "https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY",
               "no_proxy", "NO_PROXY", "no_grpc_proxy", "NO_GRPC_PROXY")
CHILD = r'''
import grpc, json, os, socket, sys, threading, time
sink = socket.socket(); sink.bind(("127.0.0.1", 0)); sink.listen(4); port = sink.getsockname()[1]
seen = []
def serve():
    while True:
        try: conn, _ = sink.accept()
        except OSError: return
        conn.settimeout(1)
        try: seen.append(conn.recv(4096).split(b"\r\n")[0].decode("latin1"))
        except OSError: pass
        try: conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        except OSError: pass
        conn.close()
threading.Thread(target=serve, daemon=True).start()
spec = json.loads(sys.argv[1])
for name, value in spec["env"].items():
    os.environ[name] = value.replace("{port}", str(port))
channel = grpc.insecure_channel(spec["target"], options=[("grpc.enable_retries", 0)])
try: grpc.channel_ready_future(channel).result(timeout=2)
except Exception: pass
time.sleep(0.3)
print(json.dumps(bool(seen)), flush=True)
os._exit(0)
'''
PROXY = "http://127.0.0.1:{port}"
TARGET = "collector.internal:50051"
IP_TARGET = "10.255.255.1:50051"

# (id, environment, target, routed through the proxy by grpcio)
ROWS = [
    ("grpc_proxy", {"grpc_proxy": PROXY}, TARGET, True),
    ("GRPC_PROXY upper case is ignored", {"GRPC_PROXY": PROXY}, TARGET, False),
    ("https_proxy routes a plain channel", {"https_proxy": PROXY}, TARGET, True),
    ("HTTPS_PROXY upper case is ignored", {"HTTPS_PROXY": PROXY}, TARGET, False),
    ("http_proxy", {"http_proxy": PROXY}, TARGET, True),
    ("HTTP_PROXY upper case is ignored", {"HTTP_PROXY": PROXY}, TARGET, False),
    ("ALL_PROXY is ignored", {"ALL_PROXY": PROXY}, TARGET, False),
    ("all_proxy is ignored", {"all_proxy": PROXY}, TARGET, False),
    ("empty https_proxy shadows http_proxy", {"https_proxy": "", "http_proxy": PROXY}, TARGET, False),
    ("empty grpc_proxy shadows https_proxy", {"grpc_proxy": "", "https_proxy": PROXY}, TARGET, False),
    ("no_proxy exact host", {"grpc_proxy": PROXY, "no_proxy": "collector.internal"}, TARGET, False),
    ("NO_PROXY upper case is ignored", {"grpc_proxy": PROXY, "NO_PROXY": "collector.internal"}, TARGET, True),
    ("no_proxy suffix", {"grpc_proxy": PROXY, "no_proxy": ".internal"}, TARGET, False),
    ("no_proxy bare suffix label", {"grpc_proxy": PROXY, "no_proxy": "internal"}, TARGET, False),
    ("no_proxy host:port does not bypass", {"grpc_proxy": PROXY, "no_proxy": "collector.internal:50051"}, TARGET, True),
    ("no_proxy star does not bypass", {"grpc_proxy": PROXY, "no_proxy": "*"}, TARGET, True),
    ("no_proxy list", {"grpc_proxy": PROXY, "no_proxy": "a.example, collector.internal"}, TARGET, False),
    ("no_proxy is case-insensitive", {"grpc_proxy": PROXY, "no_proxy": "COLLECTOR.INTERNAL"}, TARGET, False),
    ("no_grpc_proxy bypasses", {"grpc_proxy": PROXY, "no_grpc_proxy": "collector.internal"}, TARGET, False),
    ("no_grpc_proxy takes precedence over no_proxy", {"grpc_proxy": PROXY, "no_grpc_proxy": "other.example", "no_proxy": "collector.internal"}, TARGET, True),
    ("empty no_grpc_proxy shadows no_proxy", {"grpc_proxy": PROXY, "no_grpc_proxy": "", "no_proxy": "collector.internal"}, TARGET, True),
    ("no_proxy network range bypasses an address", {"grpc_proxy": PROXY, "no_proxy": "10.255.255.0/24"}, IP_TARGET, False),
    ("no_proxy network range that does not contain the address", {"grpc_proxy": PROXY, "no_proxy": "10.0.0.0/16"}, IP_TARGET, True),
    ("no_proxy network range does not bypass a name", {"grpc_proxy": PROXY, "no_proxy": "10.0.0.0/8"}, TARGET, True),
    ("dns target takes the same rule", {"grpc_proxy": PROXY, "no_proxy": "collector.internal"}, "dns:///collector.internal:50051", False),
]


def _grpc_routes(env, target):
    spec = {"env": env, "target": target}
    clean = {k: v for k, v in os.environ.items() if k not in PROXY_NAMES}
    out = subprocess.run([sys.executable, "-c", CHILD, json.dumps(spec)], capture_output=True, text=True, timeout=60, env=clean).stdout.strip().splitlines()
    return json.loads(out[-1])


@pytest.mark.parametrize("row", ROWS, ids=[row[0] for row in ROWS])
def test_grpcio_routes_a_plain_channel_the_way_the_rule_says(row, monkeypatch):
    _, env, target, routed = row
    assert _grpc_routes(env, target) is routed, "grpcio does not behave as the measured table says"
    for name in PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value.replace("{port}", "3128"))
    host, _, port = target.removeprefix("dns:///").rpartition(":")
    assert otel._proxy_applies(host, int(port), grpc=True) is routed


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in PROXY_NAMES + (OPT_IN, "OTEL_EXPORTER_OTLP_INSECURE", "OTEL_EXPORTER_OTLP_TRACES_INSECURE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FI_API_KEY", "synthetic-env-key")
    monkeypatch.setenv("FI_SECRET_KEY", "synthetic-env-secret")
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("FI_GRPC_URL", "http://collector.internal:50051")
    otel._PLAIN_TEXT_WARNED.clear()
    yield
    otel._PLAIN_TEXT_WARNED.clear()


def _build():
    otel.GRPCSpanExporter(timeout=2).shutdown()


@pytest.mark.parametrize("name", ["grpc_proxy", "https_proxy", "http_proxy"])
def test_a_variable_grpcio_reads_refuses_the_opt_in(name, monkeypatch):
    monkeypatch.setenv(OPT_IN, "1")
    monkeypatch.setenv(name, "http://proxy.internal:3128")
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        _build()


@pytest.mark.parametrize("name", ["GRPC_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "all_proxy"])
def test_a_variable_grpcio_does_not_read_leaves_the_opt_in(name, monkeypatch):
    monkeypatch.setenv(OPT_IN, "1")
    monkeypatch.setenv(name, "http://proxy.internal:3128")
    _build()


def test_no_proxy_in_lower_case_bypasses_and_upper_case_does_not(monkeypatch):
    monkeypatch.setenv(OPT_IN, "1")
    monkeypatch.setenv("grpc_proxy", "http://proxy.internal:3128")
    monkeypatch.setenv("NO_PROXY", "collector.internal")
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        _build()
    monkeypatch.setenv("no_proxy", "collector.internal")
    _build()


def test_no_grpc_proxy_takes_precedence_over_no_proxy(monkeypatch):
    monkeypatch.setenv(OPT_IN, "1")
    monkeypatch.setenv("grpc_proxy", "http://proxy.internal:3128")
    monkeypatch.setenv("no_proxy", "collector.internal")
    monkeypatch.setenv("no_grpc_proxy", "other.example")
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        _build()


def test_the_http_rule_is_unchanged(monkeypatch):
    from fi_instrumentation import settings

    monkeypatch.setenv("FI_BASE_URL", "http://collector.internal:8080")
    monkeypatch.setenv(OPT_IN, "1")
    monkeypatch.setenv("https_proxy", "http://proxy.internal:3128")
    otel.HTTPSpanExporter(timeout=2).shutdown()
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.internal:3128")
    with pytest.raises(otel.UnsupportedFutureAGIEndpointError):
        otel.HTTPSpanExporter(timeout=2)
    assert settings is not None
