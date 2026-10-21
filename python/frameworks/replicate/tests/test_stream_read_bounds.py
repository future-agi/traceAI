"""The read after a stream is bounded and costs the caller nothing (TH-8320 review, round 2).

Each test is a verifier probe turned into a regression test:

* no read (no request, no wait) for a span that is not recording, or after
  the provider is shut down, or where the provider cannot wait for it;
* async streams read through the async side (an async-only transport works,
  and no sync httpx client is built on the caller's ``Client``);
* ``force_flush(timeout_millis)`` never blocks longer than asked (round 3:
  by default it does not wait for reads at all, see
  ``test_stream_read_round3.py``), ``uninstrument()`` does not wait, and the
  budget is capped at 10 s;
* the budget is an absolute deadline for the read's connection, and the cap
  counts reads whose I/O is still running, not spans waiting for them;
* the read never borrows the caller's connection pool when replicate built
  the transport, and a small caller pool is not lent at all;
* completing a stream starts no thread on the caller's path;
* status labels: a stream without ``done``, ``done`` with reason
  ``canceled``, and the ``aborted`` status;
* a forked child does not export its parent's pending span;
* a stream that is never iterated has no span.

Everything runs against the loopback ``FakeReplicate``; no live call is made.
"""

from __future__ import annotations

import asyncio
import gc
import gzip
import json
import os
import threading
import time
import warnings

import httpx
import pytest

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

from opentelemetry import context as context_api  # noqa: E402
from opentelemetry import trace as trace_api  # noqa: E402
from opentelemetry.sdk.resources import Resource  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ParentBased, TraceIdRatioBased  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402
from traceai_replicate import ReplicateInstrumentor  # noqa: E402
from traceai_replicate._wrappers import drain_released  # noqa: E402

from _support import (  # noqa: E402
    API_TOKEN,
    INPUT_TOKENS,
    OUTPUT_TOKENS,
    STREAM_ABORTED_MODEL,
    STREAM_CANCELED_MODEL,
    STREAM_CHUNKS,
    STREAM_MODEL,
    STREAM_TEXT,
    STREAM_TRUNCATED_MODEL,
    FakeReplicate,
    RecordingTransport,
    attrs,
    instrumented,
    make_client,
)

READ = "/v1/predictions/pred0001"
EPSILON = 0.75
STATUS = "replicate.prediction.status"


def _reads(fake):
    return [path for path in fake.paths("GET") if path.startswith("/v1/predictions/")]


def _texts(events):
    return [str(event) for event in events if str(event)]


def _wait_for(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _default_client(fake):
    """A client whose transport replicate builds itself (the usual case)."""
    import replicate

    client = replicate.Client(api_token=API_TOKEN, base_url=fake.origin)
    client.poll_interval = 0.01
    return client


def _final(values):
    assert values[STATUS] == "succeeded"
    assert values["gen_ai.usage.input_tokens"] == INPUT_TOKENS
    assert values["gen_ai.usage.output_tokens"] == OUTPUT_TOKENS


def _no_final(values):
    for key in (STATUS, "replicate.metrics.predict_time", "gen_ai.usage.output_tokens"):
        assert key not in values, key


class _Provider:
    """A provider with its own sampler and an in-memory exporter."""

    def __init__(self, **options):
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider(
            resource=Resource.create({"service.name": "test-replicate"}), **options
        )
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))

    def spans(self):
        return [span for span in self.exporter.get_finished_spans() if span.name.startswith("replicate.")]


# -- no read for a span nobody will export ------------------------------------------------


def _stream_and_flush(fake, provider, instrument_with, parent=None, context=None):
    instrumentor = ReplicateInstrumentor()
    instrumentor.instrument(tracer_provider=instrument_with, stream_reload_seconds=2.0)
    try:
        token = context_api.attach(context) if context is not None else None
        try:
            client = make_client(fake)
            if parent is not None:
                with trace_api.use_span(parent):
                    events = list(client.stream(STREAM_MODEL, input={}))
            else:
                events = list(client.stream(STREAM_MODEL, input={}))
        finally:
            if token is not None:
                context_api.detach(token)
        started = time.monotonic()
        provider.force_flush(timeout_millis=10_000)
        return events, time.monotonic() - started
    finally:
        instrumentor.uninstrument()


