"""The opt-in read after a stream, TH-8320 review round 3.

Each test is a verifier probe turned into a regression test (round 4: the
read is off by default, so each one turns it on with ``stream_reload_seconds``):

* ``asyncio.run()`` (any loop teardown that cancels its tasks) does not wait
  for a read (round 4: none ever runs on the caller's loop), and a plain
  ``httpx`` transport is read through the package's own pool (with its TLS
  settings), not the caller's loop or pool;
* ``force_flush()`` does not wait for reads by default (a per-request flush
  pays nothing); ``stream_flush_wait_seconds`` opts in, and the time passed
  to the provider's own flush is what is left of the caller's timeout;
* name lookups of a host-name base URL wait for a resolver (and are shared)
  instead of failing when more than two overlap;
* a KeyboardInterrupt while the read is planned ends the span and frees its
  slot;
* a slow inline exporter does not hold back the start of reads, and every
  admitted read has a connection;
* the first completed stream in a process starts no thread on the caller's
  path;
* a fork right after a stream never copies a lock the finisher holds;
* an ended span is not kept alive until its read's deadline;
* a custom transport of the caller's is never used for the read, so it can
  neither hold read slots nor turn off reads through another client.

Everything runs against the loopback ``FakeReplicate``; no live call is made.
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import os
import signal
import ssl
import subprocess
import sys
import threading
import time
import warnings
import weakref
from pathlib import Path

import httpx
import pytest

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

import replicate  # noqa: E402
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import (  # noqa: E402
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402
from traceai_replicate import ReplicateInstrumentor, _stream_read  # noqa: E402

from _support import (  # noqa: E402
    API_TOKEN,
    INPUT_TOKENS,
    OUTPUT_TOKENS,
    STREAM_CHUNKS,
    STREAM_MODEL,
    STREAM_TEXT,
    READ_ON,
    FakeReplicate,
    RecordingTransport,
    attrs,
    default_client,
    instrumented,
    make_client,
    own_sends,
)

READ = "/v1/predictions/pred0001"
STATUS = "replicate.prediction.status"
PACKAGE = Path(__file__).resolve().parents[1]
PYTHON_ROOT = PACKAGE.parents[1]


def _texts(events):
    return [str(event) for event in events if str(event)]


def _wait_for(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _default_client(fake, base_url=None):
    return default_client(fake, base_url)


def _final(values):
    assert values[STATUS] == "succeeded", values.get("replicate.stream.read")
    assert values["gen_ai.usage.input_tokens"] == INPUT_TOKENS
    assert values["gen_ai.usage.output_tokens"] == OUTPUT_TOKENS


def _no_final(values):
    for key in (STATUS, "replicate.metrics.predict_time", "gen_ai.usage.output_tokens"):
        assert key not in values, key


def _completed(span):
    values = attrs(span)
    assert span.status.status_code is StatusCode.OK
    assert values["output.value"] == STREAM_TEXT
    assert "replicate.cancelled" not in values
    return values


def _by_id(spans, prediction_id):
    (span,) = [span for span in spans if attrs(span).get("replicate.prediction.id") == prediction_id]
    return span


class _AsyncOnly(httpx.AsyncBaseTransport):
    """A transport of the caller's that can only send asynchronously."""

    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport()
        self.calls = []

    async def handle_async_request(self, request):
        self.calls.append((request.method, request.url.path))
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        await self.inner.aclose()


