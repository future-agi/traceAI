"""A running wait()/cancel() keeps the create span it continues (V2, AC-03).

A wait()/cancel() that continues a held create span claims it. While that
call runs, neither the cap (a traced call on another thread or task) nor
``force_flush()`` (a flush at the end of a request) ends the span, and
neither does a release of the prediction object: the call ends it once, when
it returns or raises, with the final status, output and end time.

Provider shutdown and interpreter exit (including the SIGTERM/SIGINT handler
``register()`` installs) still end it as a best effort: as of then, with the
prediction's last polled status and no output. The call returning later
neither ends it again nor opens a wait span.
"""

from __future__ import annotations

import asyncio
import gc
import logging
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
    TEXT_MODEL,
    TEXT_OUTPUT,
    FakeReplicate,
    RecordingTransport,
    attrs,
    instrumented,
    make_client,
)

CREATE = "replicate.predictions.create"
TRIGGERS = ("cap", "flush")
PAST_THE_CAP = 601  # the default cap is 600 s


# -- helpers ---------------------------------------------------------------------------


def _clock(monkeypatch):
    """Drive the registry's clock by hand, so the cap fires exactly when told."""
    from traceai_replicate import _wrappers

    clock = [1000.0]
    monkeypatch.setattr(_wrappers, "_monotonic", lambda: clock[0])
    return clock


