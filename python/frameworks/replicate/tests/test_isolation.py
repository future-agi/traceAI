"""Instrumentation failures never reach the caller; vendor errors pass through."""

from __future__ import annotations

import pytest

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402
from replicate.exceptions import ReplicateError  # noqa: E402

import traceai_replicate._wrappers as wrappers  # noqa: E402
from _support import (  # noqa: E402
    SLOW_MODEL,
    STREAM_CHUNKS,
    STREAM_MODEL,
    TEXT_MODEL,
    TEXT_TOKENS,
    FakeReplicate,
    instrumented,
    make_client,
)


def _boom(*_args, **_kwargs):
    raise RuntimeError("instrumentation bug")


@pytest.mark.parametrize(
    "helper", ["request_attributes", "output_attributes", "prediction_attributes", "api_tokens"]
)
def test_a_failing_attribute_helper_does_not_change_the_call(monkeypatch, helper):
    monkeypatch.setattr(wrappers, helper, _boom)
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        assert client.run(TEXT_MODEL, input={"prompt": "x"}) == TEXT_TOKENS
        prediction = client.predictions.create(model=TEXT_MODEL, input={})
        prediction.wait()
        assert prediction.status == "succeeded"
        events = [str(event) for event in client.stream(STREAM_MODEL, input={}) if str(event)]
        assert events == STREAM_CHUNKS

    spans = traced.replicate_spans()
    assert [span.name for span in spans] == [
        "replicate.run",
        "replicate.predictions.create",
        "replicate.stream",
    ]
    for span in spans:
        assert span.end_time is not None
        assert span.status.status_code is StatusCode.OK


class _BrokenTracerProvider(TracerProvider):
    def get_tracer(self, *args, **kwargs):
        tracer = super().get_tracer(*args, **kwargs)

        class Broken(type(tracer)):
            def start_span(self, *a, **k):
                raise RuntimeError("tracer bug")

        tracer.__class__ = Broken
        return tracer


def test_a_failing_tracer_does_not_change_the_call():
    from traceai_replicate import ReplicateInstrumentor

    instrumentor = ReplicateInstrumentor()
    instrumentor.instrument(tracer_provider=_BrokenTracerProvider())
    try:
        with FakeReplicate() as fake:
            client = make_client(fake)
            assert client.run(TEXT_MODEL, input={}) == TEXT_TOKENS
            prediction = client.predictions.create(model=SLOW_MODEL, input={})
            prediction.cancel()
            assert prediction.status == "canceled"
            assert [str(e) for e in client.stream(STREAM_MODEL, input={}) if str(e)] == STREAM_CHUNKS
    finally:
        instrumentor.uninstrument()


def test_vendor_errors_reach_the_caller_unchanged():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        with pytest.raises(ReplicateError) as raised:
            client.predictions.cancel("does/not/exist")
        with pytest.raises(ValueError, match="Invalid reference"):
            client.run("not a model ref", input={})

    assert raised.value.status == 404
    statuses = [span.status.status_code for span in traced.replicate_spans()]
    assert statuses == [StatusCode.ERROR, StatusCode.ERROR]