class _SlowExporter(SpanExporter):
    """Exports inline, slowly, holding a lock (as an HTTP exporter holds its pool's)."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.lock = threading.Lock()
        self.spans = []

    def export(self, spans):
        with self.lock:
            time.sleep(self.seconds)
            self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


class _Gate(httpx.BaseTransport):
    """A caller transport whose prediction reads block until released."""

    def __init__(self):
        self.inner = RecordingTransport()
        self.gate = threading.Event()
        self.lock = threading.Lock()
        self.blocked = 0

    def handle_request(self, request):
        if request.method == "GET" and request.url.path.startswith("/v1/predictions/"):
            with self.lock:
                self.blocked += 1
            try:
                self.gate.wait(10)
            finally:
                with self.lock:
                    self.blocked -= 1
        return self.inner.handle_request(request)

    def close(self):
        self.inner.close()


@pytest.fixture
def own_provider():
    """Instrument against a provider the test builds; settle and uninstrument after."""
    made = []

    def make(provider, **options):
        instrumentor = ReplicateInstrumentor()
        instrumentor.instrument(tracer_provider=provider, **options)
        made.append(instrumentor)
        return instrumentor

    yield make
    _stream_read.settle_stream_reads()
    for instrumentor in made:
        instrumentor.uninstrument()


# -- the end of the caller's event loop -----------------------------------------------------


def test_asyncio_run_does_not_wait_for_a_read_on_its_loop():
    """Round 4: an async-only transport of the caller's is a custom transport,
    so no read runs on the caller's loop at all: asyncio.run() has nothing to
    wait for, and the span says why it has no status."""
    marks = {}

    async def main(client):
        stream = await client.async_stream(STREAM_MODEL, input={})
        events = [event async for event in stream]
        marks["main"] = time.monotonic()
        return events

    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        fake.read_delay = 1.5
        transport = _AsyncOnly()
        events = asyncio.run(main(make_client(fake, transport)))
        returned = time.monotonic() - marks["main"]
        span = traced.one()  # ended at done, before asyncio.run() returned

    assert _texts(events) == STREAM_CHUNKS
    assert returned < 0.3, returned
    assert ("GET", READ) not in transport.calls
    assert READ not in fake.paths("GET")
    values = _completed(span)
    _no_final(values)
    assert (values["replicate.stream.read"], values["replicate.stream.skip_reason"]) == ("skipped", "custom_transport")


def test_a_plain_httpx_async_transport_is_read_through_the_package_pool():
    """replicate.Client(transport=httpx.AsyncHTTPTransport(retries=2)) is the
    usual way to set connect retries; it has no handle_request. Its read goes
    through the package's own pool on the package's loop, so asyncio.run()
    does not wait for it and the span still gets the final status."""
    marks = {}

    async def main(client):
        events = [event async for event in await client.async_stream(STREAM_MODEL, input={})]
        marks["main"] = time.monotonic()
        return events

    with FakeReplicate() as fake, instrumented(**READ_ON) as traced, own_sends() as sent:
        fake.read_delay = 0.5
        transport = httpx.AsyncHTTPTransport(retries=2)
        client = replicate.Client(api_token=API_TOKEN, base_url=fake.origin, transport=transport)
        events = asyncio.run(main(client))
        returned = time.monotonic() - marks["main"]
        assert _wait_for(traced.replicate_spans, 2.0)
        reads = [path for path in fake.paths("GET") if path == READ]

    assert _texts(events) == STREAM_CHUNKS
    assert returned < 0.25, returned
    assert reads == [READ]
    assert sent == [("GET", READ)]  # the one read went through the package's pool, not the caller's
    _final(_completed(traced.one()))


def test_a_client_per_call_with_its_own_transport_still_gets_every_read():
    """A sync app that runs each call with asyncio.run() and a fresh client
    (each with its own TLS context): more package clients than are kept, none
    closed while its read is in flight."""

    async def call(origin):
        client = replicate.Client(
            api_token=API_TOKEN, base_url=origin, transport=httpx.AsyncHTTPTransport(retries=2)
        )
        return [event async for event in await client.async_stream(STREAM_MODEL, input={})]

    count = 10
    with FakeReplicate() as fake, instrumented(stream_reload_seconds=5.0) as traced:
        fake.read_delay = 2.5
        took = []
        for _ in range(count):
            started = time.monotonic()
            assert _texts(asyncio.run(call(fake.origin))) == STREAM_CHUNKS
            took.append(time.monotonic() - started)
        traced.settle()
        spans = traced.replicate_spans()

    # No asyncio.run() waits for its read (2.5 s each); building a client and
    # a TLS context per call is the rest, and grows with the machine's load.
    took.sort()
    assert took[len(took) // 2] < 0.5, took
    assert took[-1] < 1.5, took
    assert len(spans) == count
    for span in spans:
        _final(_completed(span))


def test_a_plain_httpx_transport_with_a_small_pool_is_read_through_the_package_pool():
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        fake.read_delay = 1.0
        transport = httpx.HTTPTransport(limits=httpx.Limits(max_connections=1))
        client = make_client(fake, transport)
        list(client.stream(STREAM_MODEL, input={}))
        started = time.monotonic()
        list(client.stream(STREAM_MODEL, input={}))  # the caller's next request
        took = time.monotonic() - started
        traced.settle()
        spans = traced.replicate_spans()

    assert took < 0.75, took  # the caller's one connection was never lent
    assert len(spans) == 2
    for span in spans:
        _final(_completed(span))


def test_the_package_pool_copies_a_plain_transports_tls_context_and_settings():
    context = ssl.create_default_context()
    transport = httpx.HTTPTransport(verify=context, retries=3, local_address="127.0.0.1")
    settings = dict(_stream_read._plain_settings(transport))
    assert settings["verify"] is context
    assert settings["retries"] == 3
    assert settings["local_address"] == "127.0.0.1"
    assert dict(_stream_read._plain_settings(httpx.AsyncHTTPTransport(http2=False)))["http2"] is False

    class Subclassed(httpx.HTTPTransport):
        pass

    assert _stream_read._plain_settings(httpx.HTTPTransport(proxy="http://127.0.0.1:9")) is None
    assert _stream_read._plain_settings(Subclassed()) is None
    assert _stream_read._plain_settings(RecordingTransport()) is None


# -- the flush contract ---------------------------------------------------------------------


def test_force_flush_does_not_wait_for_a_read_by_default():
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        fake.read_delay = 1.0
        list(_default_client(fake).stream(STREAM_MODEL, input={}))
        started = time.monotonic()
        assert traced.provider.force_flush() is True
        took = time.monotonic() - started
        spans = traced.replicate_spans()  # exported by that flush

    assert took < 0.1, took
    (span,) = spans
    values = _completed(span)
    _no_final(values)
    assert values["replicate.stream.read"] == "flushed"


def test_a_flush_after_every_request_pays_no_read_round_trip():
    workers, rounds = 4, 5
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        fake.read_delay = 0.2
        client = _default_client(fake)
        flushes, errors = [], []

        def worker():
            try:
                for _ in range(rounds):
                    list(client.stream(STREAM_MODEL, input={}))
                    started = time.monotonic()
                    traced.provider.force_flush()
                    flushes.append(time.monotonic() - started)
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        spans = traced.replicate_spans()

    assert errors == []
    assert len(spans) == workers * rounds
    assert max(flushes) < 0.1, max(flushes)


def test_an_opt_in_shutdown_wait_waits_for_a_read_within_the_budget():
    with FakeReplicate() as fake, instrumented(stream_shutdown_wait_seconds=2, **READ_ON) as traced:
        fake.read_delay = 0.3
        list(_default_client(fake).stream(STREAM_MODEL, input={}))
        traced.provider.shutdown()
        spans = traced.replicate_spans()
    (span,) = spans
    _final(_completed(span))


@pytest.mark.parametrize("value, error", [("1", TypeError), (True, TypeError), (-1, ValueError), (10.5, ValueError)])
def test_stream_flush_wait_seconds_is_checked(value, error):
    with pytest.raises(error, match="stream_flush_wait_seconds"):
        ReplicateInstrumentor().instrument(tracer_provider=TracerProvider(), stream_flush_wait_seconds=value)


def test_a_one_millisecond_flush_still_reaches_the_processors(own_provider):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=60_000))
    own_provider(provider)
    try:
        with provider.get_tracer("app").start_as_current_span("app-span"):
            pass
        assert provider.force_flush(timeout_millis=1) is True
        assert [span.name for span in exporter.get_finished_spans()] == ["app-span"]
    finally:
        provider.shutdown()


def test_the_flush_hook_keeps_the_original_name_and_signature(own_provider):
    provider = TracerProvider()
    own_provider(provider)
    assert "timeout_millis" in inspect.signature(provider.force_flush).parameters
    assert provider.force_flush.__name__ == "force_flush"


class _Recording(SpanProcessor):
    """Records when its force_flush was called and the time it was given."""

    def __init__(self):
        self.calls = []

    def on_start(self, span, parent_context=None):
        pass

    def on_end(self, span):
        pass

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        self.calls.append((time.monotonic(), timeout_millis))
        return True


def test_a_flush_wrapped_before_instrument_still_gets_only_the_time_left(own_provider):
    recording = _Recording()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_SlowExporter(0.08)))  # ends spans inline
    provider.add_span_processor(recording)
    original = provider.force_flush
    provider.force_flush = lambda *args, **kwargs: original(*args, **kwargs)  # someone else's wrapper
    own_provider(provider, **READ_ON)
    with FakeReplicate() as fake:
        fake.read_delay = 1.5
        list(_default_client(fake).stream(STREAM_MODEL, input={}))
        started = time.monotonic()
        provider.force_flush(timeout_millis=400)
    called_at, given = recording.calls[-1]
    spent = (called_at - started) * 1000.0
    assert spent + given <= 400 + 5, (spent, given)


# -- name lookups ------------------------------------------------------------------------------


def _slow_lookups(monkeypatch, seconds):
    """Lookups of ``localhost`` on the package's resolver threads take ``seconds``
    (a real resolver's latency); everything else resolves as usual."""
    original = _stream_read._lookup  # the resolver threads' lookup (socket.getaddrinfo replaced is a socket-level mock)
    calls = []

    def slow(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        if name == "localhost" and threading.current_thread().name == _stream_read.RESOLVER_NAME:
            calls.append(args[0] if args else kwargs.get("port"))
            time.sleep(seconds)
        return original(host, *args, **kwargs)

    monkeypatch.setattr(_stream_read, "_lookup", slow)
    return calls


def test_thirty_concurrent_reads_against_a_host_name_all_carry_tokens(monkeypatch):
    lookups = _slow_lookups(monkeypatch, 0.05)
    count = 30
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        base = fake.origin.replace("127.0.0.1", "localhost")
        clients = [_default_client(fake, base) for _ in range(count)]
        barrier = threading.Barrier(count)
        errors = []

        def one(client):
            try:
                stream = client.stream(STREAM_MODEL, input={})
                barrier.wait(30)
                list(stream)
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(error)

        threads = [threading.Thread(target=one, args=(client,)) for client in clients]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        traced.settle()
        spans = traced.replicate_spans()

    assert errors == []
    assert len(spans) == count
    reads = sorted(str(attrs(span).get("replicate.stream.read")) for span in spans)
    assert reads == ["None"] * count, reads
    for span in spans:
        _final(_completed(span))
    assert len(lookups) <= 3, len(lookups)  # shared, and kept for a few seconds


def test_lookups_of_different_names_wait_for_a_free_resolver(monkeypatch):
    """Four reads that each need their own lookup (another port is another
    query) with two resolver threads: the last two wait, they do not fail."""
    _slow_lookups(monkeypatch, 0.2)
    fakes = [FakeReplicate() for _ in range(4)]
    try:
        with instrumented(**READ_ON) as traced:
            clients = [_default_client(fake, fake.origin.replace("127.0.0.1", "localhost")) for fake in fakes]
            threads = [
                threading.Thread(target=lambda client=client: list(client.stream(STREAM_MODEL, input={})))
                for client in clients
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            traced.settle()
            spans = traced.replicate_spans()
    finally:
        for fake in fakes:
            fake.close()
    assert len(spans) == 4
    for span in spans:
        _final(_completed(span))


def test_while_every_resolver_is_stuck_a_new_lookup_fails_at_once(monkeypatch):
    monkeypatch.setattr(_stream_read, "_RESOLVER_STUCK_SECONDS", 0.2, raising=False)
    release = threading.Event()
    original = _stream_read._lookup  # the resolver threads' lookup (socket.getaddrinfo replaced is a socket-level mock)
    fakes = [FakeReplicate() for _ in range(3)]
    hung_ports = {int(fake.origin.rsplit(":", 1)[1]) for fake in fakes[:2]}

    def hung(host, port, *args, **kwargs):
        if threading.current_thread().name == _stream_read.RESOLVER_NAME and port in hung_ports:
            release.wait(10)
        return original(host, port, *args, **kwargs)

    monkeypatch.setattr(_stream_read, "_lookup", hung)
    try:
        with instrumented(stream_reload_seconds=2.0) as traced:
            for fake in fakes[:2]:
                list(_default_client(fake, fake.origin.replace("127.0.0.1", "localhost")).stream(STREAM_MODEL, input={}))
            time.sleep(0.4)  # both resolver threads are stuck now
            third = fakes[2]
            list(_default_client(third, third.origin.replace("127.0.0.1", "localhost")).stream(STREAM_MODEL, input={}))
            ended = time.monotonic()
            assert _wait_for(lambda: len(traced.replicate_spans()) == 1, 1.0)
            took = time.monotonic() - ended
            failed = attrs(traced.replicate_spans()[0])
            release.set()
    finally:
        release.set()
        for fake in fakes:
            fake.close()
    assert took < 1.0, took
    assert failed["replicate.stream.read"] == "failed"


# -- KeyboardInterrupt while the read is planned ----------------------------------------------


@pytest.mark.parametrize("where", ["before-admission", "after-admission"])
def test_a_keyboard_interrupt_while_the_read_is_planned_ends_the_span_and_frees_its_slot(monkeypatch, where):
    if where == "before-admission":

        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(_stream_read, "_read_url", interrupted)
    else:
        original = _stream_read._Read.admit

        def admit(self, *args):
            original(self, *args)  # takes its slot
            raise KeyboardInterrupt

        monkeypatch.setattr(_stream_read._Read, "admit", admit)

    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        stream = _default_client(fake).stream(STREAM_MODEL, input={})
        with pytest.raises(KeyboardInterrupt):
            for _ in stream:
                pass
        monkeypatch.undo()
        spans = traced.replicate_spans()  # no flush
        live, pending = _stream_read.live_reads(), _stream_read.pending_spans()

    assert len(spans) == 1
    values = _completed(spans[0])
    _no_final(values)
    assert (values["replicate.stream.read"], values["replicate.stream.skip_reason"]) == ("skipped", "interrupted")
    assert (live, pending) == (0, 0)


# -- the finisher, the pool and the threads ----------------------------------------------------


def test_a_slow_inline_exporter_does_not_hold_back_the_reads(own_provider):
    """Streams complete faster (about 20 a second) than an inline exporter
    exports (10 a second): span ends queue up, but no read waits behind them."""
    exporter = _SlowExporter(0.1)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    own_provider(provider, stream_reload_seconds=0.5)
    with FakeReplicate() as fake:
        client = _default_client(fake)
        for _ in range(20):
            list(client.stream(STREAM_MODEL, input={}))
            time.sleep(0.05)
        assert _wait_for(lambda: len(exporter.spans) == 20, 10.0)
    reads = [attrs(span).get("replicate.stream.read") for span in exporter.spans]
    assert reads == [None] * 20, reads
    for span in exporter.spans:
        _final(_completed(span))


def test_spans_waiting_for_a_slow_exporter_are_capped(own_provider, monkeypatch):
    monkeypatch.setattr(_stream_read, "_MAX_PENDING_SPANS", 3, raising=False)
    exporter = _SlowExporter(0.3)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    own_provider(provider, **READ_ON)
    with FakeReplicate() as fake:
        client = _default_client(fake)
        for _ in range(6):
            list(client.stream(STREAM_MODEL, input={}))
            assert _stream_read.pending_spans() <= 3
        assert _wait_for(lambda: len(exporter.spans) == 6, 10.0)
    reads = sorted(str(attrs(span).get("replicate.stream.read")) for span in exporter.spans)
    assert reads.count("skipped") >= 1, reads  # past the cap: ended on the caller's thread
    reasons = {attrs(span).get("replicate.stream.skip_reason") for span in exporter.spans} - {None}
    assert reasons == {"at_capacity"}, reasons


def test_every_admitted_read_has_a_connection():
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        fake.read_delay = 1.2
        client = _default_client(fake)
        for _ in range(20):
            list(client.stream(STREAM_MODEL, input={}))
        traced.settle()
        spans = traced.replicate_spans()
    assert len(spans) == 20
    for span in spans:
        _final(_completed(span))


FIRST_STREAM_SCRIPT = """
import os
import threading

import replicate
from opentelemetry.sdk.trace import TracerProvider
from traceai_replicate import ReplicateInstrumentor

provider = TracerProvider()
ReplicateInstrumentor().instrument(tracer_provider=provider, stream_reload_seconds=2.0)
starts = []
original = threading.Thread.start


def start(self):
    if threading.current_thread() is threading.main_thread():
        starts.append(self.name)
    return original(self)


threading.Thread.start = start
client = replicate.Client(api_token="r8_placeholder", base_url=os.environ["REPLICATE_BASE_URL"])
list(client.stream("{model}", input={{}}))
threading.Thread.start = original
print("STARTS=" + ",".join(name for name in starts if name.startswith("traceai-replicate")))
provider.shutdown()
""".format(model=STREAM_MODEL)


def test_the_first_stream_in_a_process_starts_no_thread_on_the_callers_path(tmp_path):
    script = tmp_path / "first_stream.py"
    script.write_text(FIRST_STREAM_SCRIPT)
    with FakeReplicate() as fake:
        env = {key: value for key, value in os.environ.items() if not key.startswith(("REPLICATE_", "FI_"))}
        env["REPLICATE_BASE_URL"] = fake.origin
        env["PYTHONPATH"] = os.pathsep.join([str(PACKAGE), str(PYTHON_ROOT), os.environ.get("PYTHONPATH", "")])
        result = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert "STARTS=\n" in result.stdout.decode(), result.stdout.decode()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_a_fork_right_after_a_stream_never_copies_a_lock_the_finisher_holds(own_provider):
    exporter = _SlowExporter(0.3)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    own_provider(provider, **READ_ON)
    stuck = 0
    with FakeReplicate() as fake:
        client = _default_client(fake)
        for _ in range(3):
            list(client.stream(STREAM_MODEL, input={}))
            # The read returns at once; the finisher is now exporting its span.
            assert _wait_for(exporter.lock.locked, 2.0)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)  # fork with threads (3.12+)
                pid = os.fork()
            if pid == 0:  # pragma: no cover - the child
                code = 3
                try:
                    signal.alarm(5)  # a child stuck on a copied lock is ended by SIGALRM
                    with provider.get_tracer("child").start_as_current_span("child-span"):
                        pass
                    code = 0
                finally:
                    os._exit(code)
            _, status = os.waitpid(pid, 0)
            if not (os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0):
                stuck += 1
        assert _wait_for(lambda: len(exporter.spans) == 3, 5.0)
    assert stuck == 0


def test_an_ended_span_is_not_kept_alive_until_its_reads_deadline():
    with FakeReplicate() as fake, instrumented(stream_reload_seconds=10) as traced:
        stream = _default_client(fake).stream(STREAM_MODEL, input={})
        call = weakref.ref(stream._self_call)
        list(stream)
        assert _wait_for(traced.replicate_spans, 2.0)  # the read returned; its deadline is 10 s away
        del stream
        gc.collect()
        alive = call() is not None
    _final(attrs(traced.one()))
    assert not alive


def test_a_stuck_custom_transport_is_never_used_for_the_read_and_holds_no_slot():
    """Round 3 gave each caller transport its own read slots; round 4 never
    reads through a custom transport, so one that would block holds nothing."""
    gate = _Gate()
    try:
        with FakeReplicate() as fake, instrumented(stream_reload_seconds=0.5) as traced:
            stuck = make_client(fake, gate)
            for _ in range(4):
                list(stuck.stream(STREAM_MODEL, input={}))
            assert gate.blocked == 0
            assert _stream_read.live_reads() == 0
            list(_default_client(fake).stream(STREAM_MODEL, input={}))
            traced.settle()
            spans = traced.replicate_spans()
    finally:
        gate.gate.set()
    assert len(spans) == 5
    for number in range(1, 5):
        values = attrs(_by_id(spans, "pred000{0}".format(number)))
        assert (values["replicate.stream.read"], values["replicate.stream.skip_reason"]) == ("skipped", "custom_transport")
    _final(_completed(_by_id(spans, "pred0005")))