@pytest.mark.parametrize("case", ["always-off", "parent-based-ratio-0", "unsampled-remote-parent"])
def test_an_unsampled_stream_makes_no_read_and_a_flush_does_not_wait(case):
    if case == "always-off":
        traced = _Provider(sampler=ALWAYS_OFF)
        parent = None
    elif case == "parent-based-ratio-0":
        traced = _Provider(sampler=ParentBased(TraceIdRatioBased(0.0)))
        parent = None
    else:
        traced = _Provider(sampler=ParentBased(TraceIdRatioBased(1.0)))
        parent = trace_api.NonRecordingSpan(
            trace_api.SpanContext(
                trace_id=0x1234, span_id=0x5678, is_remote=True, trace_flags=trace_api.TraceFlags(0)
            )
        )
    with FakeReplicate() as fake:
        fake.read_delay = 1.0
        events, flushed = _stream_and_flush(fake, traced.provider, traced.provider, parent=parent)
        assert _texts(events) == STREAM_CHUNKS
        assert _reads(fake) == []  # no API request spent on a span nobody exports
        assert flushed < 0.5, flushed
    assert traced.spans() == []


def test_a_call_under_the_callers_suppress_instrumentation_is_not_traced_and_makes_no_read():
    from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY

    traced = _Provider()
    with FakeReplicate() as fake:
        fake.read_delay = 1.0
        suppressed = context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True)
        events, flushed = _stream_and_flush(fake, traced.provider, traced.provider, context=suppressed)
        assert _texts(events) == STREAM_CHUNKS
        assert _reads(fake) == []
        assert flushed < 0.5, flushed
        # The create inside the stream is not traced either.
        assert fake.paths("POST") == ["/v1/models/acme/stream-model/predictions"]
    assert traced.spans() == []


def test_no_read_after_the_provider_is_shut_down():
    with FakeReplicate() as fake, instrumented() as traced:
        traced.provider.shutdown()
        list(make_client(fake).stream(STREAM_MODEL, input={}))
        list(_default_client(fake).stream(STREAM_MODEL, input={}))
        assert _reads(fake) == []


def test_without_a_provider_hook_a_stream_ends_at_once_without_a_read():
    """A provider whose force_flush/shutdown cannot be hooked cannot wait for a
    read (instrument() before register() is the common case), so none is made:
    the span is ended, and exported, inline."""

    class Bare:
        __slots__ = ("inner",)

        def __init__(self, inner):
            self.inner = inner

        def get_tracer(self, *args, **kwargs):
            return self.inner.get_tracer(*args, **kwargs)

    traced = _Provider()
    instrumentor = ReplicateInstrumentor()
    instrumentor.instrument(tracer_provider=Bare(traced.provider))
    try:
        with FakeReplicate() as fake:
            events = list(make_client(fake).stream(STREAM_MODEL, input={}))
            (span,) = traced.spans()  # no flush
            assert _reads(fake) == []
    finally:
        instrumentor.uninstrument()
    assert _texts(events) == STREAM_CHUNKS
    values = attrs(span)
    assert values["replicate.stream.read"] == "skipped"
    assert values["output.value"] == STREAM_TEXT


# -- async streams use the async side --------------------------------------------------------


class _AsyncOnly(httpx.AsyncBaseTransport):
    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport()
        self.calls = []

    async def handle_async_request(self, request):
        self.calls.append((request.method, request.url.path))
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        await self.inner.aclose()


