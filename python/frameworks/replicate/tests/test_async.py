"""Async parity: async_run, async_create + async_wait, async_cancel."""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

from opentelemetry.trace import StatusCode  # noqa: E402
from replicate.exceptions import ModelError  # noqa: E402

from _support import (  # noqa: E402
    FAIL_MODEL,
    ITERATOR_VERSION,
    MODEL_ERROR,
    PREDICT_TIME,
    PROMPT,
    SLOW_MODEL,
    TEXT_MODEL,
    TEXT_OUTPUT,
    TEXT_TOKENS,
    FakeReplicate,
    RecordingTransport,
    attrs,
    exception_events,
    instrumented,
    make_client,
)


def test_async_run_matches_the_sync_span():
    with FakeReplicate() as fake, instrumented() as traced:
        transport = RecordingTransport(tracer=traced.tracer())
        client = make_client(fake, transport)
        output = asyncio.run(client.async_run(TEXT_MODEL, input={"prompt": PROMPT}, wait=False))

    assert output == TEXT_TOKENS
    span = traced.one()
    assert span.name == "replicate.run"
    values = attrs(span)
    assert values["gen_ai.provider.name"] == "replicate"
    assert values["gen_ai.request.model"] == TEXT_MODEL
    assert values["gen_ai.span.kind"] == "LLM"
    assert values["replicate.prediction.id"] == "pred0001"
    assert values["replicate.prediction.status"] == "succeeded"
    assert values["replicate.metrics.predict_time"] == PREDICT_TIME
    assert values["output.value"] == TEXT_OUTPUT
    assert json.loads(values["input.value"]) == {"prompt": PROMPT}
    assert span.status.status_code is StatusCode.OK
    http = [s for s in traced.spans() if s.name.startswith("HTTP ")]
    assert len(http) == 3
    assert {s.parent.span_id for s in http} == {span.context.span_id}


def test_async_run_failure_raises_model_error_and_marks_the_span():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        with pytest.raises(ModelError):
            asyncio.run(client.async_run(FAIL_MODEL, input={}))

    span = traced.one()
    assert span.status.status_code is StatusCode.ERROR
    assert MODEL_ERROR in span.status.description
    assert len(exception_events(span)) == 1


def test_async_create_then_async_wait_is_one_span():
    async def main(client):
        prediction = await client.predictions.async_create(model=TEXT_MODEL, input={})
        await prediction.async_wait()
        return prediction

    with FakeReplicate() as fake, instrumented() as traced:
        prediction = asyncio.run(main(make_client(fake)))
        span = traced.one()
        assert prediction.status == "succeeded"

    assert span.name == "replicate.predictions.create"
    assert attrs(span)["replicate.prediction.status"] == "succeeded"
    assert attrs(span)["output.value"] == TEXT_OUTPUT
    assert span.status.status_code is StatusCode.OK


def test_async_cancel_ends_the_create_span_as_canceled():
    async def main(client):
        prediction = await client.predictions.async_create(model=SLOW_MODEL, input={})
        await prediction.async_cancel()
        return prediction

    with FakeReplicate() as fake, instrumented() as traced:
        prediction = asyncio.run(main(make_client(fake)))
        span = traced.one()
        assert prediction.status == "canceled"

    assert attrs(span)["replicate.prediction.status"] == "canceled"
    assert span.status.status_code is StatusCode.OK
    assert exception_events(span) == []


def test_async_cancel_by_id_without_a_create_span():
    with FakeReplicate() as fake:
        prediction_id = make_client(fake).predictions.create(model=SLOW_MODEL, input={}).id
        with instrumented() as traced:
            client = make_client(fake)
            canceled = asyncio.run(client.predictions.async_cancel(prediction_id))

    assert canceled.status == "canceled"
    span = traced.one()
    assert span.name == "replicate.predictions.cancel"
    assert attrs(span)["replicate.prediction.id"] == prediction_id


def test_async_iterator_output_parity_and_early_aclose():
    ref = "{0}:{1}".format(TEXT_MODEL, ITERATOR_VERSION)

    async def full(client):
        iterator = await client.async_run(ref, input={}, wait=False)
        assert inspect.isasyncgen(iterator)
        return [chunk async for chunk in iterator]

    async def partial(client, traced):
        iterator = await client.async_run(ref, input={}, wait=False)
        await iterator.__anext__()
        await iterator.aclose()
        return traced.replicate_spans()

    with FakeReplicate() as fake, instrumented() as traced:
        assert asyncio.run(full(make_client(fake))) == TEXT_TOKENS
        (complete,) = traced.replicate_spans()
        traced.exporter.clear()
        (closed,) = asyncio.run(partial(make_client(fake), traced))

    assert attrs(complete)["output.value"] == TEXT_OUTPUT
    assert complete.status.status_code is StatusCode.OK
    assert closed.status.status_code is StatusCode.ERROR
    assert closed.status.description == "cancelled"
    assert attrs(closed)["replicate.cancelled"] is True


def test_cancelling_an_async_run_task_ends_the_span_as_cancelled():
    async def main(client):
        task = asyncio.ensure_future(client.async_run(SLOW_MODEL, input={}, wait=False))
        await asyncio.sleep(0.2)  # the client is polling a prediction that never ends
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with FakeReplicate() as fake, instrumented() as traced:
        asyncio.run(main(make_client(fake)))
        span = traced.one()

    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "cancelled"
    assert attrs(span)["replicate.cancelled"] is True
    assert attrs(span)["replicate.prediction.id"] == "pred0001"
    assert exception_events(span) == []
