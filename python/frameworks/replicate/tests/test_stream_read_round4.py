"""The read after a stream, TH-8320 review round 4: off by default.

Default (``stream_reload_seconds`` = 0):

* no thread is ever started (``threading.enumerate()`` before and after
  ``instrument()`` and a run of sync and async streams, in a process of its
  own, compared with the same run uninstrumented);
* no request is made after a stream (counted at the server and at the
  caller's transport);
* a stream span ends at its ``done`` event, on the caller's thread, OK, with
  what the stream gave, before the caller's ``next()`` / ``__anext__()``
  returns that event;
* ``force_flush()``, ``shutdown()``, ``uninstrument()``, interpreter exit and
  ``os.fork()`` add no wait.

Opt-in (``stream_reload_seconds`` > 0):

* a transport of the caller's other than replicate's own or a plain httpx one
  (custom, mock, scripted, recording, subclassed, proxied, wrapped,
  async-only) is never called for the read: the span records
  ``replicate.stream.read`` = ``skipped`` and
  ``replicate.stream.skip_reason`` = ``custom_transport``;
* plain transports are read through the package's own pool;
* ``shutdown()`` and ``uninstrument()`` end pending stream spans at once, and
  a shutdown after ``uninstrument()`` still exports them; a span the finisher
  is exporting at that moment is not cut off;
* ``asyncio.run()`` with an async-only transport adds no wait and leaves no
  task on its loop;
* the name-lookup queue is bounded.

Everything runs against the loopback ``FakeReplicate``; no live call is made.
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import re
import subprocess
import sys
import threading
import time
import warnings
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
from replicate.client import RetryTransport  # noqa: E402
from replicate.stream import ServerSentEvent  # noqa: E402
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
FINAL_KEYS = (STATUS, "replicate.metrics.predict_time", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens")
PACKAGE = Path(__file__).resolve().parents[1]
PYTHON_ROOT = PACKAGE.parents[1]
# What "no wait" may cost on a loaded shared machine; a read here takes 1.5 s or more.
NO_WAIT = 0.15


def _texts(events):
    return [str(event) for event in events if str(event)]


def _is_done(event):
    return event.event == ServerSentEvent.EventType.DONE


def _reads(fake):
    return [path for path in fake.paths("GET") if path.startswith("/v1/predictions/")]


def _final(values):
    assert values[STATUS] == "succeeded", values.get("replicate.stream.read")
    assert values["gen_ai.usage.input_tokens"] == INPUT_TOKENS
    assert values["gen_ai.usage.output_tokens"] == OUTPUT_TOKENS


def _no_final(values):
    for key in FINAL_KEYS:
        assert key not in values, key


def _completed(span):
    values = attrs(span)
    assert span.status.status_code is StatusCode.OK
    assert values["output.value"] == STREAM_TEXT
    assert "replicate.cancelled" not in values
    return values


def _skipped(values, reason):
    assert (values.get("replicate.stream.read"), values.get("replicate.stream.skip_reason")) == ("skipped", reason)


def _env(fake, **extra):
    env = {key: value for key, value in os.environ.items() if not key.startswith(("FI_", "REPLICATE_", "OTEL_"))}
    env["REPLICATE_BASE_URL"] = fake.origin
    env["PYTHONPATH"] = os.pathsep.join([str(PACKAGE), str(PYTHON_ROOT), os.environ.get("PYTHONPATH", "")])
    env.update(extra)
    return env


class _EndThreads(SpanProcessor):
    """Records the thread each span ended on."""

    def __init__(self):
        self.threads = []

    def on_start(self, span, parent_context=None):
        pass

    def on_end(self, span):
        self.threads.append((span.name, threading.current_thread().name))

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


# -- default: no thread, ever ------------------------------------------------------------------

THREADS_SCRIPT = """
import asyncio, json, os, threading
import replicate
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Built first in both modes: the SDK's own resource detection starts (and
# ends) ThreadPoolExecutor threads in TracerProvider(); that is not the package.
exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
started = []
original = threading.Thread.start


