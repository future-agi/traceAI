"""uninstrument() restores everything; instrument() validates its options."""

from __future__ import annotations

import gc
from importlib import import_module

import pytest

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

import replicate  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402

from _support import (  # noqa: E402
    SLOW_MODEL,
    TEXT_MODEL,
    FakeReplicate,
    attrs,
    instrumented,
    make_client,
)

TARGETS = [
    ("replicate.client", "Client", "run"),
    ("replicate.client", "Client", "async_run"),
    ("replicate.client", "Client", "stream"),
    ("replicate.client", "Client", "async_stream"),
    ("replicate.prediction", "Predictions", "create"),
    ("replicate.prediction", "Predictions", "async_create"),
    ("replicate.prediction", "Predictions", "cancel"),
    ("replicate.prediction", "Predictions", "async_cancel"),
    ("replicate.prediction", "Prediction", "wait"),
    ("replicate.prediction", "Prediction", "async_wait"),
    ("replicate.prediction", "Prediction", "cancel"),
    ("replicate.prediction", "Prediction", "async_cancel"),
    ("replicate.model", "ModelsPredictions", "create"),
    ("replicate.model", "ModelsPredictions", "async_create"),
    ("replicate.deployment", "DeploymentsPredictions", "create"),
    ("replicate.deployment", "DeploymentsPredictions", "async_create"),
    ("replicate.deployment", "DeploymentPredictions", "create"),
    ("replicate.deployment", "DeploymentPredictions", "async_create"),
]
MODULE_FUNCTIONS = ("run", "async_run", "stream", "async_stream")


def _current():
    return {
        (module, cls, name): getattr(import_module(module), cls).__dict__[name]
        for module, cls, name in TARGETS
    }


def test_every_wrapped_method_is_restored_by_identity():
    before = _current()
    functions = {name: getattr(replicate, name) for name in MODULE_FUNCTIONS}
    with instrumented():
        during = _current()
        for key in TARGETS:
            assert during[key] is not before[key], key
    after = _current()
    for key in TARGETS:
        assert after[key] is before[key], key
    for name in MODULE_FUNCTIONS:
        assert getattr(replicate, name) is functions[name], name


def test_no_spans_after_uninstrument():
    with instrumented() as traced:
        pass
    with FakeReplicate() as fake:
        client = make_client(fake)
        client.run(TEXT_MODEL, input={})
        client.predictions.create(model=TEXT_MODEL, input={}).wait()
        list(client.stream("acme/stream-model", input={}))
    assert traced.spans() == []


def test_uninstrument_ends_create_spans_still_waiting_for_wait():
    with FakeReplicate() as fake:
        with instrumented() as traced:
            prediction = make_client(fake).predictions.create(model=SLOW_MODEL, input={})
            assert traced.replicate_spans() == []
        (span,) = traced.replicate_spans()
        assert attrs(span)["replicate.prediction.status"] == "starting"
        del prediction
        gc.collect()
        assert len(traced.replicate_spans()) == 1  # ended once


def test_instrument_twice_and_uninstrument_twice_are_safe():
    from traceai_replicate import ReplicateInstrumentor

    before = _current()
    provider = TracerProvider()
    instrumentor = ReplicateInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    instrumentor.instrument(tracer_provider=provider)
    instrumentor.uninstrument()
    instrumentor.uninstrument()
    assert _current() == before


def test_instrument_accepts_a_trace_config_and_rejects_unknown_options():
    from traceai_replicate import ReplicateInstrumentor

    before = _current()
    with pytest.raises(TypeError, match="capture_everything"):
        ReplicateInstrumentor().instrument(tracer_provider=TracerProvider(), capture_everything=True)
    with pytest.raises(TypeError, match="TraceConfig"):
        ReplicateInstrumentor().instrument(tracer_provider=TracerProvider(), config={"hide": 1})
    assert _current() == before  # nothing was patched by a rejected call

    with instrumented(config=TraceConfig(hide_inputs=True)):
        assert _current() != before
