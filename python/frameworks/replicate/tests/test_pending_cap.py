"""A held create span is held for at most ``max_pending_seconds`` (R2).

A create span that no wait/cancel continues is ended, as of create time and
with its create-time status, the next time the registry is touched after it
is older than the cap: any traced call, ``force_flush`` or ``shutdown``. A
later ``wait()`` on that prediction gets its own span.
"""

from __future__ import annotations

import asyncio
import gc
import time

import pytest

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402
from replicate.prediction import Predictions  # noqa: E402

from _support import SLOW_MODEL, TEXT_MODEL, FakeReplicate, attrs, instrumented, make_client  # noqa: E402

CAP = 0.05

TOUCHES = {
    "run": lambda client: client.run(TEXT_MODEL, input={}),
    "async_run": lambda client: asyncio.run(client.async_run(TEXT_MODEL, input={})),
    "create": lambda client: client.predictions.create(model=SLOW_MODEL, input={}),
    "async_create": lambda client: asyncio.run(
        client.predictions.async_create(model=SLOW_MODEL, input={})
    ),
}


@pytest.mark.parametrize("touch", sorted(TOUCHES))
def test_a_held_create_span_past_the_cap_ends_at_the_next_traced_call(touch):
    with FakeReplicate() as fake, instrumented(max_pending_seconds=CAP) as traced:
        client = make_client(fake)
        prediction = client.predictions.create(model=TEXT_MODEL, input={})
        returned = time.time_ns()
        time.sleep(CAP * 2)
        assert traced.replicate_spans() == []  # lazy: nothing touched the registry yet

        TOUCHES[touch](client)
        create = traced.replicate_spans()[0]
        assert create.name == "replicate.predictions.create"
        values = attrs(create)
        assert values["replicate.prediction.id"] == prediction.id
        assert values["replicate.prediction.status"] == "starting"  # the create-time status
        assert create.status.status_code is StatusCode.OK
        assert create.end_time <= returned  # the create-time end timestamp
        assert prediction.status == "starting"  # the caller's object is untouched

        del prediction
        gc.collect()
        traced.provider.force_flush()
        ids = [attrs(span).get("replicate.prediction.id") for span in traced.replicate_spans()]
        assert ids.count(values["replicate.prediction.id"]) == 1  # ended once


def test_wait_after_the_cap_ended_the_create_span_gets_its_own_wait_span():
    with FakeReplicate() as fake, instrumented(max_pending_seconds=CAP) as traced:
        client = make_client(fake)
        prediction = client.predictions.create(model=TEXT_MODEL, input={})
        time.sleep(CAP * 2)
        prediction.wait()  # its own entry ends the stale create span first
        assert prediction.status == "succeeded"

    create, wait = traced.replicate_spans()
    assert (create.name, wait.name) == ("replicate.predictions.create", "replicate.prediction.wait")
    assert attrs(create)["replicate.prediction.status"] == "starting"
    assert attrs(wait)["replicate.prediction.status"] == "succeeded"
    assert attrs(wait)["replicate.prediction.id"] == attrs(create)["replicate.prediction.id"]


def test_the_default_cap_is_600_seconds(monkeypatch):
    from traceai_replicate import _wrappers

    clock = [1000.0]
    monkeypatch.setattr(_wrappers, "_monotonic", lambda: clock[0], raising=False)
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        prediction = client.predictions.create(model=TEXT_MODEL, input={})
        clock[0] += 599
        client.run(TEXT_MODEL, input={})
        assert [span.name for span in traced.replicate_spans()] == ["replicate.run"]
        clock[0] += 2
        client.run(TEXT_MODEL, input={})
        assert [span.name for span in traced.replicate_spans()] == [
            "replicate.run",
            "replicate.predictions.create",
            "replicate.run",
        ]
        assert prediction.status == "starting"


def test_a_held_span_younger_than_the_cap_is_still_continued_by_wait():
    with FakeReplicate() as fake, instrumented(max_pending_seconds=60) as traced:
        client = make_client(fake)
        prediction = client.predictions.create(model=TEXT_MODEL, input={})
        client.run(TEXT_MODEL, input={})
        prediction.wait()

    names = [span.name for span in traced.replicate_spans()]
    assert names == ["replicate.run", "replicate.predictions.create"]


@pytest.mark.parametrize(
    "value, error",
    [
        ("600", TypeError),
        (None, TypeError),
        (True, TypeError),
        (0, ValueError),
        (-1, ValueError),
        (float("nan"), ValueError),
        (float("inf"), ValueError),
    ],
)
def test_max_pending_seconds_is_validated(value, error):
    from traceai_replicate import ReplicateInstrumentor

    before = Predictions.__dict__["create"]
    with pytest.raises(error, match="max_pending_seconds must be"):
        ReplicateInstrumentor().instrument(tracer_provider=TracerProvider(), max_pending_seconds=value)
    assert Predictions.__dict__["create"] is before  # nothing was patched


@pytest.mark.parametrize("value", [1, 0.5, 3600])
def test_max_pending_seconds_accepts_a_positive_number(value):
    before = Predictions.__dict__["create"]
    with instrumented(max_pending_seconds=value):
        assert Predictions.__dict__["create"] is not before
    assert Predictions.__dict__["create"] is before