def start(self):
    started.append(self.name)
    return original(self)


threading.Thread.start = start


def alive():
    return sorted(thread.name for thread in threading.enumerate())


N = int(os.environ["STREAMS"])
before = alive()
instrumented = os.environ["MODE"] == "instrumented"
if instrumented:
    from traceai_replicate import ReplicateInstrumentor

    instrumentor = ReplicateInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
after_instrument = alive()
client = replicate.Client(api_token="r8_placeholder", base_url=os.environ["REPLICATE_BASE_URL"])
for _ in range(N):
    list(client.stream("{model}", input={{}}))
after_sync = alive()


async def main():
    aclient = replicate.Client(api_token="r8_placeholder", base_url=os.environ["REPLICATE_BASE_URL"])
    for _ in range(N):
        [event async for event in await aclient.async_stream("{model}", input={{}})]


asyncio.run(main())
spans = 0
if instrumented:
    provider.force_flush()
    spans = len([span for span in exporter.get_finished_spans() if span.name == "replicate.stream"])
    instrumentor.uninstrument()
    provider.shutdown()
print("RESULT=" + json.dumps(dict(
    before=before, after_instrument=after_instrument, after_sync=after_sync, after=alive(),
    started=started, spans=spans,
)))
""".format(model=STREAM_MODEL)


def _thread_run(tmp_path, mode, streams):
    script = tmp_path / "threads_{0}.py".format(mode)
    script.write_text(THREADS_SCRIPT)
    with FakeReplicate() as fake:
        fake.read_delay = 5.0  # a read, if one were made, would still be running at the end
        result = subprocess.run(
            [sys.executable, str(script)],
            env=_env(fake, MODE=mode, STREAMS=str(streams)),
            capture_output=True,
            timeout=120,
        )
        gets = fake.paths("GET")
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    found = re.search(r"RESULT=(\{.*\})", result.stdout.decode())
    return json.loads(found.group(1)), gets


def test_by_default_no_thread_is_ever_started(tmp_path):
    streams = 5
    run, gets = _thread_run(tmp_path, "instrumented", streams)
    plain, plain_gets = _thread_run(tmp_path, "plain", streams)

    # Only the main thread, before instrument(), after it, after the streams and at the end.
    assert run["before"] == run["after_instrument"] == run["after_sync"] == run["after"] == ["MainThread"], run
    # The same thread starts as the same streams uninstrumented (asyncio's own
    # executor, if it resolves anything): none of them the package's.
    assert collections.Counter(run["started"]) == collections.Counter(plain["started"]), (run, plain)
    assert [name for name in run["started"] if name.startswith("traceai-replicate")] == []
    assert run["spans"] == 2 * streams
    # No request after a stream: the event streams only, as uninstrumented.
    assert gets == plain_gets
    assert [path for path in gets if path.startswith("/v1/predictions/")] == []


def test_by_default_instrument_starts_no_worker_thread(monkeypatch):
    provider = TracerProvider()  # its resource detection starts the SDK's own threads
    started = []
    original = threading.Thread.start

    def start(self):
        started.append(self.name)
        return original(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    before = set(threading.enumerate())
    instrumentor = ReplicateInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        during = set(threading.enumerate())
        assert instrumentor._reads.budget == 0
    finally:
        instrumentor.uninstrument()
    assert started == []
    assert during == before


# -- default: the span ends at done, on the caller's thread ----------------------------------------


@pytest.mark.parametrize("client_kind", ["default-transport", "custom-transport"])
def test_by_default_a_stream_span_ends_at_done_on_the_callers_thread(client_kind):
    with FakeReplicate() as fake, instrumented() as traced:
        fake.read_delay = 5.0
        threads = _EndThreads()
        traced.provider.add_span_processor(threads)
        transport = RecordingTransport()
        client = default_client(fake) if client_kind == "default-transport" else make_client(fake, transport)
        stream = client.stream(STREAM_MODEL, input={})
        at_done = None
        for event in stream:
            if _is_done(event):
                at_done = (time.time_ns(), list(traced.replicate_spans()))
        exited = time.time_ns()
        spans = traced.replicate_spans()

    returned_ns, ended_by_then = at_done
    assert len(ended_by_then) == 1  # ended before next() handed done back
    span = ended_by_then[0]
    assert span.end_time <= returned_ns <= exited
    assert spans == ended_by_then
    assert threads.threads == [("replicate.stream", threading.current_thread().name)]
    values = _completed(span)
    _no_final(values)
    assert "replicate.stream.read" not in values
    assert "replicate.stream.skip_reason" not in values
    assert fake.paths("GET") == ["/stream/pred0001"]
    if client_kind == "custom-transport":
        assert [method for method, _ in transport.requests] == ["POST", "GET"]


@pytest.mark.parametrize("client_kind", ["default-transport", "custom-transport"])
def test_by_default_an_async_stream_span_ends_at_done_on_the_callers_thread(client_kind):
    async def consume(client, traced):
        at_done = None
        async for event in await client.async_stream(STREAM_MODEL, input={}):
            if _is_done(event):
                at_done = (time.time_ns(), list(traced.replicate_spans()))
        others = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        return at_done, others

    with FakeReplicate() as fake, instrumented() as traced:
        fake.read_delay = 5.0
        threads = _EndThreads()
        traced.provider.add_span_processor(threads)
        transport = RecordingTransport()
        client = default_client(fake) if client_kind == "default-transport" else make_client(fake, transport)
        (returned_ns, ended_by_then), others = asyncio.run(consume(client, traced))

    assert len(ended_by_then) == 1
    assert ended_by_then[0].end_time <= returned_ns
    assert others == []  # nothing of the package's on the caller's loop
    assert threads.threads == [("replicate.stream", threading.current_thread().name)]
    values = _completed(ended_by_then[0])
    _no_final(values)
    assert "replicate.stream.read" not in values
    assert fake.paths("GET") == ["/stream/pred0001"]


# -- default: no wait in flush, shutdown, uninstrument, exit or fork --------------------------------


def test_by_default_flush_uninstrument_and_shutdown_add_no_wait():
    async def consume(client):
        return [event async for event in await client.async_stream(STREAM_MODEL, input={})]

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=60_000))
    instrumentor = ReplicateInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    took = {}
    try:
        with FakeReplicate() as fake:
            fake.read_delay = 5.0
            list(default_client(fake).stream(STREAM_MODEL, input={}))
            asyncio.run(consume(default_client(fake)))
            for name, call in (
                ("force_flush", provider.force_flush),
                ("uninstrument", instrumentor.uninstrument),
                ("shutdown", provider.shutdown),
            ):
                started = time.monotonic()
                call()
                took[name] = time.monotonic() - started
            gets = fake.paths("GET")
    finally:
        instrumentor.uninstrument()
    assert max(took.values()) < NO_WAIT, took
    spans = [span for span in exporter.get_finished_spans() if span.name == "replicate.stream"]
    assert len(spans) == 2
    for span in spans:
        values = _completed(span)
        _no_final(values)
        assert "replicate.stream.read" not in values
    assert _reads(fake) == [] and len(gets) == 2


EXIT_SCRIPT = """
import asyncio, atexit, os, sys, time

