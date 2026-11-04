"""predictions.create, Prediction.wait and cancel (PRD J2/J3, AC-02, AC-03, AC-04)."""

from __future__ import annotations

import copy
import gc
import json
import pickle
import time

import pytest

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

from opentelemetry.trace import StatusCode  # noqa: E402
from replicate.prediction import Prediction  # noqa: E402
from traceai_replicate._wrappers import drain_released  # noqa: E402

from _support import (  # noqa: E402
    DEPLOYMENT,
    FAIL_MODEL,
    IMAGE_MODEL,
    MODEL_ERROR,
    PREDICT_TIME,
    PROMPT,
    SLOW_MODEL,
    TEXT_MODEL,
    TEXT_OUTPUT,
    TEXT_VERSION,
    FakeReplicate,
    RecordingTransport,
    attrs,
    exception_events,
    instrumented,
    make_client,
)

_PLAIN_KEYS = {
    "id",
    "model",
    "version",
    "status",
    "input",
    "output",
    "logs",
    "error",
    "metrics",
    "created_at",
    "started_at",
    "completed_at",
    "urls",
}


def test_create_without_wait_is_one_span_ended_at_create_and_never_polled():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        before = time.time_ns()
        prediction = client.predictions.create(model=SLOW_MODEL, input={"prompt": PROMPT})
        returned = time.time_ns()
        assert prediction.status == "starting"
        time.sleep(0.05)
        del prediction
        gc.collect()
        drain_released()  # __del__ only queues the finish; end it here, explicitly
        span = traced.one()

    # AC-02: one span, the status the create response carried, and no poll.
    assert fake.paths("GET") == []
    assert span.name == "replicate.predictions.create"
    values = attrs(span)
    assert values["replicate.prediction.status"] == "starting"
    assert values["replicate.prediction.id"] == "pred0001"
    assert values["gen_ai.request.model"] == SLOW_MODEL
    assert values["gen_ai.provider.name"] == "replicate"
    assert values["gen_ai.span.kind"] == "CHAIN"  # no output yet
    assert "output.value" not in values
    assert before <= span.start_time <= span.end_time <= returned
    assert span.status.status_code is StatusCode.OK


def test_create_then_wait_is_one_span_with_the_terminal_status():
    with FakeReplicate() as fake, instrumented() as traced:
        transport = RecordingTransport(tracer=traced.tracer())
        client = make_client(fake, transport)
        prediction = client.predictions.create(model=TEXT_MODEL, input={"prompt": PROMPT})
        assert traced.replicate_spans() == []  # still open: wait may follow
        prediction.wait()
        assert prediction.status == "succeeded"
        span = traced.one()  # ended by wait, while the prediction is still held

    # AC-03: one span, not two; the client's own poll loop ran inside it.
    assert fake.paths("GET").count("/v1/predictions/pred0001") == 2
    values = attrs(span)
    assert span.name == "replicate.predictions.create"
    assert values["replicate.prediction.status"] == "succeeded"
    assert values["replicate.metrics.predict_time"] == PREDICT_TIME
    assert values["output.value"] == TEXT_OUTPUT
    assert values["gen_ai.span.kind"] == "LLM"
    assert span.status.status_code is StatusCode.OK
    http = [s for s in traced.spans() if s.name.startswith("HTTP ")]
    assert [s.name for s in http] == ["HTTP POST", "HTTP GET", "HTTP GET"]
    assert {s.parent.span_id for s in http} == {span.context.span_id}


def test_cancel_ends_the_create_span_as_canceled_without_an_exception():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        prediction = client.predictions.create(model=SLOW_MODEL, input={})
        prediction.cancel()
        assert prediction.status == "canceled"
        span = traced.one()

    # AC-04: canceled is a terminal state, not a failure and not an exception.
    assert attrs(span)["replicate.prediction.status"] == "canceled"
    assert span.status.status_code is StatusCode.OK
    assert exception_events(span) == []
    assert "replicate.cancelled" not in attrs(span)
    assert fake.paths("POST")[-1] == "/v1/predictions/pred0001/cancel"


def test_cancel_by_id_ends_the_pending_create_span():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        prediction = client.predictions.create(model=SLOW_MODEL, input={})
        canceled = client.predictions.cancel(prediction.id)
        assert canceled.status == "canceled"
        span = traced.one()

    assert span.name == "replicate.predictions.create"
    assert attrs(span)["replicate.prediction.status"] == "canceled"


def test_cancel_of_a_prediction_without_a_create_span_is_one_cancel_span():
    with FakeReplicate() as fake:
        untraced = make_client(fake).predictions.create(model=SLOW_MODEL, input={})
        with instrumented() as traced:
            client = make_client(fake)
            canceled = client.predictions.cancel(untraced.id)  # positional id

    assert canceled.status == "canceled"
    span = traced.one()
    assert span.name == "replicate.predictions.cancel"
    values = attrs(span)
    assert values["replicate.prediction.id"] == untraced.id
    assert values["replicate.prediction.status"] == "canceled"
    assert span.status.status_code is StatusCode.OK