def _until(predicate, what, timeout=20.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for " + what)
        time.sleep(0.005)


async def _until_async(predicate, what, timeout=20.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for " + what)
        await asyncio.sleep(0.005)


def _polls(fake, prediction_id):
    prediction = fake.predictions.get(prediction_id)
    return prediction.polls if prediction is not None else 0


def _set_status(fake, prediction_id, status):
    with fake._lock:
        fake.predictions[prediction_id].status = status


def _ended(traced, prediction_id):
    return [
        span
        for span in traced.replicate_spans()
        if attrs(span).get("replicate.prediction.id") == prediction_id
    ]


class _Background(threading.Thread):
    """Runs one call on another thread and keeps its result or exception."""

    def __init__(self, function):
        super().__init__(daemon=True)
        self._function = function
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = self._function()
        except BaseException as error:  # noqa: BLE001 - reported by the test
            self.error = error


class _HeldCancel(RecordingTransport):
    """Holds every cancel request until ``go`` is set, so a test can act
    while ``cancel()`` is running."""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.go = threading.Event()

    def handle_request(self, request):
        if request.url.path.endswith("/cancel"):
            self.entered.set()
            self.go.wait(20)
        return super().handle_request(request)

    async def handle_async_request(self, request):
        if request.url.path.endswith("/cancel"):
            self.entered.set()
            while not self.go.is_set():
                await asyncio.sleep(0.005)
        return await super().handle_async_request(request)


class _Ends(SpanProcessor):
    """Sees every span end, even after the provider has shut down."""

    def __init__(self):
        self.ended = []

    def on_end(self, span):
        self.ended.append(span)


def _fire(trigger, traced, client, clock):
    if trigger == "cap":
        clock[0] += PAST_THE_CAP
        client.run(TEXT_MODEL, input={})  # a traced call: it touches the registry
    else:
        assert traced.provider.force_flush() is True


async def _fire_async(trigger, traced, client, clock):
    if trigger == "cap":
        clock[0] += PAST_THE_CAP
        await client.async_run(TEXT_MODEL, input={})
    else:
        assert traced.provider.force_flush() is True


def _assert_one_final_create_span(traced, prediction_id, status, fired):
    spans = _ended(traced, prediction_id)
    # One span for create + wait/cancel: no second (wait/cancel) span.
    assert [span.name for span in spans] == [CREATE]
    (span,) = spans
    values = attrs(span)
    assert values["replicate.prediction.status"] == status
    assert span.status.status_code is StatusCode.OK
    assert span.end_time >= fired  # ended when the call returned, not at create time
    return values


# -- wait ------------------------------------------------------------------------------


@pytest.mark.parametrize("trigger", TRIGGERS)
def test_the_cap_or_a_flush_leaves_a_running_wait_its_create_span(monkeypatch, trigger):
    clock = _clock(monkeypatch)
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        idle = client.predictions.create(model=SLOW_MODEL, input={})  # nobody waits on it
        prediction = client.predictions.create(model=SLOW_MODEL, input={})
        waiter = _Background(prediction.wait)
        waiter.start()
        try:
            _until(lambda: _polls(fake, prediction.id) >= 1, "the wait to poll")

            _fire(trigger, traced, client, clock)  # on this thread, while the wait polls
            fired = time.time_ns()
            assert [span.name for span in _ended(traced, idle.id)] == [CREATE]  # still capped
            assert _ended(traced, prediction.id) == []  # claimed by the running wait
        finally:
            _set_status(fake, prediction.id, "succeeded")
            waiter.join(20)
        assert not waiter.is_alive()
        assert waiter.error is None
        assert prediction.status == "succeeded"

    values = _assert_one_final_create_span(traced, prediction.id, "succeeded", fired)
    assert values["output.value"] == TEXT_OUTPUT


@pytest.mark.parametrize("trigger", TRIGGERS)
def test_the_cap_or_a_flush_leaves_a_running_async_wait_its_create_span(monkeypatch, trigger):
    clock = _clock(monkeypatch)
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)

        async def scenario():
            idle = await client.predictions.async_create(model=SLOW_MODEL, input={})
            prediction = await client.predictions.async_create(model=SLOW_MODEL, input={})
            waiting = asyncio.ensure_future(prediction.async_wait())
            try:
                await _until_async(lambda: _polls(fake, prediction.id) >= 1, "the wait to poll")

                await _fire_async(trigger, traced, client, clock)  # another task
                fired = time.time_ns()
                assert [span.name for span in _ended(traced, idle.id)] == [CREATE]
                assert _ended(traced, prediction.id) == []
            finally:
                _set_status(fake, prediction.id, "succeeded")
                await asyncio.wait_for(waiting, 20)
            return prediction, fired

        prediction, fired = asyncio.run(scenario())
        assert prediction.status == "succeeded"

    values = _assert_one_final_create_span(traced, prediction.id, "succeeded", fired)
    assert values["output.value"] == TEXT_OUTPUT


# -- cancel ----------------------------------------------------------------------------

CANCELS = {
    "Prediction.cancel": lambda client, prediction: prediction.cancel,
    "Predictions.cancel(id)": lambda client, prediction: (
        lambda: client.predictions.cancel(prediction.id)
    ),
}
ASYNC_CANCELS = {
    "Prediction.async_cancel": lambda client, prediction: prediction.async_cancel(),
    "Predictions.async_cancel(id)": lambda client, prediction: (
        client.predictions.async_cancel(prediction.id)
    ),
}


@pytest.mark.parametrize("trigger", TRIGGERS)
@pytest.mark.parametrize("route", sorted(CANCELS))
def test_the_cap_or_a_flush_leaves_a_running_cancel_its_create_span(monkeypatch, route, trigger):
    clock = _clock(monkeypatch)
    transport = _HeldCancel()
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake, transport)
        prediction = client.predictions.create(model=SLOW_MODEL, input={})
        canceller = _Background(CANCELS[route](client, prediction))
        canceller.start()
        try:
            assert transport.entered.wait(20), "the cancel request never started"

            _fire(trigger, traced, client, clock)
            fired = time.time_ns()
            assert _ended(traced, prediction.id) == []  # claimed by the running cancel
        finally:
            transport.go.set()
            canceller.join(20)
        assert not canceller.is_alive()
        assert canceller.error is None

    _assert_one_final_create_span(traced, prediction.id, "canceled", fired)