LOOP_EXIT = [time.monotonic()]


def report():  # registered first, so it runs last: after the provider's shutdown
    sys.stderr.write("EXIT_AFTER_LOOP=%.3f\\n" % (time.monotonic() - LOOP_EXIT[0]))


atexit.register(report)

import replicate
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_replicate import ReplicateInstrumentor

provider = register(project_name="replicate-exit-default", project_type=ProjectType.OBSERVE, verbose=False)
ReplicateInstrumentor().instrument(tracer_provider=provider)
client = replicate.Client(base_url=os.environ["REPLICATE_BASE_URL"])
if os.environ["MODE"] == "sync":
    for event in client.stream("{model}", input={{}}):
        pass
else:
    async def main():
        async for event in await client.async_stream("{model}", input={{}}):
            pass

    asyncio.run(main())
LOOP_EXIT[0] = time.monotonic()
""".format(model=STREAM_MODEL)


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_by_default_exit_right_after_a_stream_adds_no_wait(tmp_path, mode):
    pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")
    from harness import Receiver, _flatten_attributes, run

    script = tmp_path / "exit_default.py"
    script.write_text(EXIT_SCRIPT)
    with FakeReplicate() as fake, Receiver() as receiver:
        fake.read_delay = 5.0
        env = _env(
            fake,
            MODE=mode,
            FI_BASE_URL=receiver.origin,
            FI_API_KEY="placeholder-fi-api-key",
            FI_SECRET_KEY="placeholder-fi-secret-key",
            REPLICATE_API_TOKEN="r8_exit-placeholder-token",
        )
        result = run([sys.executable, str(script)], env, None, timeout=120)
        spans = receiver.spans()
        gets = fake.paths("GET")

    stderr = result.stderr.decode(errors="replace")
    assert not result.timed_out
    assert result.returncode == 0, stderr
    exit_after_loop = float(re.search(r"EXIT_AFTER_LOOP=([0-9.]+)", stderr).group(1))
    # The export to the receiver only: the base waited for the read (2 s).
    assert exit_after_loop < 1.0, exit_after_loop
    assert [span["name"] for span in spans] == ["replicate.stream"]
    values = _flatten_attributes(spans[0]["attributes"])
    assert spans[0]["status"]["code"] == "STATUS_CODE_OK"
    assert values["output.value"] == STREAM_TEXT
    assert "replicate.stream.read" not in values
    assert "replicate.prediction.status" not in values
    assert gets == ["/stream/pred0001"]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_by_default_a_fork_after_a_stream_adds_no_wait():
    with FakeReplicate() as fake, instrumented() as traced:
        list(default_client(fake).stream(STREAM_MODEL, input={}))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # fork with threads (3.12+)
            started = time.monotonic()
            pid = os.fork()
        if pid == 0:  # pragma: no cover - the child
            os._exit(0)
        took = time.monotonic() - started
        _, status = os.waitpid(pid, 0)
        assert len(traced.replicate_spans()) == 1
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert took < NO_WAIT, took


# -- opt-in: a custom transport is never called for the read -----------------------------------------


class _Logging(httpx.BaseTransport):
    """A caller's wrapper around a plain transport (logging, metrics, auth)."""

    def __init__(self):
        self.inner = httpx.HTTPTransport()
        self.calls = []

    def handle_request(self, request):
        self.calls.append((request.method, request.url.path))
        return self.inner.handle_request(request)

    def close(self):
        self.inner.close()