def test_failed_prediction_after_wait_sets_error_from_the_error_field():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        prediction = client.predictions.create(model=FAIL_MODEL, input={})
        prediction.wait()  # the client returns; it does not raise
        span = traced.one()

    assert attrs(span)["replicate.prediction.status"] == "failed"
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == MODEL_ERROR
    assert exception_events(span) == []


def test_prefer_wait_create_that_returns_terminal_ends_at_create():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        # replicate 1.0.x drops wait= when predictions.create delegates a
        # model= call, so the version route is the one that sends Prefer.
        prediction = client.predictions.create(version=TEXT_VERSION, input={}, wait=True)
        assert prediction.status == "succeeded"
        span = traced.one()  # no wait call needed
        assert prediction.status == "succeeded"

    assert fake.request_headers("POST", "/v1/predictions")["prefer"] == "wait"
    values = attrs(span)
    assert values["replicate.prediction.status"] == "succeeded"
    assert json.loads(values["gen_ai.request.parameters"])["wait"] is True


def test_create_by_model_is_one_span_although_the_sdk_delegates():
    # predictions.create(model=...) calls models.predictions.create internally.
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        client.predictions.create(model=IMAGE_MODEL, input={}).wait()

    span = traced.one()
    assert attrs(span)["replicate.output.type"] == "url"


def test_create_by_positional_version_records_the_version_and_response_model():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        client.predictions.create(TEXT_VERSION, {"prompt": PROMPT}).wait()

    values = attrs(traced.one())
    assert values["replicate.prediction.version"] == TEXT_VERSION
    assert values["gen_ai.request.model"] == TEXT_MODEL  # from the response's model field
    assert json.loads(values["input.value"]) == {"prompt": PROMPT}


def test_official_model_create_route_is_traced():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        client.models.predictions.create(TEXT_MODEL, {"prompt": PROMPT}).wait()

    assert fake.paths("POST") == ["/v1/models/acme/text-model/predictions"]
    assert attrs(traced.one())["gen_ai.request.model"] == TEXT_MODEL


def test_deployment_create_routes_record_the_deployment_ref():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        client.deployments.predictions.create(DEPLOYMENT, {"prompt": PROMPT}).wait()
        client.predictions.create(deployment=DEPLOYMENT, input={}).wait()
        client.deployments.get(DEPLOYMENT).predictions.create(input={}).wait()

    spans = traced.replicate_spans()
    assert [span.name for span in spans] == ["replicate.predictions.create"] * 3
    for span in spans:
        assert attrs(span)["replicate.deployment"] == DEPLOYMENT
        assert attrs(span)["replicate.prediction.status"] == "succeeded"


def test_wait_on_a_fetched_prediction_is_one_wait_span():
    with FakeReplicate() as fake:
        created = make_client(fake).predictions.create(model=TEXT_MODEL, input={})
        with instrumented() as traced:
            client = make_client(fake)
            fetched = client.predictions.get(created.id)  # get is not traced
            fetched.wait()

    span = traced.one()
    assert span.name == "replicate.prediction.wait"
    assert attrs(span)["replicate.prediction.id"] == created.id
    assert attrs(span)["replicate.prediction.status"] == "succeeded"


def test_wait_on_a_terminal_prediction_adds_no_span():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        prediction = client.predictions.create(model=TEXT_MODEL, input={})
        prediction.wait()
        prediction.wait()  # already succeeded: no request, no second span
        assert len(traced.replicate_spans()) == 1


def _outcome(operation, value):
    try:
        result = operation(value)
    except Exception as error:  # e.g. the client's httpx transport is not picklable
        return type(error)
    return type(result), result.id


def test_returned_prediction_stays_a_plain_prediction_to_the_caller():
    with FakeReplicate() as fake, instrumented():
        client = make_client(fake)
        prediction = client.predictions.create(model=SLOW_MODEL, input={})
        plain = client.predictions.get(prediction.id)  # untraced: a plain Prediction
        assert type(plain) is Prediction
        assert isinstance(prediction, Prediction)
        assert set(prediction.dict()) == _PLAIN_KEYS == set(plain.dict())
        assert type(copy.copy(prediction)) is Prediction
        # Copying and pickling behave exactly as they do for a plain Prediction.
        for operation in (copy.copy, copy.deepcopy, lambda p: pickle.loads(pickle.dumps(p))):
            assert _outcome(operation, prediction) == _outcome(operation, plain)


def test_webhook_url_is_not_recorded():
    hook = "https://hooks.example.invalid/WEBHOOK-SECRET-MARKER"
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        client.predictions.create(version=TEXT_VERSION, input={}, webhook=hook, wait=True)

    params = json.loads(attrs(traced.one())["gen_ai.request.parameters"])
    assert params["webhook"] is True
    assert "WEBHOOK-SECRET-MARKER" not in traced.wire()
