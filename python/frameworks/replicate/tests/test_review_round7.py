"""TH-8320 review round 7.

* A stream (or run iterator) stopped before it completes, by a ``close()``, a
  drop, a cancelled task, an exception, or a provider shutdown, keeps the
  output it had received (subject to ``TraceConfig``) and so keeps span kind
  ``LLM`` for text; the status is as before (ERROR ``cancelled`` with
  ``replicate.cancelled`` for the caller stopping it, ERROR with the error
  otherwise).
* Ctrl-C (``KeyboardInterrupt``) on a sync stream is recorded the same way
  whether it lands inside ``next()`` or in the loop body: as the caller
  stopping the stream (``replicate.cancelled``), not as an exception.
* A fork whose wait for the finisher's export (or for the read loop to
  pause) ran out does not make later forks wait again until that thread has
  moved on.
* ``_read_routing`` never calls a ``_transport_for_url`` that a subclass of
  httpx's client overrides: no read, ``custom_transport``.
* With the opt-in read, many threads sharing one client: see
  ``test_stream_read_bounds`` (the caller's own pool most likely has an
  httpcore race under that load, so that test now gives each thread its own
  client).
"""

from __future__ import annotations

import asyncio
import gc
import os
import threading
import time
import warnings

import httpx
import pytest

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

import replicate  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402
from traceai_replicate import _stream_read  # noqa: E402
from traceai_replicate._wrappers import drain_released  # noqa: E402

from _support import (  # noqa: E402
    API_TOKEN,
    ITERATOR_VERSION,
    READ_ON,
    STREAM_CHUNKS,
    STREAM_ERROR,
    STREAM_ERROR_MODEL,
    STREAM_MODEL,
    STREAM_SLOW_MODEL,
    STREAM_TEXT,
    TEXT_MODEL,
    TEXT_TOKENS,
    FakeReplicate,
    attrs,
    default_client,
    exception_events,
    instrumented,
    make_client,
)

RECEIVED = "".join(STREAM_CHUNKS[:2])  # what the caller had when it stopped


def _summary(span):
    values = attrs(span)
    return {
        "status": span.status.status_code,
        "description": span.status.description,
        "cancelled": values.get("replicate.cancelled"),
        "kind": values.get("gen_ai.span.kind"),
        "output": values.get("output.value"),
        "type": values.get("replicate.output.type"),
        "exceptions": [event.attributes.get("exception.type") for event in exception_events(span)],
        "read": values.get("replicate.stream.read"),
    }


def _stopped_by_the_caller(span, output=RECEIVED):
    summary = _summary(span)
    assert summary["status"] is StatusCode.ERROR
    assert summary["description"] == "cancelled"
    assert summary["cancelled"] is True
    assert summary["exceptions"] == []
    assert summary["output"] == output
    assert summary["type"] == "text"
    assert summary["kind"] == "LLM"
    return summary


def _two_events(stream):
    first, second = next(stream), next(stream)
    assert [str(first), str(second)] == STREAM_CHUNKS[:2]


# -- m1: a stream stopped before done keeps what it received -------------------------------------


def test_a_stream_closed_before_done_keeps_its_text_and_stays_llm():
    with FakeReplicate() as fake, instrumented() as traced:
        stream = default_client(fake).stream(STREAM_MODEL, input={})
        _two_events(stream)
        stream.close()
        span = traced.one()
    _stopped_by_the_caller(span)


def test_a_stream_dropped_before_done_keeps_its_text_and_stays_llm():
    with FakeReplicate() as fake, instrumented() as traced:
        stream = default_client(fake).stream(STREAM_MODEL, input={})
        _two_events(stream)
        del stream
        gc.collect()
        drain_released()
        span = traced.one()
    _stopped_by_the_caller(span)


def test_a_stream_still_open_at_provider_shutdown_keeps_its_text():
    with FakeReplicate() as fake, instrumented() as traced:
        stream = default_client(fake).stream(STREAM_MODEL, input={})
        _two_events(stream)
        traced.provider.shutdown()
        span = traced.one()
        assert stream is not None  # still referenced at shutdown
    _stopped_by_the_caller(span)