class _Subclassed(httpx.HTTPTransport):
    """An httpx transport of the caller's own kind."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    def handle_request(self, request):
        self.calls.append((request.method, request.url.path))
        return super().handle_request(request)


class _AsyncOnly(httpx.AsyncBaseTransport):
    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport()
        self.calls = []

    async def handle_async_request(self, request):
        self.calls.append((request.method, request.url.path))
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        await self.inner.aclose()


def _custom(kind):
    if kind == "recording":
        transport = RecordingTransport()
        return transport, lambda: [(method, httpx.URL(url).path) for method, url in transport.requests]
    if kind == "logging-wrapper":
        transport = _Logging()
        return transport, lambda: transport.calls
    if kind == "subclassed":
        transport = _Subclassed()
        return transport, lambda: transport.calls
    if kind == "retry-around-logging":
        inner = _Logging()
        return RetryTransport(inner, max_attempts=3), lambda: inner.calls
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["recording", "logging-wrapper", "subclassed", "retry-around-logging"])
def test_with_the_read_on_a_custom_transport_is_never_called_for_the_read(kind):
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        transport, calls = _custom(kind)
        client = make_client(fake, transport)
        events = list(client.stream(STREAM_MODEL, input={}))
        span = traced.one()  # ended at done: no read to wait for
        seen = calls()

    assert _texts(events) == STREAM_CHUNKS
    assert [path for _, path in seen if path.startswith("/v1/predictions/")] == []
    assert seen == [("POST", "/v1/models/acme/stream-model/predictions"), ("GET", "/stream/pred0001")]
    assert _reads(fake) == []
    values = _completed(span)
    _no_final(values)
    _skipped(values, "custom_transport")


def test_with_the_read_on_an_async_only_transport_is_never_called_for_the_read():
    async def consume(client):
        events = [event async for event in await client.async_stream(STREAM_MODEL, input={})]
        others = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        return events, others

    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        transport = _AsyncOnly()
        events, others = asyncio.run(consume(make_client(fake, transport)))
        span = traced.one()

    assert _texts(events) == STREAM_CHUNKS
    assert others == []  # no read task on the caller's loop
    assert transport.calls == [("POST", "/v1/models/acme/stream-model/predictions"), ("GET", "/stream/pred0001")]
    assert _reads(fake) == []
    _skipped(_completed(span), "custom_transport")


SSE = b"event: output\nid: 1\ndata: hello\n\nevent: done\nid: 2\ndata: {}\n\n"


def _mock_payloads(base):
    created = {
        "id": "abc123",
        "model": "acme/llm",
        "version": "v1",
        "status": "starting",
        "input": {},
        "output": None,
        "logs": "",
        "error": None,
        "metrics": {},
        "created_at": "2026-10-20T00:00:00Z",
        "urls": {
            "get": base + "/v1/predictions/abc123",
            "cancel": base + "/v1/predictions/abc123/cancel",
            "stream": base + "/stream/abc123",
        },
    }
    final = dict(created, status="succeeded", output=["hello"], logs="APP-POLL")
    final["metrics"] = {"input_token_count": 5, "output_token_count": 1}
    return created, final


def test_with_the_read_on_a_scripted_mock_transport_keeps_every_response_for_the_app():
    """A test double that serves the responses its own code needs, in order
    (round-3 caller probe ``mock_transport.py`` b): the package must never
    take one. Round 3 read through it and the app's own get() then raised."""
    base = "http://127.0.0.1:1"
    created, final = _mock_payloads(base)
    script = collections.deque(
        [
            httpx.Response(201, json=created),
            httpx.Response(200, content=SSE, headers={"content-type": "text/event-stream"}),
            httpx.Response(200, json=final),  # for the app's own predictions.get()
        ]
    )
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        return script.popleft()

    with instrumented(**READ_ON) as traced:
        client = replicate.Client(api_token=API_TOKEN, base_url=base, transport=httpx.MockTransport(handler))
        events = [str(event) for event in client.stream("acme/llm", input={})]
        time.sleep(0.2)  # the app does something, then polls the prediction itself
        prediction = client.predictions.get("abc123")
        span = traced.one()

    assert "hello" in events
    assert (prediction.status, prediction.logs) == ("succeeded", "APP-POLL")
    assert calls == [
        ("POST", "/v1/models/acme/llm/predictions"),
        ("GET", "/stream/abc123"),
        ("GET", "/v1/predictions/abc123"),
    ]
    _skipped(attrs(span), "custom_transport")


