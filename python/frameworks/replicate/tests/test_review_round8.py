"""TH-8320 review round 8.

* A traced call still running when the provider shuts down or the
  interpreter exits ends then, as cancelled (ERROR ``cancelled``,
  ``replicate.cancelled``) and with what it knows, so it is exported before
  the processors stop: ``run()`` / ``async_run()`` while the client polls, a
  create in flight, a ``wait()`` (which before ended OK, with the last polled
  status). Under the SIGTERM/SIGINT handler ``register()`` installs, the span
  of an in-flight ``run()`` / ``async_run()`` used to be lost, while a stream
  at the same moment was exported as cancelled.
* Ctrl-C (``KeyboardInterrupt``) while ``run()``, ``async_run()``,
  ``create()`` or ``wait()`` runs is the caller stopping the call
  (``replicate.cancelled``, no exception event), as it is inside a stream's
  ``next()``.
* A trio cancellation of an async stream (``trio.Cancelled``, also what anyio
  raises under trio) is the caller stopping it, as an asyncio one is.
* ``throw(GeneratorExit)`` (what a generator's ``close()`` raises inside it)
  is the caller stopping the stream, not an error.
* A ``close()`` from another thread while ``next()`` runs is refused by the
  generator (``ValueError``). The stream goes on in that thread, and so does
  its span: the refused close does not end it.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

import fi_instrumentation  # noqa: E402
from opentelemetry.sdk.trace import SpanProcessor  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402

from _support import (  # noqa: E402
    SLOW_MODEL,
    STREAM_CHUNKS,
    STREAM_MODEL,
    STREAM_SLOW_MODEL,
    TEXT_MODEL,
    FakeReplicate,
    RecordingTransport,
    attrs,
    default_client,
    exception_events,
    instrumented,
    make_client,
)

RUN = "replicate.run"
STREAM = "replicate.stream"
CREATE = "replicate.predictions.create"
FIRST = "pred0001"


def _until(predicate, what, timeout=20.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for " + what)
        time.sleep(0.005)


def _polls(fake, prediction_id=FIRST):
    prediction = fake.predictions.get(prediction_id)
    return prediction.polls if prediction is not None else 0


def _set_status(fake, status, prediction_id=FIRST):
    with fake._lock:
        fake.predictions[prediction_id].status = status


def _cancelled(span, name):
    """Ended as the caller stopping the call: no exception event."""
    values = attrs(span)
    assert span.name == name
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "cancelled"
    assert values.get("replicate.cancelled") is True
    assert exception_events(span) == []
    return values


class _Ends(SpanProcessor):
    """Sees every span end, even after the provider has shut down."""

    def __init__(self):
        self.ended = []

    def on_end(self, span):
        self.ended.append(span)

    def names(self):
        return [span.name for span in self.ended if span.name.startswith("replicate.")]


class _CtrlC(RecordingTransport):
    """Raises ``KeyboardInterrupt`` (what Ctrl-C raises in the main thread)
    inside the client's own call, on the request ``when`` picks."""

    def __init__(self, when):
        super().__init__()
        self._when = when

    def handle_request(self, request):
        self._check(request)
        if self._when(request):
            raise KeyboardInterrupt
        return self._sync.handle_request(request)

    async def handle_async_request(self, request):
        self._check(request)
        if self._when(request):
            raise KeyboardInterrupt
        return await self._async.handle_async_request(request)


def _second_poll():
    polls = []

    def when(request):
        if request.method == "GET" and request.url.path.startswith("/v1/predictions/"):
            polls.append(request.url.path)
            return len(polls) == 2
        return False

    return when


def _a_create(request):
    return request.method == "POST"


# -- Ctrl-C while a call runs ---------------------------------------------------------------


def test_ctrl_c_while_run_polls_is_the_caller_stopping_it():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake, transport=_CtrlC(_second_poll()))
        with pytest.raises(KeyboardInterrupt):
            client.run(SLOW_MODEL, input={})
        span = traced.one()
    values = _cancelled(span, RUN)
    assert values["replicate.prediction.id"] == FIRST
    assert values["replicate.prediction.status"] == "starting"  # the last polled status


def test_ctrl_c_while_async_run_polls_is_the_caller_stopping_it():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake, transport=_CtrlC(_second_poll()))
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(client.async_run(SLOW_MODEL, input={}))
        span = traced.one()
    values = _cancelled(span, RUN)
    assert values["replicate.prediction.id"] == FIRST


