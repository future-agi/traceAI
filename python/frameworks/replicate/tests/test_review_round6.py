"""TH-8320 review round 6 regressions.

The opt-in path pre-imports AnyIO on the caller's thread before starting its
workers (an import lock held across fork otherwise hangs the child); detects
socket-level HTTP mocks; refuses custom network backends / per-instance
transport resolvers; cancels reads as soon as their spans are ended; and
coordinates the loop as well as the finisher around fork. Default-path
regressions cover ``data:`` text chunks, an iterator first consumed in a
forked child, and an iterator still open at provider shutdown.
"""
from __future__ import annotations

import json
import os
import threading
import warnings

import httpcore
import httpx
import pytest

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

import replicate  # noqa: E402
from mocket import Mocket, Mocketizer  # noqa: E402
from mocket.mockhttp import Entry, Response  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402
from traceai_replicate import _attributes, _stream_read
from _support import API_TOKEN, STREAM_MODEL, FakeReplicate, attrs, default_client, instrumented

READ_ON = {"stream_reload_seconds": 2.0}


def _fake_prediction(base: str, status: str = "starting", output=None, metrics=None, logs=""):
    return {
        "id": "p1",
        "model": "acme/m",
        "version": "v1",
        "status": status,
        "input": {},
        "output": output,
        "logs": logs,
        "error": None,
        "metrics": metrics or {},
        "created_at": "2026-10-21T00:00:00Z",
        "urls": {
            "get": base + "/v1/predictions/p1",
            "cancel": base + "/v1/predictions/p1/cancel",
            "stream": base + "/stream/p1",
        },
    }


def test_a_data_uri_text_chunk_is_preserved_verbatim():
    collector = _attributes.OutputCollector(count_files=True)
    for chunk in ("prefix ", "data:x,y", " suffix"):
        collector.add(chunk)
    values = collector.attributes()
    assert values["output.value"] == "prefix data:x,y suffix"
    assert values["replicate.output.type"] == "text"
    assert "replicate.stream.file_count" not in values


def test_socket_level_mock_does_not_consume_the_apps_scripted_prediction_response():
    base = "https://replicate.invalid"
    started = _fake_prediction(base)
    final_app = _fake_prediction(
        base,
        status="succeeded",
        output=["APP-SCRIPTED-1"],
        metrics={"predict_time": 0.1, "input_token_count": 3, "output_token_count": 1},
        logs="APP-SCRIPTED-RESPONSE",
    )
    sse = "event: output\nid: 1\ndata: hello\n\nevent: done\nid: 2\ndata: {}\n\n"
    with instrumented(**READ_ON) as traced:
        with Mocketizer(strict_mode=True):
            Entry.single_register(
                Entry.POST,
                base + "/v1/models/acme/m/predictions",
                body=json.dumps(started),
                status=201,
                headers={"content-type": "application/json"},
            )
            Entry.single_register(
                Entry.GET,
                base + "/stream/p1",
                body=sse,
                status=200,
                headers={"content-type": "text/event-stream"},
            )
            Entry.register(
                Entry.GET,
                base + "/v1/predictions/p1",
                Response(body=json.dumps(final_app), headers={"content-type": "application/json"}),
                Response(body=json.dumps(final_app), headers={"content-type": "application/json"}),
            )
            client = replicate.Client(api_token=API_TOKEN, base_url=base)
            events = list(client.stream("acme/m", input={}))
            span = traced.one()
            app_read = client.predictions.get("p1")
            requests = Mocket.request_list()

    assert [getattr(event.event, "value", event.event) for event in events] == ["output", "done"]
    assert app_read.logs == "APP-SCRIPTED-RESPONSE"
    assert [(request.method, request.path) for request in requests] == [
        ("POST", "/v1/models/acme/m/predictions"),
        ("GET", "/stream/p1"),
        ("GET", "/v1/predictions/p1"),
    ]
    values = attrs(span)
    assert span.status.status_code is StatusCode.OK
    assert values["replicate.stream.read"] == "skipped"
    assert values["replicate.stream.skip_reason"] == "patched_socket"
    assert "replicate.prediction.status" not in values


@pytest.mark.parametrize("name", ["handle_request", "handle_async_request"])
def test_httpcore_http11_connection_class_patch_is_detected(name, monkeypatch):
    owner = getattr(httpcore, "HTTP11Connection" if name == "handle_request" else "AsyncHTTP11Connection")
    original = owner.__dict__[name]
    monkeypatch.setattr(owner, name, lambda self, request: original(self, request))
    assert _stream_read.send_path_patched()