def test_with_the_read_on_a_mock_transport_with_an_async_handler_is_never_called():
    """Round-3 caller probe ``mock_transport.py`` a: an async handler behind
    httpx.MockTransport; round 3 called it synchronously and left a coroutine
    that was never awaited."""
    base = "http://127.0.0.1:1"
    created, final = _mock_payloads(base)
    seen = []

    async def handler(request):
        seen.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(201, json=created)
        if request.url.path.startswith("/stream/"):
            return httpx.Response(200, content=SSE, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=final)

    async def consume(client):
        return [str(event) async for event in await client.async_stream("acme/llm", input={})]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with instrumented(**READ_ON) as traced:
            client = replicate.Client(api_token=API_TOKEN, base_url=base, transport=httpx.MockTransport(handler))
            events = asyncio.run(consume(client))
            time.sleep(0.2)
            span = traced.one()
    never_awaited = [str(w.message) for w in caught if issubclass(w.category, RuntimeWarning)]

    assert "hello" in events
    assert seen == [("POST", "/v1/models/acme/llm/predictions"), ("GET", "/stream/abc123")]
    assert never_awaited == []
    _skipped(attrs(span), "custom_transport")


def test_with_the_read_on_a_proxied_transport_is_not_read_through():
    client = replicate.Client(api_token=API_TOKEN, transport=httpx.HTTPTransport(proxy="http://127.0.0.1:9"))
    http = client._client  # built lazily; sends nothing
    url = _stream_read._read_url(http, "pred0001")
    assert _stream_read._read_routing(client, http, url) is None
    plain = replicate.Client(api_token=API_TOKEN, transport=httpx.HTTPTransport(retries=1))
    assert _stream_read._read_routing(plain, plain._client, url)[0] == "transport"
    default = replicate.Client(api_token=API_TOKEN)
    assert _stream_read._read_routing(default, default._client, url)[0] == "client"