@pytest.mark.parametrize("trigger", TRIGGERS)
@pytest.mark.parametrize("route", sorted(ASYNC_CANCELS))
def test_the_cap_or_a_flush_leaves_a_running_async_cancel_its_create_span(
    monkeypatch, route, trigger
):
    clock = _clock(monkeypatch)
    transport = _HeldCancel()
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake, transport)

        async def scenario():
            prediction = await client.predictions.async_create(model=SLOW_MODEL, input={})
            cancelling = asyncio.ensure_future(ASYNC_CANCELS[route](client, prediction))
            try:
                await _until_async(transport.entered.is_set, "the cancel request to start")

                await _fire_async(trigger, traced, client, clock)
                fired = time.time_ns()
                assert _ended(traced, prediction.id) == []
            finally:
                transport.go.set()
                await asyncio.wait_for(cancelling, 20)
            return prediction.id, fired

        prediction_id, fired = asyncio.run(scenario())

    _assert_one_final_create_span(traced, prediction_id, "canceled", fired)


def test_a_prediction_released_while_cancel_by_id_runs_is_ended_by_that_cancel():
    transport = _HeldCancel()
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake, transport)
        prediction = client.predictions.create(model=SLOW_MODEL, input={})
        prediction_id = prediction.id
        canceller = _Background(lambda: client.predictions.cancel(prediction_id))
        canceller.start()
        try:
            assert transport.entered.wait(20), "the cancel request never started"
            del prediction  # the held object is released while its cancel runs
            gc.collect()
            assert traced.provider.force_flush() is True  # drains the release queue
            fired = time.time_ns()
            assert _ended(traced, prediction_id) == []
        finally:
            transport.go.set()
            canceller.join(20)
        assert canceller.error is None

    _assert_one_final_create_span(traced, prediction_id, "canceled", fired)


# -- shutdown --------------------------------------------------------------------------


def _assert_ended_once_at_shutdown(traced, ends, prediction_id, stopping, caplog):
    spans = _ended(traced, prediction_id)
    assert [span.name for span in spans] == [CREATE]
    (span,) = spans
    values = attrs(span)
    assert values["replicate.prediction.status"] == "processing"  # the last polled status
    assert "output.value" not in values
    assert span.status.status_code is StatusCode.OK
    assert span.end_time >= stopping  # as of the shutdown, not as of create
    # The call returning later neither ended the span again nor opened a
    # wait span (the processor sees ends even after the provider stopped).
    assert [s.name for s in ends.ended if s.name.startswith("replicate.")] == [CREATE]
    assert "ended span" not in caplog.text


def test_shutdown_during_a_running_wait_ends_its_span_once(caplog):
    with FakeReplicate() as fake, instrumented() as traced:
        ends = _Ends()
        traced.provider.add_span_processor(ends)
        client = make_client(fake)
        prediction = client.predictions.create(model=SLOW_MODEL, input={})
        _set_status(fake, prediction.id, "processing")
        waiter = _Background(prediction.wait)
        waiter.start()
        with caplog.at_level(logging.WARNING, logger="opentelemetry"):
            try:
                # Two polls: the first answer ("processing") is on the object.
                _until(lambda: _polls(fake, prediction.id) >= 2, "the wait to poll")
                stopping = time.time_ns()
                traced.provider.shutdown()
                assert len(_ended(traced, prediction.id)) == 1  # ended now, not lost
            finally:
                _set_status(fake, prediction.id, "succeeded")
                waiter.join(20)
            assert not waiter.is_alive()
            assert waiter.error is None  # nothing raised into the caller
            assert prediction.status == "succeeded"

    _assert_ended_once_at_shutdown(traced, ends, prediction.id, stopping, caplog)


def test_shutdown_during_a_running_async_wait_ends_its_span_once(caplog):
    with FakeReplicate() as fake, instrumented() as traced:
        ends = _Ends()
        traced.provider.add_span_processor(ends)
        client = make_client(fake)

        async def scenario():
            prediction = await client.predictions.async_create(model=SLOW_MODEL, input={})
            _set_status(fake, prediction.id, "processing")
            waiting = asyncio.ensure_future(prediction.async_wait())
            try:
                await _until_async(lambda: _polls(fake, prediction.id) >= 2, "the wait to poll")
                stopping = time.time_ns()
                traced.provider.shutdown()
                assert len(_ended(traced, prediction.id)) == 1
            finally:
                _set_status(fake, prediction.id, "succeeded")
                await asyncio.wait_for(waiting, 20)
            return prediction, stopping

        with caplog.at_level(logging.WARNING, logger="opentelemetry"):
            prediction, stopping = asyncio.run(scenario())
        assert prediction.status == "succeeded"

    _assert_ended_once_at_shutdown(traced, ends, prediction.id, stopping, caplog)


