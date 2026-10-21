"""The opt-in read after a completed stream runs off the caller's path (TH-8320 review).

A stream's span is completed at its ``done`` event. With
``stream_reload_seconds`` > 0 (the default is 0: no read) the prediction is
then read in the background through the package's own pool, within that
budget, never retried and with instrumentation suppressed, and the span ends
when that read returns or the budget runs out, as of the stream's end. The
caller's iteration is not held up, a caller's own timeout is not tripped,
and closing or dropping a stream after ``done`` is not a cancellation.

Everything runs against the loopback ``FakeReplicate``; no live call is made.
"""

from __future__ import annotations

import asyncio
import gc
import os
import re
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

from opentelemetry.trace import StatusCode  # noqa: E402
from replicate.client import RetryTransport  # noqa: E402
from replicate.stream import ServerSentEvent  # noqa: E402
from traceai_replicate import _stream_read  # noqa: E402
from traceai_replicate._wrappers import drain_released  # noqa: E402

from _support import (  # noqa: E402
    API_TOKEN,
    INPUT_TOKENS,
    OUTPUT_TOKENS,
    PREDICT_TIME,
    STREAM_CHUNKS,
    STREAM_MODEL,
    STREAM_TEXT,
    READ_ON,
    FakeReplicate,
    RecordingTransport,
    attrs,
    default_client,
    instrumentation_suppressed,
    instrumented,
    make_client,
)

READ = "/v1/predictions/pred0001"
# What the caller may see between the last event and the end of its loop: a
# thread start, nothing that waits on the network.
# A correctness bound with generous scheduling slack; this does not assert a benchmark.
CALLER_LATENCY = 0.5
# Slack for thread scheduling on a loaded machine, on top of a budget.
EPSILON = 0.75
CONCURRENT = 200
# 200 streams end 400 threads' worth of spans at about the same time; CI runners are slower.
CONCURRENT_SLACK = 2.0
# Longer than budget + CONCURRENT_SLACK, so a read left running would still be alive.
CONCURRENT_READ_DELAY = 5.0

PACKAGE = Path(__file__).resolve().parents[1]
PYTHON_ROOT = PACKAGE.parents[1]


def _texts(events):
    return [str(event) for event in events if str(event)]


def _is_done(event):
    return event.event == ServerSentEvent.EventType.DONE


def _traceai_threads():
    return [thread for thread in threading.enumerate() if thread.name.startswith("traceai-replicate")]


def _settled():
    """No read's I/O is still running and no stream span waits for one."""
    return _stream_read.live_reads() == 0 and _stream_read.pending_spans() == 0


def _default_client(fake):
    """A client whose transport replicate builds itself: reads go through the package's own pool."""
    return default_client(fake)


def _plain_client(fake):
    """A client given a plain ``httpx.HTTPTransport``: also read through the package's own pool."""
    import replicate

    client = replicate.Client(api_token=API_TOKEN, base_url=fake.origin, transport=httpx.HTTPTransport())
    client.poll_interval = 0.01
    return client