# -- opt-in: plain transports are read through the package's pool ----------------------------------------


@pytest.mark.parametrize("kind", ["default", "HTTPTransport", "HTTPTransport-retries", "AsyncHTTPTransport-retries"])
def test_with_the_read_on_plain_transports_are_read_through_the_package_pool(kind):
    transport = None
    if kind == "HTTPTransport":
        transport = httpx.HTTPTransport()
    elif kind == "HTTPTransport-retries":
        transport = httpx.HTTPTransport(retries=1)
    elif kind == "AsyncHTTPTransport-retries":
        transport = httpx.AsyncHTTPTransport(retries=2)

    async def consume(client):
        return [event async for event in await client.async_stream(STREAM_MODEL, input={})]

    with FakeReplicate() as fake, instrumented(**READ_ON) as traced, own_sends() as sent:
        options = {} if transport is None else {"transport": transport}
        client = default_client(fake, **options)
        if kind.startswith("Async"):
            events = asyncio.run(consume(client))
        else:
            events = list(client.stream(STREAM_MODEL, input={}))
        traced.settle()
        span = traced.one()

    assert _texts(events) == STREAM_CHUNKS
    assert _reads(fake) == [READ]  # the read reached the API
    assert sent == [("GET", READ)]  # through the package's pool, never the caller's transport
    values = _completed(span)
    _final(values)
    assert "replicate.stream.read" not in values


# -- opt-in: shutdown and uninstrument end pending spans at once ----------------------------------------


@pytest.mark.parametrize("processor", ["simple", "batch"])
def test_with_the_read_on_shutdown_ends_pending_stream_spans_at_once(processor):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(exporter) if processor == "simple" else BatchSpanProcessor(exporter, schedule_delay_millis=60_000)
    )
    instrumentor = ReplicateInstrumentor()
    instrumentor.instrument(tracer_provider=provider, **READ_ON)
    try:
        with FakeReplicate() as fake:
            fake.read_delay = 1.5
            list(default_client(fake).stream(STREAM_MODEL, input={}))
            started = time.monotonic()
            provider.shutdown()
            took = time.monotonic() - started
            spans = [span for span in exporter.get_finished_spans() if span.name == "replicate.stream"]
    finally:
        instrumentor.uninstrument()
    assert took < NO_WAIT, took
    (span,) = spans  # exported by that shutdown
    values = _completed(span)
    _no_final(values)
    assert values["replicate.stream.read"] == "shutdown"