# -- SIGTERM and exit under register() -------------------------------------------------

PACKAGE = Path(__file__).resolve().parents[1]
PYTHON_ROOT = PACKAGE.parents[1]
FI_ROOT = Path(fi_instrumentation.__file__).resolve().parents[1]

SCRIPT = """
import os
import threading
import time

import replicate
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_replicate import ReplicateInstrumentor

provider = register(
    project_name="replicate-inflight",
    project_type=ProjectType.OBSERVE,
    batch=os.environ["BATCH"] == "1",
    verbose=False,
)
ReplicateInstrumentor().instrument(tracer_provider=provider)
client = replicate.Client(base_url=os.environ["REPLICATE_BASE_URL"])
client.poll_interval = 0.01
PREDICTION = client.predictions.create(model="{model}", input={{}})


def ready():
    path = os.environ["READY_FILE"]
    with open(path + ".tmp", "w") as handle:
        handle.write(str(time.time_ns()))
    os.replace(path + ".tmp", path)


if os.environ["MODE"] == "signal":
    ready()
    PREDICTION.wait()  # the signal arrives while this polls
    print("the wait returned")
else:
    threading.Thread(target=PREDICTION.wait, daemon=True).start()
    ready()
    while not os.path.exists(os.environ["GO_FILE"]):
        time.sleep(0.01)
    # Returning exits the interpreter while the daemon thread still waits.
""".format(model=SLOW_MODEL)


def _drive(tmp_path, fake, receiver, mode, batch):
    script = tmp_path / "inflight.py"
    script.write_text(SCRIPT)
    ready = tmp_path / "ready"
    go = tmp_path / "go"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("FI_", "REPLICATE_", "OTEL_"))
    }
    env.update(
        {
            "BATCH": "1" if batch else "0",
            "MODE": mode,
            "READY_FILE": str(ready),
            "GO_FILE": str(go),
            "FI_BASE_URL": receiver.origin,
            "FI_API_KEY": "placeholder-fi-api-key",
            "FI_SECRET_KEY": "placeholder-fi-secret-key",
            "REPLICATE_BASE_URL": fake.origin,
            "REPLICATE_API_TOKEN": "r8_inflight-placeholder-token",
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
        _set_status(fake, "pred0001", "processing")
        seen = _polls(fake, "pred0001")
        _until(
            lambda: _polls(fake, "pred0001") >= seen + 2 or process.poll() is not None,
            "the wait to poll",
            60,
        )
        if mode == "signal":
            process.send_signal(signal.SIGTERM)
        else:
            go.write_text("go")
        _, stderr = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    return process.returncode, stderr.decode(errors="replace"), int(ready.read_text())


@pytest.mark.parametrize(
    "mode, batch",
    [("signal", True), ("signal", False), ("exit", True)],
    ids=["SIGTERM-batch", "SIGTERM-simple", "exit-batch"],
)
def test_a_wait_still_running_at_sigterm_or_exit_is_exported_once(tmp_path, mode, batch):
    pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")
    from harness import Receiver, _flatten_attributes

    with FakeReplicate() as fake, Receiver() as receiver:
        returncode, stderr, waiting_since = _drive(tmp_path, fake, receiver, mode, batch)
        spans = receiver.spans()

    assert returncode == 0, stderr
    assert [span["name"] for span in spans] == [CREATE], stderr
    (span,) = spans
    values = _flatten_attributes(span["attributes"])
    assert values["replicate.prediction.id"] == "pred0001"
    assert values["replicate.prediction.status"] == "processing"  # last polled, not "starting"
    assert "output.value" not in values
    assert "gen_ai.span.leaked" not in values
    assert span["status"]["code"] == "STATUS_CODE_OK"
    assert int(span["endTimeUnixNano"]) >= waiting_since  # as of the signal/exit