def test_ctrl_c_while_a_create_is_sent_is_the_caller_stopping_it():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake, transport=_CtrlC(_a_create))
        with pytest.raises(KeyboardInterrupt):
            client.predictions.create(model=TEXT_MODEL, input={})
        span = traced.one()
    values = _cancelled(span, CREATE)
    assert "replicate.prediction.id" not in values  # the create never answered


def test_ctrl_c_while_wait_polls_ends_the_create_span_as_the_caller_stopping_it():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake, transport=_CtrlC(_second_poll()))
        prediction = client.predictions.create(model=SLOW_MODEL, input={})
        with pytest.raises(KeyboardInterrupt):
            prediction.wait()
        span = traced.one()
    values = _cancelled(span, CREATE)  # the create span the wait continued, not a second one
    assert values["replicate.prediction.id"] == FIRST


# -- a call still running at shutdown, on another thread -----------------------------------------


def _in_thread(target):
    outcome = {}

    def body():
        try:
            outcome["result"] = target()
        except BaseException as error:  # noqa: BLE001 - reported to the test
            outcome["error"] = error

    worker = threading.Thread(target=body, daemon=True)
    worker.start()
    return worker, outcome


@pytest.mark.parametrize("flavour", ["sync", "async"])
def test_a_shutdown_while_run_polls_on_another_thread_exports_its_span_as_cancelled(flavour):
    with FakeReplicate() as fake, instrumented() as traced:
        ends = _Ends()
        traced.provider.add_span_processor(ends)
        client = make_client(fake)
        if flavour == "sync":
            worker, outcome = _in_thread(lambda: client.run(SLOW_MODEL, input={}))
        else:
            worker, outcome = _in_thread(lambda: asyncio.run(client.async_run(SLOW_MODEL, input={})))
        _until(lambda: _polls(fake) >= 2, "run() to poll")
        stopping = time.time_ns()
        traced.provider.shutdown()
        exported = traced.replicate_spans()  # what reached the exporter before it stopped
        _set_status(fake, "canceled")  # lets the client's poll loop return
        worker.join(20)
        assert not worker.is_alive()

    assert "error" not in outcome, outcome
    (span,) = exported
    values = _cancelled(span, RUN)
    assert values["replicate.prediction.id"] == FIRST
    assert values["replicate.prediction.status"] == "starting"  # the last polled status
    assert span.end_time >= stopping
    # The call returning later ended no second span.
    assert ends.names() == [RUN]


def test_a_shutdown_while_a_create_is_in_flight_exports_its_span_and_holds_nothing():
    with FakeReplicate() as fake, instrumented() as traced:
        ends = _Ends()
        traced.provider.add_span_processor(ends)
        fake.create_delay = 1.0
        client = make_client(fake)
        worker, outcome = _in_thread(lambda: client.predictions.create(model=SLOW_MODEL, input={}))
        _until(lambda: fake.paths("POST"), "the create to be sent")
        traced.provider.shutdown()
        exported = traced.replicate_spans()
        worker.join(20)
        assert not worker.is_alive()
        held = dict(traced.instrumentor._registry._calls)

    assert "error" not in outcome, outcome
    (span,) = exported
    values = _cancelled(span, CREATE)
    assert "replicate.prediction.id" not in values  # cut off before the create answered
    # The create returned the client's own prediction: its span had ended, so nothing holds it.
    assert type(outcome["result"]).__name__ == "Prediction"
    assert held == {}
    assert ends.names() == [CREATE]


# -- SIGTERM / SIGINT under register(), and exit, while a call runs ------------------------------

PACKAGE = Path(__file__).resolve().parents[1]
PYTHON_ROOT = PACKAGE.parents[1]
FI_ROOT = Path(fi_instrumentation.__file__).resolve().parents[1]