def test_a_custom_httpcore_network_backend_is_not_replaced_by_the_read():
    client = replicate.Client(api_token=API_TOKEN)
    http = client._client
    transport = http._transport._wrapped_transport
    transport._pool._network_backend = object()
    url = _stream_read._read_url(http, "p1")
    assert _stream_read._read_routing(client, http, url) is None


def test_an_instance_transport_resolver_is_not_called_for_the_read():
    client = replicate.Client(api_token=API_TOKEN)
    http = client._client
    called = []
    http._transport_for_url = lambda url: called.append(str(url)) or http._transport
    url = _stream_read._read_url(http, "p1")
    assert _stream_read._read_routing(client, http, url) is None
    assert called == []


def test_anyio_async_backend_is_imported_by_the_instrumenting_thread(monkeypatch):
    original = _stream_read.importlib.import_module
    seen = []

    def record(name, *args, **kwargs):
        if name == "anyio._backends._asyncio":
            seen.append(threading.current_thread().name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(_stream_read.importlib, "import_module", record)
    with instrumented(**READ_ON):
        assert seen
        assert seen[0] == threading.current_thread().name
        module = __import__("sys").modules["anyio._backends._asyncio"]
        assert not module.__spec__._initializing


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_a_stream_created_in_the_parent_but_first_iterated_in_the_child_starts_there():
    with FakeReplicate() as fake, instrumented() as traced:
        stream = default_client(fake).stream(STREAM_MODEL, input={})
        read_fd, write_fd = os.pipe()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()
        if pid == 0:  # pragma: no cover - child
            os.close(read_fd)
            try:
                events = list(stream)
                spans = [span for span in traced.replicate_spans() if span.name == "replicate.stream"]
                os.write(write_fd, b"ok:" + str(len(events)).encode() + b":" + str(len(spans)).encode())
                os._exit(0)
            except BaseException as error:
                os.write(write_fd, ("error:" + repr(error)).encode())
                os._exit(1)
        os.close(write_fd)
        child_result = os.read(read_fd, 128).decode()
        os.close(read_fd)
        _, status = os.waitpid(pid, 0)
        parent_spans = traced.replicate_spans()

    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, child_result
    assert child_result.startswith("ok:")
    assert child_result.endswith(":1")  # the child's first iteration starts and exports one span
    assert parent_spans == []


def test_provider_shutdown_ends_a_referenced_stream_still_open():
    with FakeReplicate() as fake, instrumented() as traced:
        stream = default_client(fake).stream(STREAM_MODEL, input={})
        next(stream)
        assert traced.replicate_spans() == []
        traced.provider.shutdown()
        span = traced.one()
        assert stream is not None  # keep the vendor iterator referenced through shutdown

    assert span.status.status_code is StatusCode.ERROR
    assert attrs(span)["replicate.cancelled"] is True
    assert "output.value" not in attrs(span)


@pytest.mark.parametrize("action", ["shutdown", "uninstrument"])
def test_ending_a_span_cancels_a_read_in_flight_and_closes_its_connection(action):
    with FakeReplicate() as fake, instrumented(stream_reload_seconds=10) as traced:
        fake.read_trickle = 10
        list(default_client(fake).stream(STREAM_MODEL, input={}))
        import time

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not any(
            path.startswith("/v1/predictions/") for path in fake.paths("GET")
        ):
            time.sleep(0.01)
        assert any(path.startswith("/v1/predictions/") for path in fake.paths("GET"))
        assert _stream_read.live_reads() == 1

        if action == "shutdown":
            traced.provider.shutdown()
        else:
            traced.instrumentor.uninstrument()
        assert _stream_read.pending_spans() == 0
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and _stream_read.live_reads():
            time.sleep(0.01)
        assert _stream_read.live_reads() == 0
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and not fake.read_disconnected:
            time.sleep(0.01)
        assert fake.read_disconnected
        expected = "shutdown" if action == "shutdown" else "uninstrumented"
        assert traced.one().attributes["replicate.stream.read"] == expected


def test_default_socket_layer_is_not_mistaken_for_a_mock():
    assert not _stream_read.socket_layer_patched()
    assert _stream_read.http_patched() is None