def test_a_stream_that_errors_before_done_keeps_its_text_and_stays_llm():
    with FakeReplicate() as fake, instrumented() as traced:
        with pytest.raises(RuntimeError, match=STREAM_ERROR):
            list(default_client(fake).stream(STREAM_ERROR_MODEL, input={}))
        span = traced.one()
    summary = _summary(span)
    assert summary["status"] is StatusCode.ERROR
    assert summary["exceptions"] == ["RuntimeError"]
    assert summary["cancelled"] is None
    assert summary["output"] == STREAM_CHUNKS[0]  # the one output event before the error
    assert summary["kind"] == "LLM"


def test_a_stopped_stream_keeps_hidden_output_hidden():
    with FakeReplicate() as fake, instrumented(config=TraceConfig(hide_outputs=True)) as traced:
        stream = default_client(fake).stream(STREAM_MODEL, input={})
        _two_events(stream)
        stream.close()
        span = traced.one()
        wire = traced.wire()
    summary = _summary(span)
    assert summary["output"] == "__REDACTED__"
    assert summary["kind"] == "LLM"
    assert summary["cancelled"] is True
    for chunk in STREAM_CHUNKS[:2]:
        assert chunk not in wire


def test_an_async_stream_closed_before_done_keeps_its_text():
    async def main(client):
        stream = await client.async_stream(STREAM_MODEL, input={})
        await stream.__anext__()
        await stream.__anext__()
        await stream.aclose()

    with FakeReplicate() as fake, instrumented() as traced:
        asyncio.run(main(default_client(fake)))
        span = traced.one()
    _stopped_by_the_caller(span)


def test_a_cancelled_task_reading_an_async_stream_keeps_its_text():
    async def reader(client):
        stream = await client.async_stream(STREAM_SLOW_MODEL, input={})
        async for _ in stream:
            pass

    async def main(client):
        task = asyncio.ensure_future(reader(client))
        await asyncio.sleep(0.3)  # the first event has arrived; the stand-in now stalls
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with FakeReplicate() as fake, instrumented() as traced:
        asyncio.run(main(make_client(fake)))
        span = traced.one()
    _stopped_by_the_caller(span, output=STREAM_CHUNKS[0])


def test_a_run_iterator_closed_early_keeps_its_text():
    ref = "{0}:{1}".format(TEXT_MODEL, ITERATOR_VERSION)
    with FakeReplicate() as fake, instrumented() as traced:
        iterator = make_client(fake).run(ref, input={}, wait=False)
        first = next(iterator)
        iterator.close()
        span = traced.one()
    assert first == TEXT_TOKENS[0]
    assert span.name == "replicate.run"
    _stopped_by_the_caller(span, output=TEXT_TOKENS[0])


def test_a_completed_stream_is_unchanged():
    with FakeReplicate() as fake, instrumented() as traced:
        list(default_client(fake).stream(STREAM_MODEL, input={}))
        span = traced.one()
    summary = _summary(span)
    assert summary["status"] is StatusCode.OK
    assert summary["cancelled"] is None
    assert summary["output"] == STREAM_TEXT
    assert summary["kind"] == "LLM"


# -- m2: Ctrl-C inside next() and in the loop body are recorded alike ----------------------------------


def _interrupt_inside_next(stream, raised):
    """The vendor generator gives two events, then the third next() is interrupted."""
    vendor = stream.__wrapped__

    def interrupted():
        yield next(vendor)
        yield next(vendor)
        raise raised

    stream.__wrapped__ = interrupted()
    with pytest.raises(raised):
        for _ in stream:
            pass


def _interrupt_in_the_loop_body(stream, raised):
    with pytest.raises(raised):
        for index, _ in enumerate(stream):
            if index == 1:
                raise raised