def test_an_async_stream_through_an_async_only_transport_is_read_through_it():
    async def main(client, traced):
        stream = await client.async_stream(STREAM_MODEL, input={})
        events = [event async for event in stream]
        # A server's loop keeps running; the read runs on it.
        for _ in range(300):
            if traced.replicate_spans():
                break
            await asyncio.sleep(0.01)
        return events

    with FakeReplicate() as fake, instrumented() as traced:
        transport = _AsyncOnly()
        client = make_client(fake, transport)
        events = asyncio.run(main(client, traced))

    assert _texts(events) == STREAM_CHUNKS
    assert ("GET", READ) in transport.calls
    span = traced.one()
    assert span.status.status_code is StatusCode.OK
    _final(attrs(span))


def test_async_streams_on_a_default_client_build_no_sync_http_client():
    async def main(client):
        results = []
        for _ in range(5):
            stream = await client.async_stream(STREAM_MODEL, input={})
            results.append([event async for event in stream])
        return results

    with FakeReplicate() as fake, instrumented() as traced:
        client = _default_client(fake)
        results = asyncio.run(main(client))
        traced.settle()
        built = vars(client).get("_Client__client")

    assert all(_texts(events) == STREAM_CHUNKS for events in results)
    assert built is None  # the caller never asked for one
    assert len(traced.replicate_spans()) == 5
    for span in traced.replicate_spans():
        _final(attrs(span))


# -- flush, shutdown and uninstrument honour their time ----------------------------------------


def test_force_flush_with_an_opt_in_wait_never_waits_longer_than_half_its_timeout():
    with FakeReplicate() as fake, instrumented(stream_flush_wait_seconds=5) as traced:
        fake.read_delay = 1.5
        list(make_client(fake).stream(STREAM_MODEL, input={}))
        started = time.monotonic()
        result = traced.provider.force_flush(timeout_millis=100)
        took = time.monotonic() - started
        spans = traced.replicate_spans()

    assert 0.05 - 0.01 <= took < 0.1 + 0.25, took
    assert result is True  # the provider's own flush had the time left
    (span,) = spans
    values = attrs(span)
    _no_final(values)
    assert values["replicate.stream.read"] == "flushed"
    assert span.status.status_code is StatusCode.OK


def test_force_flush_with_an_opt_in_wait_waits_for_the_read():
    with FakeReplicate() as fake, instrumented(stream_flush_wait_seconds=2) as traced:
        fake.read_delay = 0.5
        list(make_client(fake).stream(STREAM_MODEL, input={}))
        assert traced.provider.force_flush() is True
        (span,) = traced.replicate_spans()
    _final(attrs(span))
    assert "replicate.stream.read" not in attrs(span)


def test_uninstrument_does_not_wait_for_a_read_and_the_span_still_ends():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = ReplicateInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    with FakeReplicate() as fake:
        fake.read_delay = 1.0
        list(make_client(fake).stream(STREAM_MODEL, input={}))
        started = time.monotonic()
        instrumentor.uninstrument()
        took = time.monotonic() - started
        assert took < 0.2, took
        assert _wait_for(lambda: len(exporter.get_finished_spans()) == 1, 2.0 + EPSILON)
    _final(attrs(exporter.get_finished_spans()[0]))


def test_shutdown_waits_no_longer_than_the_budget():
    budget = 0.5
    with FakeReplicate() as fake, instrumented(stream_reload_seconds=budget) as traced:
        fake.read_delay = 5.0
        list(make_client(fake).stream(STREAM_MODEL, input={}))
        started = time.monotonic()
        traced.provider.shutdown()
        took = time.monotonic() - started
        spans = traced.replicate_spans()
    assert took < budget + 0.25 + EPSILON, took
    assert len(spans) == 1


@pytest.mark.parametrize("value", [10.5, 60, 3600])
def test_stream_reload_seconds_is_capped_at_ten_seconds(value):
    with pytest.raises(ValueError, match="from 0 to 10"):
        ReplicateInstrumentor().instrument(tracer_provider=TracerProvider(), stream_reload_seconds=value)


