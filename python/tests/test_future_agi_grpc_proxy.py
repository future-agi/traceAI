"""A plain gRPC channel this package builds reads no proxy: measured against a proxy sink and a target listener."""
import pytest

pytest.importorskip("grpc")
pytest.importorskip("opentelemetry.exporter.otlp.proto.grpc.trace_exporter")

import oracle_conformance as oc  # noqa: E402
from fi_instrumentation import otel  # noqa: E402

OPT_IN = "FI_ALLOW_INSECURE_HTTP"
SPELLINGS = ("grpc_proxy", "https_proxy", "http_proxy", "GRPC_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "all_proxy")


@pytest.fixture(scope="module")
def listeners():
    found = oc.Listeners()
    yield found
    found.close()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in oc.PROXY_NAMES + oc.OWN_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FI_API_KEY", oc.MARKER_KEY)
    monkeypatch.setenv("FI_SECRET_KEY", oc.MARKER_SECRET)
    otel._PLAIN_TEXT_WARNED.clear()
    yield
    otel._PLAIN_TEXT_WARNED.clear()


def _send(listeners, monkeypatch, target, **kwargs):
    """Build the exporter for the target ``FI_GRPC_URL`` names, on a plain channel and send one export, with the real channel opened to the target
    listener. Returns what the sinks measured."""
    listeners.reset()
    monkeypatch.setenv("FI_GRPC_URL", target)
    recorder = oc.Recorder(listeners)
    with recorder.patched(), listeners.shim():
        exporter = otel.GRPCSpanExporter(timeout=1, **kwargs)
        try:
            oc.send_grpc(exporter)
        finally:
            exporter.shutdown()
    measured = listeners.measure()
    measured["channel"] = recorder.channels[-1] if recorder.channels else None
    return measured


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_a_proxy_variable_does_not_route_the_channel_and_the_keys_reach_the_target(spelling, listeners, monkeypatch):
    monkeypatch.setenv(OPT_IN, "1")
    monkeypatch.setenv(spelling, f"http://127.0.0.1:{listeners.a.port}")
    measured = _send(listeners, monkeypatch, "http://collector.internal:50051")
    assert measured["dialed"] == "target"
    assert listeners.a.connections == 0 and listeners.a.marker_bytes == 0
    assert measured["marker_bytes"] > 0
    assert dict(measured["channel"][2])["grpc.enable_http_proxy"] == 0


def test_a_bypass_list_does_not_change_the_route_of_a_plain_channel(listeners, monkeypatch):
    monkeypatch.setenv(OPT_IN, "1")
    monkeypatch.setenv("grpc_proxy", f"http://127.0.0.1:{listeners.a.port}")
    monkeypatch.setenv("no_proxy", "other.example")
    monkeypatch.setenv("no_grpc_proxy", "other.example")
    measured = _send(listeners, monkeypatch, "collector.internal:50051", insecure=True)
    assert measured["dialed"] == "target" and listeners.a.connections == 0


def test_the_channel_options_the_caller_passes_are_kept_and_its_proxy_settings_are_replaced(monkeypatch):
    options = otel._options_without_proxy((("grpc.max_send_message_length", 1000), ("grpc.enable_http_proxy", 1),
                                           ["grpc.http_proxy", "http://proxy.internal:3128"]))
    assert options == (("grpc.max_send_message_length", 1000), ("grpc.enable_http_proxy", 0))
    assert otel._options_without_proxy(None) == (("grpc.enable_http_proxy", 0),)


def test_a_channel_with_tls_is_given_no_proxy_option(listeners, monkeypatch):
    monkeypatch.setenv("grpc_proxy", "http://proxy.internal:3128")
    measured = _send(listeners, monkeypatch, "https://collector.internal:50051")
    assert measured["channel"][0] == "tls"
    assert "grpc.enable_http_proxy" not in dict(measured["channel"][2] or ())


def test_an_exporter_release_without_channel_options_gets_a_plain_channel_with_the_proxy_off(listeners, monkeypatch):
    """The constructors of the releases before 1.35 take no channel options: the client is rebuilt on a channel that has it."""
    import inspect

    monkeypatch.setenv(OPT_IN, "1")
    monkeypatch.setenv("grpc_proxy", f"http://127.0.0.1:{listeners.a.port}")
    real = otel._get_class_signature

    def without_channel_options(fn, kind=None):
        signature = real(fn, kind)
        return inspect.Signature([p for p in signature.parameters.values() if p.name != "channel_options"])

    monkeypatch.setattr(otel, "_get_class_signature", without_channel_options)
    measured = _send(listeners, monkeypatch, "collector.internal:50051", insecure=True)
    assert measured["dialed"] == "target" and listeners.a.connections == 0
    assert measured["marker_bytes"] > 0