@pytest.mark.parametrize("raised", [KeyboardInterrupt, SystemExit])
def test_an_interrupt_inside_next_and_in_the_loop_body_are_recorded_alike(raised):
    summaries = {}
    for where, interrupt in (("next", _interrupt_inside_next), ("body", _interrupt_in_the_loop_body)):
        with FakeReplicate() as fake, instrumented() as traced:
            stream = default_client(fake).stream(STREAM_MODEL, input={})
            interrupt(stream, raised)
            del stream
            gc.collect()
            drain_released()
            summaries[where] = _stopped_by_the_caller(traced.one())
    assert summaries["next"] == summaries["body"]


def test_a_keyboard_interrupt_inside_an_async_streams_anext_is_a_caller_stop():
    async def main(client):
        stream = await client.async_stream(STREAM_MODEL, input={})
        vendor = stream.__wrapped__

        async def interrupted():
            yield await vendor.__anext__()
            yield await vendor.__anext__()
            raise KeyboardInterrupt

        stream.__wrapped__ = interrupted()
        await stream.__anext__()
        await stream.__anext__()
        try:
            await stream.__anext__()
        except KeyboardInterrupt:
            return "interrupted"

    with FakeReplicate() as fake, instrumented() as traced:
        assert asyncio.run(main(default_client(fake))) == "interrupted"
        span = traced.one()
    _stopped_by_the_caller(span)


# -- m3: a fork whose wait ran out does not make the next forks wait ------------------------------------


def _fork_and_reap():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        started = time.monotonic()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child
            os._exit(0)
        took = time.monotonic() - started
    os.waitpid(pid, 0)
    return took


class _StuckOnTheFinisher(SpanExporter):
    """An exporter that hangs while the package's finisher exports through it."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def export(self, spans):
        if threading.current_thread().name == _stream_read.FINISHER_NAME:
            self.entered.set()
            self.release.wait(30)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        self.release.set()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_after_a_forks_wait_for_the_finisher_ran_out_later_forks_do_not_wait(monkeypatch):
    monkeypatch.setattr(_stream_read, "_FORK_WAIT_SECONDS", 0.6)
    stuck = _StuckOnTheFinisher()
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        traced.provider.add_span_processor(SimpleSpanProcessor(stuck))
        list(default_client(fake).stream(STREAM_MODEL, input={}))
        assert stuck.entered.wait(5)  # the finisher is inside the stuck export
        try:
            forks = [_fork_and_reap() for _ in range(3)]
        finally:
            stuck.release.set()
    assert forks[0] >= 0.5, forks  # the first fork waited for the export, up to the cap
    assert max(forks[1:]) < 0.3, forks  # the next ones did not wait for it again


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_after_a_forks_wait_for_the_read_loop_ran_out_later_forks_do_not_wait(monkeypatch):
    monkeypatch.setattr(_stream_read, "_FORK_WAIT_SECONDS", 0.6)
    with instrumented(**READ_ON):
        loop = _stream_read._STATE.loop
        assert loop is not None and _stream_read._STATE.loop_running
        blocked, unblock = threading.Event(), threading.Event()

        def busy():  # a step of the read loop that does not return for a while
            blocked.set()
            unblock.wait(10)

        loop.call_soon_threadsafe(busy)
        assert blocked.wait(5)
        try:
            forks = [_fork_and_reap() for _ in range(3)]
        finally:
            unblock.set()
        assert forks[0] >= 0.5, forks  # the first fork waited for the loop, up to the cap
        assert max(forks[1:]) < 0.3, forks  # the next ones did not wait for it again
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and getattr(_stream_read._STATE, "fork_loop_timed_out", True):
            time.sleep(0.01)
        # Once the loop runs again, a fork waits for it as before.
        assert getattr(_stream_read._STATE, "fork_loop_timed_out", True) is False


# -- m4: a subclass's _transport_for_url is never called for the read -------------------------------------


class _ResolvingClient(httpx.Client):
    calls: list = []

    def _transport_for_url(self, url):
        type(self).calls.append(str(url))
        return super()._transport_for_url(url)


class _ResolvingAsyncClient(httpx.AsyncClient):
    calls: list = []

    def _transport_for_url(self, url):
        type(self).calls.append(str(url))
        return super()._transport_for_url(url)


@pytest.mark.parametrize("kind", [_ResolvingClient, _ResolvingAsyncClient])
def test_a_subclassed_clients_transport_for_url_is_not_called_for_the_read(kind):
    kind.calls = []
    client = replicate.Client(api_token=API_TOKEN)
    http = kind(base_url="https://api.replicate.com", headers={"Authorization": "Bearer " + API_TOKEN})
    url = _stream_read._read_url(http, "p1")
    assert _stream_read._read_routing(client, http, url) is None
    assert kind.calls == []


def test_a_subclassed_client_gets_no_read_and_its_resolver_never_sees_the_read_url():
    _ResolvingClient.calls = []
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        client = default_client(fake)
        client._Client__client = _ResolvingClient(
            base_url=fake.origin, headers={"Authorization": "Bearer " + API_TOKEN}
        )
        events = list(client.stream(STREAM_MODEL, input={}))
        traced.settle()
        span = traced.one()
        reads = [path for path in fake.paths("GET") if path.startswith("/v1/predictions/")]
    assert [str(event) for event in events if str(event)] == STREAM_CHUNKS
    values = attrs(span)
    assert values["replicate.stream.read"] == "skipped"
    assert values["replicate.stream.skip_reason"] == "custom_transport"
    assert reads == []
    # The caller's own requests went through its resolver; the read's URL never did.
    assert _ResolvingClient.calls
    assert not [url for url in _ResolvingClient.calls if "/v1/predictions/pred" in url]


def test_a_plain_httpx_client_is_still_read():
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        list(default_client(fake).stream(STREAM_MODEL, input={}))
        traced.settle()
        values = attrs(traced.one())
    assert values["replicate.prediction.status"] == "succeeded"
    assert "replicate.stream.read" not in values


# -- m5: the skip reasons the README names for common integrations -------------------------------------


def _in_a_fresh_interpreter(code):
    """Run ``code`` in its own interpreter: these integrations patch process-wide
    state that has no complete undo."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, env=dict(os.environ)
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