def _wait_for(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _final(values):
    assert values["replicate.prediction.status"] == "succeeded"
    assert values["replicate.metrics.predict_time"] == PREDICT_TIME
    assert values["gen_ai.usage.input_tokens"] == INPUT_TOKENS
    assert values["gen_ai.usage.output_tokens"] == OUTPUT_TOKENS


def _no_final(values):
    for key in (
        "replicate.prediction.status",
        "replicate.metrics.predict_time",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
    ):
        assert key not in values, key


def _completed(span):
    values = attrs(span)
    assert span.status.status_code is StatusCode.OK
    assert values["output.value"] == STREAM_TEXT
    assert "replicate.cancelled" not in values
    return values


def _timed(stream):
    """Iterate a sync stream; (events, seconds from iteration start to loop
    exit, seconds from the last event to loop exit)."""
    events = []
    started = time.monotonic()
    last = started
    for event in stream:
        events.append(event)
        last = time.monotonic()
    exited = time.monotonic()
    return events, exited - started, exited - last


async def _atimed(stream):
    events = []
    started = time.monotonic()
    last = started
    async for event in stream:
        events.append(event)
        last = time.monotonic()
    exited = time.monotonic()
    return events, exited - started, exited - last


# -- no caller-visible latency ------------------------------------------------------------


def test_a_slow_read_adds_no_latency_to_the_callers_loop():
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        fake.read_delay = 1.5  # inside the 2 s budget: the read succeeds, late
        events, total, tail = _timed(_default_client(fake).stream(STREAM_MODEL, input={}))
        assert tail < CALLER_LATENCY, tail
        assert total < 1.0, total
        assert traced.replicate_spans() == []  # the read is still running
        traced.settle()  # waits for it

    assert _texts(events) == STREAM_CHUNKS
    _final(_completed(traced.one()))


def test_a_slow_read_adds_no_latency_to_the_callers_async_loop():
    async def consume(client):
        return await _atimed(await client.async_stream(STREAM_MODEL, input={}))

    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        fake.read_delay = 1.5
        events, total, tail = asyncio.run(consume(_default_client(fake)))
        assert tail < CALLER_LATENCY, tail
        assert total < 1.0, total
        traced.settle()

    assert _texts(events) == STREAM_CHUNKS
    _final(_completed(traced.one()))


def test_a_callers_timeout_around_a_stream_that_finished_in_time_is_not_tripped():
    async def consume(client):
        stream = await client.async_stream(STREAM_MODEL, input={})
        return [event async for event in stream]

    async def main(client):
        if hasattr(asyncio, "timeout"):  # 3.11+
            async with asyncio.timeout(1.0):
                return await consume(client)
        return await asyncio.wait_for(consume(client), 1.0)

    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        fake.read_delay = 5.0  # longer than the caller's timeout and the 2 s budget
        events = asyncio.run(main(_default_client(fake)))
        traced.settle()

    assert _texts(events) == STREAM_CHUNKS
    _no_final(_completed(traced.one()))


# -- done, then close / drop / keep --------------------------------------------------------


@pytest.mark.parametrize("after_done", ["close", "drop", "keep"])
def test_breaking_at_done_is_a_completed_stream_with_its_output_and_the_read(after_done):
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        stream = _default_client(fake).stream(STREAM_MODEL, input={})
        events = []
        for event in stream:
            if _is_done(event):
                break
            events.append(event)
        if after_done == "close":
            stream.close()
        elif after_done == "drop":
            del stream
            gc.collect()
            drain_released()
        traced.settle()
        spans = traced.replicate_spans()  # "keep" still holds the open stream here

    assert _texts(events) == STREAM_CHUNKS
    assert len(spans) == 1
    _final(_completed(spans[0]))
    assert fake.paths("GET") == ["/stream/pred0001", READ]


def test_breaking_at_done_then_aclose_is_a_completed_async_stream():
    async def consume(client):
        stream = await client.async_stream(STREAM_MODEL, input={})
        events = []
        async for event in stream:
            if _is_done(event):
                break
            events.append(event)
        await stream.aclose()
        return events

    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        events = asyncio.run(consume(_default_client(fake)))
        traced.settle()
        spans = traced.replicate_spans()

    assert _texts(events) == STREAM_CHUNKS
    assert len(spans) == 1
    _final(_completed(spans[0]))


def test_a_stream_closed_before_done_is_still_cancelled_and_not_read():
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        stream = _default_client(fake).stream(STREAM_MODEL, input={})
        next(stream)
        stream.close()
        span = traced.one()  # ended by close(), no flush

    assert span.status.status_code is StatusCode.ERROR
    assert attrs(span)["replicate.cancelled"] is True
    assert READ not in fake.paths("GET")


# -- the budget ---------------------------------------------------------------------------


def test_when_the_budget_runs_out_the_span_ends_then_as_of_the_stream_end():
    budget = 0.5
    with FakeReplicate() as fake, instrumented(stream_reload_seconds=budget) as traced:
        fake.read_delay = 5.0
        started_ns = time.time_ns()
        _, total, tail = _timed(_default_client(fake).stream(STREAM_MODEL, input={}))
        exited_ns, exited = time.time_ns(), time.monotonic()
        assert tail < CALLER_LATENCY, tail
        # No flush: the finisher ends the span when the budget runs out.
        assert _wait_for(traced.replicate_spans, budget + EPSILON)
        ended_after = time.monotonic() - exited
        span = traced.one()

    assert budget / 2 <= ended_after < budget + EPSILON, ended_after
    assert started_ns < span.end_time <= exited_ns  # the stream's end, not the budget's
    _no_final(_completed(span))


@pytest.mark.parametrize("route", ["plain-transport", "own-pool"])
@pytest.mark.parametrize(
    "status, retry_after", [(429, "30"), (503, None)], ids=["429-retry-after-30", "503"]
)
def test_a_refused_read_is_not_retried_and_is_over_within_the_budget(status, retry_after, route):
    budget = 1.0
    with FakeReplicate() as fake, instrumented(stream_reload_seconds=budget) as traced:
        fake.read_status = status
        fake.read_retry_after = retry_after
        client = _plain_client(fake) if route == "plain-transport" else _default_client(fake)
        _, _, tail = _timed(client.stream(STREAM_MODEL, input={}))
        exited = time.monotonic()
        assert tail < CALLER_LATENCY, tail
        assert _wait_for(_settled, budget + EPSILON)
        assert time.monotonic() - exited < budget + EPSILON
        assert len(traced.replicate_spans()) == 1  # ended with the refusal, no flush

    assert fake.paths("GET").count(READ) == 1  # one attempt; Retry-After is not slept on
    values = _completed(traced.one())
    _no_final(values)
    assert values["replicate.stream.read"] == "failed"


def test_a_callers_own_retry_transport_does_not_retry_the_read_either():
    """replicate's RetryTransport around a plain transport, passed by the
    caller: the package looks below it, reads through its own pool, once."""
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        fake.read_status = 503
        transport = RetryTransport(httpx.HTTPTransport(), max_attempts=5, backoff_factor=0.5)
        list(make_client(fake, transport).stream(STREAM_MODEL, input={}))
        traced.settle()

    assert fake.paths("GET").count(READ) == 1
    values = _completed(traced.one())
    _no_final(values)
    assert values["replicate.stream.read"] == "failed"


def test_a_retry_transport_around_a_custom_transport_is_never_used_for_the_read():
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        inner = RecordingTransport()
        transport = RetryTransport(inner, max_attempts=5, backoff_factor=0.5)
        list(make_client(fake, transport).stream(STREAM_MODEL, input={}))
        traced.settle()

    assert READ not in fake.paths("GET")
    assert [url for _, url in inner.requests if url.endswith(READ)] == []
    values = _completed(traced.one())
    _no_final(values)
    assert values["replicate.stream.read"] == "skipped"
    assert values["replicate.stream.skip_reason"] == "custom_transport"


def test_past_the_cap_on_reads_in_flight_a_stream_ends_at_once_without_one(monkeypatch):
    monkeypatch.setattr(_stream_read, "_MAX_READS", 1)
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        fake.read_delay = 1.0
        client = _default_client(fake)
        list(client.stream(STREAM_MODEL, input={}))  # its read is in flight
        list(client.stream(STREAM_MODEL, input={}))
        (capped,) = traced.replicate_spans()  # ended at once, no flush
        traced.settle()
        spans = traced.replicate_spans()

    assert attrs(capped)["replicate.prediction.id"] == "pred0002"
    _no_final(_completed(capped))
    assert attrs(capped)["replicate.stream.read"] == "skipped"
    assert attrs(capped)["replicate.stream.skip_reason"] == "at_capacity"
    _final(_completed(spans[1]))
    assert fake.paths("GET").count("/v1/predictions/pred0002") == 0


# -- no span for the read -------------------------------------------------------------------


def test_the_read_adds_no_span_and_runs_with_instrumentation_suppressed(monkeypatch):
    seen = []
    original = _stream_read._send_own

    async def recording(entry, request):
        seen.append((request.url.path, instrumentation_suppressed()))
        return await original(entry, request)

    monkeypatch.setattr(_stream_read, "_send_own", recording)
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        list(_default_client(fake).stream(STREAM_MODEL, input={}))
        traced.settle()

    span = traced.one()
    _final(_completed(span))
    assert len(traced.spans()) == 1
    assert seen == [(READ, True)]


@pytest.mark.parametrize("route", ["caller-transport", "own-pool", "own-pool-async"])
def test_with_the_real_httpx_instrumentation_the_read_adds_no_span(route):
    httpx_instrumentation = pytest.importorskip(
        "opentelemetry.instrumentation.httpx", reason="opentelemetry-instrumentation-httpx is optional"
    )
    instrumentor = httpx_instrumentation.HTTPXClientInstrumentor()

    async def consume(client):
        return [event async for event in await client.async_stream(STREAM_MODEL, input={})]

    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        instrumentor.instrument(tracer_provider=traced.provider)
        try:
            if route == "caller-transport":
                list(make_client(fake).stream(STREAM_MODEL, input={}))
            elif route == "own-pool":
                list(_default_client(fake).stream(STREAM_MODEL, input={}))
            else:
                asyncio.run(consume(_default_client(fake)))
            traced.settle()
        finally:
            instrumentor.uninstrument()

    span = traced.one()
    values = _completed(span)
    http = [s for s in traced.spans() if not s.name.startswith("replicate.")]
    urls = [str(value) for s in http for value in attrs(s).values() if "127.0.0.1" in str(value)]
    assert {s.parent.span_id for s in http} == {span.context.span_id}
    assert not [url for url in urls if url.endswith(READ)]
    if route == "caller-transport":  # a custom transport: no read at all
        _no_final(values)
        assert values["replicate.stream.skip_reason"] == "custom_transport"
        assert fake.paths("GET") == ["/stream/pred0001"]
        return
    _final(values)
    assert len(http) == 2, [s.name for s in http]  # the create POST and the SSE GET
    assert fake.paths("GET") == ["/stream/pred0001", READ]


# -- KeyboardInterrupt -------------------------------------------------------------------


def test_no_thread_is_joined_on_the_callers_path(monkeypatch):
    joined = []
    original = threading.Thread.join

    def join(self, timeout=None):
        if self.name.startswith("traceai-replicate") and threading.current_thread() is threading.main_thread():
            joined.append(self.name)
            raise KeyboardInterrupt  # what a Ctrl-C during that join would do
        return original(self, timeout)

    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        fake.read_delay = 0.3
        monkeypatch.setattr(threading.Thread, "join", join)
        try:
            events = list(_default_client(fake).stream(STREAM_MODEL, input={}))
        except KeyboardInterrupt:  # pytest would stop the whole session on it
            pytest.fail("the caller's loop joined {0}".format(joined))
        finally:
            monkeypatch.undo()
        traced.settle()

    assert joined == []
    assert _texts(events) == STREAM_CHUNKS
    _final(_completed(traced.one()))


def test_a_keyboard_interrupt_as_the_read_starts_ends_the_span_then_propagates(monkeypatch):
    def hand_over(entry):
        raise KeyboardInterrupt  # what a Ctrl-C while the stream is handed over would do

    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        stream = _default_client(fake).stream(STREAM_MODEL, input={})
        monkeypatch.setattr(_stream_read, "_hand_over", hand_over)
        with pytest.raises(KeyboardInterrupt):
            for _ in stream:
                pass
        monkeypatch.undo()
        spans = traced.replicate_spans()  # ended before the interrupt left the loop

    assert len(spans) == 1
    values = _completed(spans[0])
    _no_final(values)
    assert (values["replicate.stream.read"], values["replicate.stream.skip_reason"]) == ("skipped", "interrupted")
    assert READ not in fake.paths("GET")
    assert _stream_read.live_reads() == 0  # its slot was given back


def test_a_keyboard_interrupt_while_a_flush_waits_for_the_read_ends_the_span_then_propagates(monkeypatch):
    def wait(cond, timeout):
        raise KeyboardInterrupt  # a Ctrl-C while force_flush waits

    with FakeReplicate() as fake, instrumented(stream_flush_wait_seconds=5, **READ_ON) as traced:
        fake.read_delay = 5.0
        list(_default_client(fake).stream(STREAM_MODEL, input={}))
        monkeypatch.setattr(_stream_read, "_wait", wait)
        with pytest.raises(KeyboardInterrupt):
            traced.provider.force_flush()
        monkeypatch.undo()
        spans = traced.replicate_spans()

    assert len(spans) == 1
    _no_final(_completed(spans[0]))


# -- 200 concurrent streams ---------------------------------------------------------------


def _assert_all_completed(traced, count):
    spans = traced.replicate_spans()
    assert len(spans) == count
    assert len({attrs(span)["replicate.prediction.id"] for span in spans}) == count
    for span in spans:
        _no_final(_completed(span))


def test_200_concurrent_streams_on_threads_end_every_span_and_leave_no_read_running():
    budget = 1.0
    with FakeReplicate() as fake, instrumented(stream_reload_seconds=budget) as traced:
        fake.read_delay = CONCURRENT_READ_DELAY  # every read outlives its budget
        clients = [_default_client(fake) for _ in range(CONCURRENT)]
        barrier = threading.Barrier(CONCURRENT)
        tails, errors = [], []
        peak = [0]

        def consume(client):
            try:
                barrier.wait(30)
                events, _, tail = _timed(client.stream(STREAM_MODEL, input={}))
                assert _texts(events) == STREAM_CHUNKS
                tails.append(tail)
                peak[0] = max(peak[0], _stream_read.live_reads())
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(error)

        workers = [threading.Thread(target=consume, args=(client,)) for client in clients]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(60)
        streamed = time.monotonic()
        assert errors == []
        assert max(tails) < 0.5, max(tails)
        # No flush: each span ends when its budget runs out, and each read is
        # cancelled at its deadline (its connection closed), not left running.
        assert _wait_for(lambda: len(traced.replicate_spans()) == CONCURRENT, budget + CONCURRENT_SLACK)
        assert _wait_for(_settled, budget + CONCURRENT_SLACK), _stream_read.live_reads()
        assert time.monotonic() - streamed < budget + CONCURRENT_SLACK
        assert peak[0] <= _stream_read._MAX_READS

    _assert_all_completed(traced, CONCURRENT)


def test_200_concurrent_async_streams_end_every_span_and_leave_no_read_running():
    budget = 1.0

    peak = [0]

    async def consume(client):
        events, _, tail = await _atimed(await client.async_stream(STREAM_MODEL, input={}))
        assert _texts(events) == STREAM_CHUNKS
        peak[0] = max(peak[0], _stream_read.live_reads())
        return tail

    async def main(clients):
        return await asyncio.gather(*(consume(client) for client in clients))

    with FakeReplicate() as fake, instrumented(stream_reload_seconds=budget) as traced:
        fake.read_delay = CONCURRENT_READ_DELAY
        # One client, as an app has: 200 clients would each build an httpx
        # client and TLS context on this one event loop, and that set-up (not
        # the instrumentation) would dominate the tails measured here.
        shared = _default_client(fake)
        tails = asyncio.run(main([shared] * CONCURRENT))
        streamed = time.monotonic()
        assert max(tails) < 1.5, max(tails)
        assert peak[0] <= _stream_read._MAX_READS
        assert _wait_for(lambda: len(traced.replicate_spans()) == CONCURRENT, budget + CONCURRENT_SLACK)
        assert _wait_for(_settled, budget + CONCURRENT_SLACK), _stream_read.live_reads()
        assert time.monotonic() - streamed < budget + CONCURRENT_SLACK

    _assert_all_completed(traced, CONCURRENT)
    # Past the cap a stream ends without a read, and says so.
    reads = [attrs(span).get("replicate.stream.read") for span in traced.replicate_spans()]
    assert set(reads) <= {"timeout", "skipped"}


# -- interpreter exit ---------------------------------------------------------------------

EXIT_SCRIPT = """
import atexit
import os
import sys
import time

LOOP_EXIT = [time.monotonic()]


def report():  # registered first, so it runs last: after the provider's shutdown
    sys.stderr.write("EXIT_AFTER_LOOP=%.3f\\n" % (time.monotonic() - LOOP_EXIT[0]))


atexit.register(report)

import replicate
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_replicate import ReplicateInstrumentor

provider = register(project_name="replicate-exit-stream", project_type=ProjectType.OBSERVE, verbose=False)
ReplicateInstrumentor().instrument(
    tracer_provider=provider,
    stream_reload_seconds=float(os.environ["BUDGET"]),
    stream_shutdown_wait_seconds=float(os.environ["SHUTDOWN_WAIT"]),
)
client = replicate.Client(base_url=os.environ["REPLICATE_BASE_URL"])
last = time.monotonic()
for event in client.stream("{model}", input={{}}):
    last = time.monotonic()
LOOP_EXIT[0] = time.monotonic()
print("TAIL=%.4f" % (LOOP_EXIT[0] - last))
""".format(model=STREAM_MODEL)


@pytest.mark.parametrize(
    "read_delay, budget, shutdown_wait, outcome",
    [(0.3, 2.0, 0.0, "shutdown"), (0.3, 2.0, 2.0, None), (5.0, 0.5, 2.0, "timeout")],
    ids=["exit-does-not-wait-by-default", "read-returns-during-an-opt-in-wait", "budget-runs-out-during-the-wait"],
)
def test_exit_right_after_a_stream_ends_its_pending_span(tmp_path, read_delay, budget, shutdown_wait, outcome):
    pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")
    from harness import Receiver, _flatten_attributes, run

    script = tmp_path / "exit_after_stream.py"
    script.write_text(EXIT_SCRIPT)
    with FakeReplicate() as fake, Receiver() as receiver:
        fake.read_delay = read_delay
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("FI_", "REPLICATE_", "OTEL_"))
        }
        env.update(
            {
                "FI_BASE_URL": receiver.origin,
                "FI_API_KEY": "placeholder-fi-api-key",
                "FI_SECRET_KEY": "placeholder-fi-secret-key",
                "REPLICATE_BASE_URL": fake.origin,
                "REPLICATE_API_TOKEN": "r8_exit-placeholder-token",
                "BUDGET": str(budget),
                "SHUTDOWN_WAIT": str(shutdown_wait),
                "PYTHONPATH": os.pathsep.join(
                    [str(PACKAGE), str(PYTHON_ROOT), os.environ.get("PYTHONPATH", "")]
                ),
            }
        )
        result = run([sys.executable, str(script)], env, None, timeout=120)
        spans = receiver.spans()

    stderr = result.stderr.decode(errors="replace")
    assert not result.timed_out
    assert result.returncode == 0, stderr
    tail = float(re.search(r"TAIL=([0-9.]+)", result.stdout.decode()).group(1))
    exit_after_loop = float(re.search(r"EXIT_AFTER_LOOP=([0-9.]+)", stderr).group(1))
    assert tail < CALLER_LATENCY, tail
    waited = 0.0 if outcome == "shutdown" else min(read_delay, budget, shutdown_wait)
    assert exit_after_loop < waited + EPSILON + 1.0, exit_after_loop
    assert [span["name"] for span in spans] == ["replicate.stream"]
    values = _flatten_attributes(spans[0]["attributes"])
    assert spans[0]["status"]["code"] == "STATUS_CODE_OK"
    assert values["output.value"] == STREAM_TEXT
    if outcome is None:
        assert values["replicate.prediction.status"] == "succeeded"
        assert int(values["gen_ai.usage.output_tokens"]) == OUTPUT_TOKENS
    else:
        assert values["replicate.stream.read"] == outcome
        assert "replicate.prediction.status" not in values
        assert "gen_ai.usage.output_tokens" not in values
    assert fake.paths("GET")[0] == "/stream/pred0001"