SCRIPT = """
import asyncio
import os
import threading
import time

import replicate
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_replicate import ReplicateInstrumentor

provider = register(
    project_name="replicate-round8",
    project_type=ProjectType.OBSERVE,
    batch=os.environ["BATCH"] == "1",
    verbose=False,
)
ReplicateInstrumentor().instrument(tracer_provider=provider)
client = replicate.Client(base_url=os.environ["REPLICATE_BASE_URL"])
client.poll_interval = 0.01
MODE = os.environ["MODE"]


def mark(name):
    path = os.environ[name]
    with open(path + ".tmp", "w") as handle:
        handle.write(str(time.time_ns()))
    os.replace(path + ".tmp", path)


def call():
    if MODE == "run":
        client.run("{slow}", input={{}})
    elif MODE == "async_run":
        asyncio.run(client.async_run("{slow}", input={{}}))
    elif MODE == "create":
        client.predictions.create(model="{slow}", input={{}})
    elif MODE == "wait":
        client.predictions.create(model="{slow}", input={{}}).wait()
    elif MODE == "stream":
        for _ in client.stream("{stream_slow}", input={{}}):
            mark("FIRST_FILE")  # the first event; the stand-in then stalls


if os.environ["WHERE"] == "main":
    mark("READY_FILE")
    call()  # the signal arrives while this runs
    print("the call returned")
else:  # the interpreter exits while the call runs on a daemon thread
    threading.Thread(target=call, daemon=True).start()
    mark("READY_FILE")
    while not os.path.exists(os.environ["GO_FILE"]):
        time.sleep(0.01)
""".format(slow=SLOW_MODEL, stream_slow=STREAM_SLOW_MODEL)


def _in_flight(fake, mode, first):
    if mode == "create":
        return bool(fake.paths("POST"))
    if mode == "stream":
        return first.exists()
    return _polls(fake) >= 2