def test_sentrys_httpx_integration_is_a_patched_http_skip():
    pytest.importorskip("sentry_sdk", reason="sentry-sdk is a dev dependency")
    code = (
        "import sentry_sdk\n"
        "from sentry_sdk.integrations.httpx import HttpxIntegration\n"
        "from traceai_replicate import _stream_read\n"
        "before = _stream_read.http_patched()\n"
        "sentry_sdk.init(dsn=None, integrations=[HttpxIntegration()], default_integrations=False)\n"
        "print(before, _stream_read.http_patched())\n"
    )
    assert _in_a_fresh_interpreter(code) == "None patched_http"


def test_truststore_inject_into_ssl_is_a_patched_socket_skip():
    pytest.importorskip("truststore", reason="truststore is a dev dependency")
    code = (
        "import truststore\n"
        "from traceai_replicate import _stream_read\n"
        "before = _stream_read.http_patched()\n"
        "truststore.inject_into_ssl()\n"
        "during = _stream_read.http_patched()\n"
        "truststore.extract_from_ssl()\n"
        "print(before, during, _stream_read.http_patched())\n"
    )
    assert _in_a_fresh_interpreter(code) == "None patched_socket None"


def test_instrument_client_on_the_instance_is_a_custom_transport_skip():
    instrumentor = pytest.importorskip(
        "opentelemetry.instrumentation.httpx", reason="opentelemetry-instrumentation-httpx is a test dependency"
    ).HTTPXClientInstrumentor
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        client = default_client(fake)
        instrumentor.instrument_client(client._client)
        try:
            assert _stream_read.http_patched() is None  # a change on the instance, not the class
            list(client.stream(STREAM_MODEL, input={}))
            traced.settle()
            values = attrs(traced.one())
        finally:
            instrumentor.uninstrument_client(client._client)
        reads = [path for path in fake.paths("GET") if path.startswith("/v1/predictions/")]
    assert values["replicate.stream.read"] == "skipped"
    assert values["replicate.stream.skip_reason"] == "custom_transport"
    assert reads == []
