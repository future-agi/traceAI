"""Releasing a traced object never runs span processors from ``__del__`` (R3).

The cyclic GC can run ``__del__`` on any thread, at any allocation, including
while that thread holds a lock on the export path (the HTTP connection pool
of an inline ``SimpleSpanProcessor`` export). ``__del__`` therefore only
queues the finish. The span is ended, exactly once and with the end
timestamp it had when it was released, at the next traced call, registry
operation, provider ``force_flush``/``shutdown``, ``uninstrument()`` or exit.
"""

from __future__ import annotations

import asyncio
import gc
import threading
import time

import pytest

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

import wrapt  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402

from _support import (  # noqa: E402
    ITERATOR_VERSION,
    SLOW_MODEL,
    STREAM_MODEL,
    TEXT_MODEL,
    FakeReplicate,
    attrs,
    instrumented,
    make_client,
)


class _EndRecorder(wrapt.ObjectProxy):  # type: ignore[misc]
    """Records every ``end()`` on the span of one traced call."""

    def __init__(self, span, ended):
        super().__init__(span)
        self._self_ended = ended

    def end(self, end_time=None):
        self._self_ended.append(threading.current_thread().name)
        return self.__wrapped__.end(end_time)


def _record_end(traced_object):
    ended = []
    call = traced_object._self_call
    call.span = _EndRecorder(call.span, ended)
    return ended


def _assert_cancelled(span):
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "cancelled"
    assert attrs(span)["replicate.cancelled"] is True


def test_releasing_a_held_prediction_queues_its_span_instead_of_ending_it_in_del():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        prediction = client.predictions.create(model=SLOW_MODEL, input={})
        returned = time.time_ns()
        ended = _record_end(prediction)
        del prediction
        gc.collect()
        assert ended == []  # __del__ did not call span.end
        assert traced.replicate_spans() == []

        client.run(TEXT_MODEL, input={})  # the next traced call ends it
        assert len(ended) == 1
        create, run = traced.replicate_spans()
        assert (create.name, run.name) == ("replicate.predictions.create", "replicate.run")
        assert attrs(create)["replicate.prediction.status"] == "starting"
        assert create.status.status_code is StatusCode.OK
        assert create.end_time <= returned  # its create-time end timestamp

    assert len(ended) == 1  # uninstrument() did not end it again


def test_dropping_a_stream_queues_its_span_until_the_provider_flushes():
    with FakeReplicate() as fake, instrumented() as traced:
        stream = make_client(fake).stream(STREAM_MODEL, input={})
        next(stream)
        ended = _record_end(stream)
        del stream
        gc.collect()
        dropped = time.time_ns()
        assert ended == []
        assert traced.replicate_spans() == []

        time.sleep(0.01)
        traced.provider.force_flush()
        (span,) = traced.replicate_spans()
        assert len(ended) == 1
        _assert_cancelled(span)
        assert span.end_time <= dropped  # when it was dropped, not when it was flushed


def test_dropping_a_run_iterator_queues_its_span_until_the_next_traced_call():
    ref = "{0}:{1}".format(TEXT_MODEL, ITERATOR_VERSION)
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        iterator = client.run(ref, input={}, wait=False)
        next(iterator)
        ended = _record_end(iterator)
        del iterator
        gc.collect()
        dropped = time.time_ns()
        assert ended == []

        client.predictions.create(model=TEXT_MODEL, input={}).wait()
        assert len(ended) == 1
        span = traced.replicate_spans()[0]
        assert span.name == "replicate.run"
        _assert_cancelled(span)
        assert span.end_time <= dropped


def test_dropping_an_async_stream_queues_its_span_until_uninstrument():
    async def read_one_and_drop(client):
        stream = await client.async_stream(STREAM_MODEL, input={})
        await stream.__anext__()
        ended = _record_end(stream)
        del stream
        gc.collect()
        return ended, time.time_ns()

    with FakeReplicate() as fake, instrumented() as traced:
        ended, dropped = asyncio.run(read_one_and_drop(make_client(fake)))
        assert ended == []
        assert traced.replicate_spans() == []

        traced.instrumentor.uninstrument()
        (span,) = traced.replicate_spans()
        assert len(ended) == 1
        _assert_cancelled(span)
        assert span.end_time <= dropped


def test_a_released_prediction_is_ended_by_provider_shutdown():
    with FakeReplicate() as fake, instrumented() as traced:
        prediction = make_client(fake).predictions.create(model=SLOW_MODEL, input={})
        ended = _record_end(prediction)
        del prediction
        gc.collect()
        traced.provider.shutdown()
        (span,) = traced.replicate_spans()
        assert attrs(span)["replicate.prediction.status"] == "starting"
        assert len(ended) == 1