def test_stream_reload_seconds_of_ten_is_accepted():
    instrumentor = ReplicateInstrumentor()
    instrumentor.instrument(tracer_provider=TracerProvider(), stream_reload_seconds=10)
    instrumentor.uninstrument()


# -- the deadline bounds the connection, the cap counts live reads ------------------------------


def test_a_trickling_read_is_cut_at_its_deadline_and_its_connection_closed():
    budget = 0.5
    with FakeReplicate() as fake, instrumented(stream_reload_seconds=budget) as traced:
        fake.read_trickle = 5.0  # headers arrive one line every 0.1 s, for 5 s
        list(_default_client(fake).stream(STREAM_MODEL, input={}))
        ended = time.monotonic()
        assert _wait_for(traced.replicate_spans, budget + EPSILON)
        # The server sees the read's connection closed soon after the deadline,
        # not after the 5 s the response would take.
        assert _wait_for(lambda: fake.read_disconnected, budget + 1.0)
        assert fake.read_disconnected[0] - ended < budget + 1.0
        assert fake.read_completed == []
    values = attrs(traced.one())
    _no_final(values)
    assert values["replicate.stream.read"] == "timeout"


def test_a_hundred_concurrent_trickling_reads_are_all_over_soon_after_their_deadline():
    """Under load a plain asyncio cancel can be absorbed inside httpcore's own
    anyio scopes and leave a read running; the deadline must still hold."""
    budget = 0.5
    with FakeReplicate() as fake, instrumented(stream_reload_seconds=budget) as traced:
        fake.read_trickle = 8.0
        client = _default_client(fake)
        workers = [
            threading.Thread(target=lambda: list(client.stream(STREAM_MODEL, input={}))) for _ in range(100)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(30)
        streamed = time.monotonic()
        assert _wait_for(lambda: len(traced.replicate_spans()) == 100, budget + EPSILON)
        # Every read's connection is closed: the server sees the client go away.
        assert _wait_for(lambda: len(fake.read_disconnected) >= 1, budget + 1.5)
        from traceai_replicate import _stream_read

        assert _wait_for(lambda: _stream_read.live_reads() == 0, budget + 1.5), _stream_read.live_reads()
        assert time.monotonic() - streamed < budget + 1.5
        assert fake.read_completed == []
    outcomes = {attrs(span).get("replicate.stream.read") for span in traced.replicate_spans()}
    assert outcomes <= {"timeout", "skipped"}


class _Gate(httpx.BaseTransport):
    """A caller transport whose prediction reads block until released."""

    def __init__(self):
        self.inner = RecordingTransport()
        self.gate = threading.Event()
        self.lock = threading.Lock()
        self.blocked = 0
        self.peak = 0

    def handle_request(self, request):
        if request.method == "GET" and request.url.path.startswith("/v1/predictions/"):
            with self.lock:
                self.blocked += 1
                self.peak = max(self.peak, self.blocked)
            try:
                self.gate.wait(10)
            finally:
                with self.lock:
                    self.blocked -= 1
        return self.inner.handle_request(request)

    def close(self):
        self.inner.close()


def test_reads_stuck_past_their_deadline_still_count_against_the_cap():
    cap = 4  # _stream_read._MAX_CALLER_READS
    budget = 0.2
    transport = _Gate()
    try:
        with FakeReplicate() as fake, instrumented(stream_reload_seconds=budget) as traced:
            client = make_client(fake, transport)
            for count in range(1, cap + 4):
                list(client.stream(STREAM_MODEL, input={}))
                # Each span ends at its deadline although its read is stuck.
                assert _wait_for(lambda: len(traced.replicate_spans()) == count, budget + EPSILON)
            assert transport.peak <= cap, transport.peak
            reads = [attrs(span).get("replicate.stream.read") for span in traced.replicate_spans()]
            assert reads.count("timeout") == cap
            assert reads.count("skipped") == 3
    finally:
        transport.gate.set()
    assert _wait_for(lambda: transport.blocked == 0, 5.0)


# -- the caller's connection pool --------------------------------------------------------------


def test_with_replicates_own_transport_the_read_does_not_use_the_callers_pool():
    with FakeReplicate() as fake, instrumented() as traced:
        client = _default_client(fake)
        events = iter(client.stream(STREAM_MODEL, input={}))
        first = next(events)  # the client has built its sync httpx client now
        inner = client._client._transport._wrapped_transport
        seen = []
        original = inner.handle_request

        def recording(request):
            seen.append((request.method, request.url.path))
            return original(request)

        inner.handle_request = recording
        rest = list(events)
        traced.settle()

    assert _texts([first] + rest) == STREAM_CHUNKS
    assert READ in fake.paths("GET")  # the read happened
    assert ("GET", READ) not in seen  # through the package's pool, not the caller's
    _final(attrs(traced.one()))


class _SubclassedTransport(httpx.HTTPTransport):
    """An httpx transport of the caller's own kind: its pool is used as it is."""


def test_a_small_caller_pool_is_not_lent_to_the_read():
    with FakeReplicate() as fake, instrumented() as traced:
        fake.read_delay = 1.5
        transport = _SubclassedTransport(limits=httpx.Limits(max_connections=1))
        client = make_client(fake, transport)
        list(client.stream(STREAM_MODEL, input={}))
        started = time.monotonic()
        list(client.stream(STREAM_MODEL, input={}))  # the caller's next request
        took = time.monotonic() - started
        traced.settle()
        reads = _reads(fake)

    assert took < 0.75, took
    assert reads == []
    assert {attrs(span)["replicate.stream.read"] for span in traced.replicate_spans()} == {"skipped"}


# -- nothing on the caller's path ------------------------------------------------------------------


def test_completing_a_stream_starts_no_thread_on_the_callers_path(monkeypatch):
    starts = []
    original = threading.Thread.start

    def start(self):
        if threading.current_thread() is threading.main_thread():
            starts.append(self.name)
        return original(self)

    with FakeReplicate() as fake, instrumented() as traced:
        list(_default_client(fake).stream(STREAM_MODEL, input={}))  # the workers exist now
        traced.settle()
        monkeypatch.setattr(threading.Thread, "start", start)
        for _ in range(5):
            list(_default_client(fake).stream(STREAM_MODEL, input={}))
            list(make_client(fake).stream(STREAM_MODEL, input={}))
        monkeypatch.undo()
        traced.settle()

    assert [name for name in starts if name.startswith("traceai-replicate")] == []
    assert len(traced.replicate_spans()) == 11
    for span in traced.replicate_spans():
        _final(attrs(span))


# -- what the stream itself says ---------------------------------------------------------------------


def test_a_stream_that_ends_without_done_is_marked_and_keeps_the_status_the_api_reports():
    with FakeReplicate() as fake, instrumented() as traced:
        events = list(make_client(fake).stream(STREAM_TRUNCATED_MODEL, input={}))
        traced.settle()
    assert _texts(events) == STREAM_CHUNKS
    values = attrs(traced.one())
    assert values["replicate.stream.truncated"] is True
    assert values[STATUS] == "processing"
    assert values["replicate.stream.read"] == "unfinished"
    assert values["output.value"] == STREAM_TEXT
    assert fake.paths("GET").count(READ) == 2


@pytest.mark.parametrize("budget", [0, 2.0], ids=["read-off", "read-on"])
def test_done_with_reason_canceled_is_a_canceled_prediction(budget):
    with FakeReplicate() as fake, instrumented(stream_reload_seconds=budget) as traced:
        list(make_client(fake).stream(STREAM_CANCELED_MODEL, input={}))
        traced.settle()
    span = traced.one()
    values = attrs(span)
    assert values["replicate.stream.done_reason"] == "canceled"
    assert values[STATUS] == "canceled"
    assert span.status.status_code is StatusCode.OK  # canceled is terminal, not a failure
    assert "replicate.cancelled" not in values  # the caller did not abandon it


def test_aborted_is_a_final_status_read_once():
    with FakeReplicate() as fake, instrumented() as traced:
        list(make_client(fake).stream(STREAM_ABORTED_MODEL, input={}))
        traced.settle()
    values = attrs(traced.one())
    assert values[STATUS] == "aborted"
    assert fake.paths("GET").count(READ) == 1


# -- the read asks for an uncompressed body; a compressed one stays bounded ---------------------------


def test_the_read_asks_for_an_uncompressed_body():
    with FakeReplicate() as fake, instrumented() as traced:
        list(_default_client(fake).stream(STREAM_MODEL, input={}))
        list(make_client(fake).stream(STREAM_MODEL, input={}))
        traced.settle()
        for path in (READ, "/v1/predictions/pred0002"):
            assert fake.request_headers("GET", path)["accept-encoding"] == "identity"


def test_a_compressed_body_is_never_inflated_past_the_cap():
    from traceai_replicate import _stream_read

    bomb = gzip.compress(b"0" * (64 * 1024 * 1024))  # 64 MiB of zeros, about 64 KiB compressed
    body = _stream_read._Body("gzip")
    accepted = True
    for start in range(0, len(bomb), 16 * 1024):
        accepted = body.add(bomb[start : start + 16 * 1024])
        assert len(body.data) <= _stream_read._MAX_READ_BYTES + 1
        if not accepted:
            break
    assert accepted is False

    small = _stream_read._Body("gzip")
    assert small.add(gzip.compress(json.dumps({"id": "p", "status": "succeeded"}).encode()))
    assert small.prediction() == {"id": "p", "status": "succeeded"}


# -- a forked child ------------------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_a_forked_child_does_not_export_its_parents_pending_span():
    with FakeReplicate() as fake, instrumented() as traced:
        fake.read_delay = 1.0
        list(make_client(fake).stream(STREAM_MODEL, input={}))  # its read is in flight
        read_end, write_end = os.pipe()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # fork with threads (3.12+)
            pid = os.fork()
        if pid == 0:  # pragma: no cover - the child
            code = 1
            try:
                os.close(read_end)
                traced.provider.force_flush(timeout_millis=2000)
                drain_released()
                exported = len(traced.replicate_spans())
                os.write(write_end, str(exported).encode())
                code = 0
            finally:
                os._exit(code)
        os.close(write_end)
        child_exported = int(os.read(read_end, 16).decode() or "-1")
        os.close(read_end)
        _, status = os.waitpid(pid, 0)
        traced.settle()
        parent_exported = len(traced.replicate_spans())

    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert child_exported == 0
    assert parent_exported == 1


# -- a stream never iterated -------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["drop", "close"])
def test_a_stream_never_iterated_sends_nothing_and_has_no_span(how):
    with FakeReplicate() as fake, instrumented() as traced:
        stream = make_client(fake).stream(STREAM_MODEL, input={})
        if how == "close":
            stream.close()
        else:
            del stream
            gc.collect()
            drain_released()
        traced.settle()
        assert fake.paths() == []
        assert traced.replicate_spans() == []


def test_a_stream_span_starts_at_its_first_next_under_the_callers_parent():
    with FakeReplicate() as fake, instrumented() as traced:
        tracer = traced.tracer()
        with tracer.start_as_current_span("parent") as parent:
            stream = make_client(fake).stream(STREAM_MODEL, input={})
        created = time.time_ns()
        time.sleep(0.05)
        events = list(stream)  # iterated outside the parent's block
        traced.settle()
    assert _texts(events) == STREAM_CHUNKS
    span = traced.one()
    assert span.parent.span_id == parent.get_span_context().span_id
    assert span.start_time > created