def _drive(tmp_path, fake, receiver, mode, where, batch):
    script = tmp_path / "round8.py"
    script.write_text(SCRIPT)
    ready, first, go = tmp_path / "ready", tmp_path / "first", tmp_path / "go"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("FI_", "REPLICATE_", "OTEL_"))
    }
    env.update(
        {
            "BATCH": "1" if batch else "0",
            "MODE": mode,
            "WHERE": "main" if where != "exit" else "thread",
            "READY_FILE": str(ready),
            "FIRST_FILE": str(first),
            "GO_FILE": str(go),
            "FI_BASE_URL": receiver.origin,
            "FI_API_KEY": "placeholder-fi-api-key",
            "FI_SECRET_KEY": "placeholder-fi-secret-key",
            "REPLICATE_BASE_URL": fake.origin,
            "REPLICATE_API_TOKEN": "r8_round8-placeholder-token",
            "PYTHONPATH": os.pathsep.join(
                [str(PACKAGE), str(FI_ROOT), str(PYTHON_ROOT), os.environ.get("PYTHONPATH", "")]
            ),
        }
    )
    process = subprocess.Popen(
        [sys.executable, str(script)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        _until(lambda: ready.exists() or process.poll() is not None, "the script", 60)
        if not ready.exists():
            process.kill()
            _, stderr = process.communicate()
            pytest.fail("the script never got ready: " + stderr.decode(errors="replace"))
        _until(
            lambda: _in_flight(fake, mode, first) or process.poll() is not None,
            "the call to be in flight",
            60,
        )
        if mode == "stream":
            time.sleep(0.2)  # back inside next(), which the stall blocks
        in_flight = time.time_ns()
        if where == "exit":
            go.write_text("go")
        else:
            process.send_signal(getattr(signal, where))
        _, stderr = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    return process.returncode, stderr.decode(errors="replace"), in_flight


CASES = [
    pytest.param("run", "SIGTERM", True, id="run-SIGTERM-batch"),
    pytest.param("run", "SIGTERM", False, id="run-SIGTERM-simple"),
    pytest.param("run", "SIGINT", True, id="run-SIGINT-batch"),
    pytest.param("async_run", "SIGTERM", True, id="async_run-SIGTERM-batch"),
    pytest.param("async_run", "SIGINT", True, id="async_run-SIGINT-batch"),
    pytest.param("create", "SIGTERM", True, id="create-SIGTERM-batch"),
    pytest.param("wait", "SIGINT", True, id="wait-SIGINT-batch"),
    pytest.param("stream", "SIGINT", True, id="stream-SIGINT-batch"),
    pytest.param("run", "exit", True, id="run-exit-batch"),
    pytest.param("async_run", "exit", True, id="async_run-exit-batch"),
]

EXPECTED = {"run": RUN, "async_run": RUN, "create": CREATE, "wait": CREATE, "stream": STREAM}


@pytest.mark.parametrize("mode, where, batch", CASES)
def test_a_call_in_flight_at_sigterm_sigint_or_exit_under_register_is_exported_as_cancelled(
    tmp_path, mode, where, batch
):
    pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")
    from harness import Receiver, _flatten_attributes

    with FakeReplicate() as fake, Receiver() as receiver:
        if mode == "create":
            fake.create_delay = 30.0  # answered only when the stand-in closes
        returncode, stderr, in_flight = _drive(tmp_path, fake, receiver, mode, where, batch)
        spans = receiver.spans()

    assert returncode == 0, stderr  # register()'s handler exits with 0
    assert [span["name"] for span in spans] == [EXPECTED[mode]], stderr
    (span,) = spans
    values = _flatten_attributes(span["attributes"])
    assert span["status"]["code"] == "STATUS_CODE_ERROR"
    assert span["status"]["message"] == "cancelled"
    assert values["replicate.cancelled"] is True
    assert [event["name"] for event in span.get("events", [])] == []  # no exception event
    assert "gen_ai.span.leaked" not in values
    assert int(span["endTimeUnixNano"]) >= in_flight - 50_000_000  # as of the signal/exit
    if mode == "create":
        assert "replicate.prediction.id" not in values
    else:
        assert values["replicate.prediction.id"] == FIRST
    if mode == "stream":
        assert values["output.value"] == STREAM_CHUNKS[0]  # what it had received
        assert values["gen_ai.span.kind"] == "LLM"


# -- trio -----------------------------------------------------------------------------------------


def test_a_trio_cancellation_of_an_async_stream_is_the_caller_stopping_it():
    trio = pytest.importorskip("trio")

    async def main(client):
        received = []
        with trio.move_on_after(0.5):  # the stand-in stalls after the first event
            async for event in await client.async_stream(STREAM_SLOW_MODEL, input={}):
                received.append(str(event))
        return received

    with FakeReplicate() as fake, instrumented() as traced:
        received = trio.run(main, default_client(fake))
        span = traced.one()

    assert received == STREAM_CHUNKS[:1]
    values = _cancelled(span, STREAM)
    assert values["output.value"] == STREAM_CHUNKS[0]
    assert values["gen_ai.span.kind"] == "LLM"


# -- throw(GeneratorExit) --------------------------------------------------------------------------


def test_throw_generator_exit_into_a_stream_is_the_caller_stopping_it():
    with FakeReplicate() as fake, instrumented() as traced:
        stream = default_client(fake).stream(STREAM_MODEL, input={})
        first = next(stream)
        with pytest.raises(GeneratorExit):
            stream.throw(GeneratorExit)
        span = traced.one()
    assert str(first) == STREAM_CHUNKS[0]
    values = _cancelled(span, STREAM)
    assert values["output.value"] == STREAM_CHUNKS[0]


def test_athrow_generator_exit_into_an_async_stream_is_the_caller_stopping_it():
    async def main(client):
        stream = await client.async_stream(STREAM_MODEL, input={})
        first = await stream.__anext__()
        with pytest.raises(GeneratorExit):
            await stream.athrow(GeneratorExit)
        return first

    with FakeReplicate() as fake, instrumented() as traced:
        first = asyncio.run(main(default_client(fake)))
        span = traced.one()
    assert str(first) == STREAM_CHUNKS[0]
    values = _cancelled(span, STREAM)
    assert values["output.value"] == STREAM_CHUNKS[0]


# -- close() from another thread while next() runs ---------------------------------------------------


def test_a_close_refused_while_another_thread_is_inside_next_leaves_the_span_to_that_stream():
    with FakeReplicate() as fake, instrumented() as traced:
        stream = default_client(fake).stream(STREAM_SLOW_MODEL, input={})
        first = next(stream)
        worker, outcome = _in_thread(lambda: next(stream))  # blocks: the stand-in stalls
        _until(lambda: stream.__wrapped__.gi_running, "the reader to be inside next()")
        with pytest.raises(ValueError, match="already executing"):
            stream.close()
        assert traced.replicate_spans() == []  # the refused close ended nothing
        fake._release.set()  # the stall ends: the response ends without done
        worker.join(20)
        assert not worker.is_alive()
        span = traced.one()

    assert str(first) == STREAM_CHUNKS[0]
    assert isinstance(outcome.get("error"), StopIteration), outcome
    values = attrs(span)
    # The stream ended in the reader's thread: truncated (no done), not cancelled.
    assert span.status.status_code is StatusCode.OK
    assert "replicate.cancelled" not in values
    assert values["replicate.stream.truncated"] is True
    assert values["output.value"] == STREAM_CHUNKS[0]
