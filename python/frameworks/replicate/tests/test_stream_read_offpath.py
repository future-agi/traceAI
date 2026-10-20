"""The read after a completed stream runs off the caller's path (TH-8320 review).

A stream's span is completed at its ``done`` event; the prediction is then
read on a background thread, within ``stream_reload_seconds``, never retried
and with instrumentation suppressed, and the span ends when that read returns
or the budget runs out, as of the stream's end. The caller's iteration is not
held up, a caller's own timeout is not tripped, and closing or dropping a
stream after ``done`` is not a cancellation.

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

import pytest

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

from opentelemetry.trace import StatusCode  # noqa: E402
from replicate.client import RetryTransport  # noqa: E402
from replicate.stream import ServerSentEvent  # noqa: E402
from traceai_replicate import _wrappers  # noqa: E402
from traceai_replicate._wrappers import drain_released  # noqa: E402

from _support import (  # noqa: E402
    INPUT_TOKENS,
    OUTPUT_TOKENS,
    PREDICT_TIME,
    STREAM_CHUNKS,
    STREAM_MODEL,
    STREAM_TEXT,
    FakeReplicate,
    RecordingTransport,
    attrs,
    instrumented,
    make_client,
)

READ = "/v1/predictions/pred0001"
# What the caller may see between the last event and the end of its loop: a
# thread start, nothing that waits on the network.
CALLER_LATENCY = 0.1
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
    with FakeReplicate() as fake, instrumented() as traced:
        fake.read_delay = 1.5  # inside the default 2 s budget: the read succeeds, late
        events, total, tail = _timed(make_client(fake).stream(STREAM_MODEL, input={}))
        assert tail < CALLER_LATENCY, tail
        assert total < 1.0, total
        assert traced.replicate_spans() == []  # the read is still running
        traced.provider.force_flush()  # waits for it

    assert _texts(events) == STREAM_CHUNKS
    _final(_completed(traced.one()))


def test_a_slow_read_adds_no_latency_to_the_callers_async_loop():
    async def consume(client):
        return await _atimed(await client.async_stream(STREAM_MODEL, input={}))

    with FakeReplicate() as fake, instrumented() as traced:
        fake.read_delay = 1.5
        events, total, tail = asyncio.run(consume(make_client(fake)))
        assert tail < CALLER_LATENCY, tail
        assert total < 1.0, total
        traced.provider.force_flush()

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

    with FakeReplicate() as fake, instrumented() as traced:
        fake.read_delay = 5.0  # longer than the caller's timeout and the 2 s budget
        events = asyncio.run(main(make_client(fake)))

    assert _texts(events) == STREAM_CHUNKS
    _no_final(_completed(traced.one()))


# -- done, then close / drop / keep --------------------------------------------------------


@pytest.mark.parametrize("after_done", ["close", "drop", "keep"])
def test_breaking_at_done_is_a_completed_stream_with_its_output_and_the_read(after_done):
    with FakeReplicate() as fake, instrumented() as traced:
        stream = make_client(fake).stream(STREAM_MODEL, input={})
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
        traced.provider.force_flush()
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

    with FakeReplicate() as fake, instrumented() as traced:
        events = asyncio.run(consume(make_client(fake)))
        traced.provider.force_flush()
        spans = traced.replicate_spans()

    assert _texts(events) == STREAM_CHUNKS
    assert len(spans) == 1
    _final(_completed(spans[0]))


def test_a_stream_closed_before_done_is_still_cancelled_and_not_read():
    with FakeReplicate() as fake, instrumented() as traced:
        stream = make_client(fake).stream(STREAM_MODEL, input={})
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
        _, total, tail = _timed(make_client(fake).stream(STREAM_MODEL, input={}))
        exited_ns, exited = time.time_ns(), time.monotonic()
        assert tail < CALLER_LATENCY, tail
        # No flush: the finisher ends the span when the budget runs out.
        assert _wait_for(traced.replicate_spans, budget + EPSILON)
        ended_after = time.monotonic() - exited
        span = traced.one()

    assert budget / 2 <= ended_after < budget + EPSILON, ended_after
    assert started_ns < span.end_time <= exited_ns  # the stream's end, not the budget's
    _no_final(_completed(span))


@pytest.mark.parametrize(
    "status, retry_after", [(429, "30"), (503, None)], ids=["429-retry-after-30", "503"]
)
def test_a_refused_read_is_not_retried_and_its_threads_are_gone_within_the_budget(status, retry_after):
    budget = 1.0
    with FakeReplicate() as fake, instrumented(stream_reload_seconds=budget) as traced:
        fake.read_status = status
        fake.read_retry_after = retry_after
        _, _, tail = _timed(make_client(fake).stream(STREAM_MODEL, input={}))
        exited = time.monotonic()
        assert tail < CALLER_LATENCY, tail
        assert _wait_for(lambda: not _traceai_threads(), budget + EPSILON), _traceai_threads()
        assert time.monotonic() - exited < budget + EPSILON
        assert len(traced.replicate_spans()) == 1  # ended with the refusal, no flush

    assert fake.paths("GET").count(READ) == 1  # one attempt; Retry-After is not slept on
    _no_final(_completed(traced.one()))


def test_a_callers_own_retry_transport_does_not_retry_the_read_either():
    with FakeReplicate() as fake, instrumented() as traced:
        fake.read_status = 503
        transport = RetryTransport(RecordingTransport(), max_attempts=5, backoff_factor=0.5)
        list(make_client(fake, transport).stream(STREAM_MODEL, input={}))
        traced.provider.force_flush()

    assert fake.paths("GET").count(READ) == 1
    _no_final(_completed(traced.one()))


def test_past_the_cap_on_reads_in_flight_a_stream_ends_at_once_without_one(monkeypatch):
    monkeypatch.setattr(_wrappers, "_MAX_READS", 1)
    with FakeReplicate() as fake, instrumented() as traced:
        fake.read_delay = 1.0
        client = make_client(fake)
        list(client.stream(STREAM_MODEL, input={}))  # its read is in flight
        list(client.stream(STREAM_MODEL, input={}))
        (capped,) = traced.replicate_spans()  # ended at once, no flush
        traced.provider.force_flush()
        spans = traced.replicate_spans()

    assert attrs(capped)["replicate.prediction.id"] == "pred0002"
    _no_final(_completed(capped))
    _final(_completed(spans[1]))
    assert fake.paths("GET").count("/v1/predictions/pred0002") == 0


# -- no span for the read -------------------------------------------------------------------


def test_the_read_adds_no_span_and_runs_with_instrumentation_suppressed():
    with FakeReplicate() as fake, instrumented() as traced:
        transport = RecordingTransport(tracer=traced.tracer())
        list(make_client(fake, transport).stream(STREAM_MODEL, input={}))

    span = traced.one()
    _final(_completed(span))
    http = [s for s in traced.spans() if s.name.startswith("HTTP ")]
    assert [s.name for s in http] == ["HTTP POST", "HTTP GET"]  # the create and the SSE GET
    assert {s.parent.span_id for s in http} == {span.context.span_id}
    assert len(traced.spans()) == 3
    assert ("GET", READ, True) in transport.suppressed
    assert [flag for _, path, flag in transport.suppressed if path != READ] == [False, False]


def test_with_the_real_httpx_instrumentation_the_read_adds_no_span():
    httpx_instrumentation = pytest.importorskip(
        "opentelemetry.instrumentation.httpx", reason="opentelemetry-instrumentation-httpx is optional"
    )
    instrumentor = httpx_instrumentation.HTTPXClientInstrumentor()
    with FakeReplicate() as fake, instrumented() as traced:
        instrumentor.instrument(tracer_provider=traced.provider)
        try:
            list(make_client(fake).stream(STREAM_MODEL, input={}))
            traced.provider.force_flush()
        finally:
            instrumentor.uninstrument()

    span = traced.one()
    _final(_completed(span))
    http = [s for s in traced.spans() if not s.name.startswith("replicate.")]
    urls = [str(value) for s in http for value in attrs(s).values() if "127.0.0.1" in str(value)]
    assert len(http) == 2, [s.name for s in http]  # the create POST and the SSE GET
    assert {s.parent.span_id for s in http} == {span.context.span_id}
    assert not [url for url in urls if url.endswith(READ)]
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

    with FakeReplicate() as fake, instrumented() as traced:
        fake.read_delay = 0.3
        monkeypatch.setattr(threading.Thread, "join", join)
        try:
            events = list(make_client(fake).stream(STREAM_MODEL, input={}))
        except KeyboardInterrupt:  # pytest would stop the whole session on it
            pytest.fail("the caller's loop joined {0}".format(joined))
        finally:
            monkeypatch.undo()
        traced.provider.force_flush()

    assert joined == []
    assert _texts(events) == STREAM_CHUNKS
    _final(_completed(traced.one()))


def test_a_keyboard_interrupt_as_the_read_starts_ends_the_span_then_propagates(monkeypatch):
    original = threading.Thread.start

    def start(self):
        if self.name.startswith("traceai-replicate"):
            raise KeyboardInterrupt
        return original(self)

    with FakeReplicate() as fake, instrumented() as traced:
        stream = make_client(fake).stream(STREAM_MODEL, input={})
        monkeypatch.setattr(threading.Thread, "start", start)
        with pytest.raises(KeyboardInterrupt):
            for _ in stream:
                pass
        monkeypatch.undo()
        spans = traced.replicate_spans()  # ended before the interrupt left the loop

    assert len(spans) == 1
    _no_final(_completed(spans[0]))
    assert READ not in fake.paths("GET")


def test_a_keyboard_interrupt_while_a_flush_waits_for_the_read_ends_the_span_then_propagates(monkeypatch):
    original = threading.Thread.join

    def join(self, timeout=None):
        if self.name == "traceai-replicate-stream-finish":
            raise KeyboardInterrupt
        return original(self, timeout)

    with FakeReplicate() as fake, instrumented() as traced:
        fake.read_delay = 5.0
        list(make_client(fake).stream(STREAM_MODEL, input={}))
        monkeypatch.setattr(threading.Thread, "join", join)
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


def test_200_concurrent_streams_on_threads_end_every_span_and_leave_no_thread():
    budget = 1.0
    with FakeReplicate() as fake, instrumented(stream_reload_seconds=budget) as traced:
        fake.read_delay = CONCURRENT_READ_DELAY  # every read outlives its budget
        clients = [make_client(fake) for _ in range(CONCURRENT)]
        barrier = threading.Barrier(CONCURRENT)
        tails, errors = [], []

        def consume(client):
            try:
                barrier.wait(30)
                events, _, tail = _timed(client.stream(STREAM_MODEL, input={}))
                assert _texts(events) == STREAM_CHUNKS
                tails.append(tail)
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
        # No flush: each span ends when its budget runs out, and its threads go.
        assert _wait_for(lambda: len(traced.replicate_spans()) == CONCURRENT, budget + CONCURRENT_SLACK)
        assert _wait_for(lambda: not _traceai_threads(), budget + CONCURRENT_SLACK), len(_traceai_threads())
        assert time.monotonic() - streamed < budget + CONCURRENT_SLACK

    _assert_all_completed(traced, CONCURRENT)


def test_200_concurrent_async_streams_end_every_span_and_leave_no_thread():
    budget = 1.0

    async def consume(client):
        events, _, tail = await _atimed(await client.async_stream(STREAM_MODEL, input={}))
        assert _texts(events) == STREAM_CHUNKS
        return tail

    async def main(clients):
        return await asyncio.gather(*(consume(client) for client in clients))

    with FakeReplicate() as fake, instrumented(stream_reload_seconds=budget) as traced:
        fake.read_delay = CONCURRENT_READ_DELAY
        clients = [make_client(fake) for _ in range(CONCURRENT)]
        tails = asyncio.run(main(clients))
        streamed = time.monotonic()
        assert max(tails) < 0.5, max(tails)
        assert _wait_for(lambda: len(traced.replicate_spans()) == CONCURRENT, budget + CONCURRENT_SLACK)
        assert _wait_for(lambda: not _traceai_threads(), budget + CONCURRENT_SLACK), len(_traceai_threads())
        assert time.monotonic() - streamed < budget + CONCURRENT_SLACK

    _assert_all_completed(traced, CONCURRENT)


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
    tracer_provider=provider, stream_reload_seconds=float(os.environ["BUDGET"])
)
client = replicate.Client(base_url=os.environ["REPLICATE_BASE_URL"])
last = time.monotonic()
for event in client.stream("{model}", input={{}}):
    last = time.monotonic()
LOOP_EXIT[0] = time.monotonic()
print("TAIL=%.4f" % (LOOP_EXIT[0] - last))
""".format(model=STREAM_MODEL)


@pytest.mark.parametrize(
    "read_delay, budget, read_in_time",
    [(0.3, 2.0, True), (5.0, 0.5, False)],
    ids=["read-returns-during-exit", "budget-runs-out-during-exit"],
)
def test_exit_right_after_a_stream_ends_its_pending_span(tmp_path, read_delay, budget, read_in_time):
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
    assert exit_after_loop < min(read_delay, budget) + EPSILON + 1.0, exit_after_loop
    assert [span["name"] for span in spans] == ["replicate.stream"]
    values = _flatten_attributes(spans[0]["attributes"])
    assert spans[0]["status"]["code"] == "STATUS_CODE_OK"
    assert values["output.value"] == STREAM_TEXT
    if read_in_time:
        assert values["replicate.prediction.status"] == "succeeded"
        assert int(values["gen_ai.usage.output_tokens"]) == OUTPUT_TOKENS
    else:
        assert "replicate.prediction.status" not in values
        assert "gen_ai.usage.output_tokens" not in values
    assert fake.paths("GET") == ["/stream/pred0001", READ]