@pytest.mark.parametrize("processor", ["simple", "batch"])
@pytest.mark.parametrize("then", ["shutdown", "force_flush"])
def test_with_the_read_on_uninstrument_ends_pending_spans_at_once_so_a_later_shutdown_exports_them(processor, then):
    """Round-3 caller probe ``uninstrument_then_shutdown.py``: round 3 left the
    span to the read's worker, and a shutdown right after dropped it."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(exporter) if processor == "simple" else BatchSpanProcessor(exporter, schedule_delay_millis=60_000)
    )
    instrumentor = ReplicateInstrumentor()
    instrumentor.instrument(tracer_provider=provider, **READ_ON)
    with FakeReplicate() as fake:
        fake.read_delay = 1.0
        list(default_client(fake).stream(STREAM_MODEL, input={}))
        started = time.monotonic()
        instrumentor.uninstrument()
        getattr(provider, then)()
        took = time.monotonic() - started
        spans = [span for span in exporter.get_finished_spans() if span.name == "replicate.stream"]
        if then == "force_flush":
            provider.shutdown()
    assert took < NO_WAIT, took
    (span,) = spans
    values = _completed(span)
    _no_final(values)
    assert values["replicate.stream.read"] == "uninstrumented"
    assert _stream_read.pending_spans() == 0


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


@pytest.mark.parametrize("call", ["shutdown", "force_flush", "uninstrument"])
def test_with_the_read_on_a_span_the_finisher_is_exporting_is_not_cut_off(call):
    """Round-4 probe ``fork_single.py``: a child that flushed and called
    os._exit() right away lost 2 of 100 spans, because the flush returned
    while the finisher was still exporting one. That one export is waited for."""
    exporter = _SlowExporter(0.4)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = ReplicateInstrumentor()
    instrumentor.instrument(tracer_provider=provider, **READ_ON)
    try:
        with FakeReplicate() as fake:
            list(default_client(fake).stream(STREAM_MODEL, input={}))
            # The read returns at once; the finisher is now exporting its span.
            assert _wait_for(exporter.lock.locked, 2.0)
            started = time.monotonic()
            if call == "uninstrument":
                instrumentor.uninstrument()
            else:
                getattr(provider, call)()
            took = time.monotonic() - started
            exported = list(exporter.spans)  # as the call returned
    finally:
        instrumentor.uninstrument()
    assert [span.name for span in exported] == ["replicate.stream"]
    _final(_completed(exported[0]))
    assert took < 0.4 + 1.0, took  # that one export, nothing more


def test_with_the_read_on_a_stream_completing_after_uninstrument_makes_no_read():
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        stream = default_client(fake).stream(STREAM_MODEL, input={})
        next(stream)
        traced.instrumentor.uninstrument()
        list(stream)
        span = traced.one()
        traced.instrumentor.instrument(tracer_provider=traced.provider)  # the block's end uninstruments
    values = _completed(span)
    _skipped(values, "after_uninstrument")
    assert _reads(fake) == []


def test_with_the_read_on_exit_after_a_stream_adds_no_wait_by_default():
    """The interpreter-exit hook ends pending spans as shutdown does: at once."""
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        fake.read_delay = 1.5
        list(default_client(fake).stream(STREAM_MODEL, input={}))
        started = time.monotonic()
        traced.instrumentor._at_exit()
        took = time.monotonic() - started
        spans = traced.replicate_spans()
    assert took < NO_WAIT, took
    (span,) = spans
    assert attrs(span)["replicate.stream.read"] == "shutdown"


@pytest.mark.parametrize("value, error", [("1", TypeError), (True, TypeError), (-1, ValueError), (10.5, ValueError)])
def test_stream_shutdown_wait_seconds_is_checked(value, error):
    with pytest.raises(error, match="stream_shutdown_wait_seconds"):
        ReplicateInstrumentor().instrument(tracer_provider=TracerProvider(), stream_shutdown_wait_seconds=value)


# -- opt-in: asyncio.run() ----------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["async-only-custom", "plain-AsyncHTTPTransport"])
def test_with_the_read_on_asyncio_run_adds_no_wait(kind):
    marks = {}

    async def main(client):
        events = [event async for event in await client.async_stream(STREAM_MODEL, input={})]
        marks["others"] = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        marks["main"] = time.monotonic()
        return events

    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        fake.read_delay = 1.5
        transport = _AsyncOnly() if kind == "async-only-custom" else httpx.AsyncHTTPTransport(retries=2)
        events = asyncio.run(main(make_client(fake, transport)))
        returned = time.monotonic() - marks["main"]
        traced.settle()
        span = traced.one()

    assert _texts(events) == STREAM_CHUNKS
    assert returned < NO_WAIT, returned
    assert marks["others"] == []
    values = _completed(span)
    if kind == "async-only-custom":
        _skipped(values, "custom_transport")
        assert _reads(fake) == []
    else:
        _final(values)  # read on the package's loop, after asyncio.run() returned


# -- opt-in: the name-lookup queue is bounded -------------------------------------------------------------


def test_with_the_read_on_the_name_lookup_queue_is_bounded(monkeypatch):
    """Round-3 concurrency probe ``dns_queue.py``: with both resolver threads
    hung, queued lookups grew without bound (713 after 12 waves). Past
    _MAX_RESOLVE_JOBS a new lookup fails at once."""
    monkeypatch.setattr(_stream_read, "_MAX_RESOLVE_JOBS", 1, raising=False)
    release = threading.Event()
    original = _stream_read._lookup  # the resolver threads' lookup (socket.getaddrinfo replaced is a socket-level mock)

    def hung(host, *args, **kwargs):
        if threading.current_thread().name == _stream_read.RESOLVER_NAME:
            release.wait(10)
        return original(host, *args, **kwargs)

    monkeypatch.setattr(_stream_read, "_lookup", hung)
    fakes = [FakeReplicate() for _ in range(4)]
    try:
        with instrumented(stream_reload_seconds=3.0) as traced:
            for index, fake in enumerate(fakes[:3]):
                list(default_client(fake, fake.origin.replace("127.0.0.1", "localhost")).stream(STREAM_MODEL, input={}))
                if index < 2:  # each resolver thread takes one lookup and hangs
                    assert _wait_for(lambda: len(_stream_read._STATE.resolver_busy) == index + 1, 2.0)
            assert _wait_for(lambda: _stream_read._STATE.resolve_queued == 1, 2.0)  # the third waits in the queue
            last = fakes[3]
            list(default_client(last, last.origin.replace("127.0.0.1", "localhost")).stream(STREAM_MODEL, input={}))
            ended = time.monotonic()
            assert _wait_for(lambda: len(traced.replicate_spans()) == 1, 1.0)
            took = time.monotonic() - ended
            failed = attrs(traced.replicate_spans()[0])
            queued = _stream_read._STATE.resolve_queued
            release.set()
    finally:
        release.set()
        for fake in fakes:
            fake.close()
    assert took < 1.0, took  # at once, not at its 3 s deadline
    assert failed["replicate.stream.read"] == "failed"
    assert queued <= 1


def _wait_for(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


# -- the documented causes ------------------------------------------------------------------------------


def test_the_readme_lists_every_skip_reason_and_read_value():
    readme = (PACKAGE / "README.md").read_text()
    for value in _stream_read.SKIP_REASONS + (
        _stream_read.SKIPPED,
        _stream_read.TIMEOUT,
        _stream_read.FAILED,
        _stream_read.UNFINISHED,
        _stream_read.FLUSHED,
        _stream_read.SHUTDOWN,
        _stream_read.UNINSTRUMENTED,
    ):
        assert "`{0}`".format(value) in readme, value
