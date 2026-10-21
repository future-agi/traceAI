"""Shapes seen on the live api.replicate.com (TH-8320 live run, 2026-10-20).

Recorded in livefill/TH-8320/ev: an official model (meta/meta-llama-3-8b-instruct)
reports ``version: "hidden"`` (spans-run.json, spans-stream.json and the stored
rows); its stream is ``output`` x3 then ``done`` (run-stream.txt) and carries
no status or metrics, while the finished prediction reports ``predict_time``,
``input_token_count`` and ``output_token_count`` (spans-run.json). The raw SSE
bytes were not recorded; the ``done`` payload ``{}`` is Replicate's documented
success payload (replicate.com/docs/topics/predictions/streaming).

Everything runs against the loopback ``FakeReplicate``; no live call is made.
"""

from __future__ import annotations

import asyncio
import time
import types

import httpx
import pytest

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

from fi_instrumentation import TraceConfig  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402
from replicate.exceptions import ReplicateError  # noqa: E402

from _support import (  # noqa: E402
    INPUT_TOKENS,
    OFFICIAL_MODEL,
    OUTPUT_TOKENS,
    PREDICT_TIME,
    STREAM_CHUNKS,
    STREAM_ERROR,
    STREAM_ERROR_MODEL,
    STREAM_MODEL,
    STREAM_TEXT,
    TEXT_MODEL,
    TEXT_OUTPUT,
    TEXT_TOKENS,
    TEXT_VERSION,
    READ_ON,
    FakeReplicate,
    RecordingTransport,
    attrs,
    default_client,
    instrumented,
    make_client,
)

READ = "/v1/predictions/pred0001"


def _texts(events):
    return [str(event) for event in events if str(event)]


def _final(values):
    assert values["replicate.prediction.status"] == "succeeded"
    assert values["replicate.metrics.predict_time"] == PREDICT_TIME
    assert values["gen_ai.usage.input_tokens"] == INPUT_TOKENS
    assert values["gen_ai.usage.output_tokens"] == OUTPUT_TOKENS
    assert values["gen_ai.usage.total_tokens"] == INPUT_TOKENS + OUTPUT_TOKENS


def _no_final(values):
    for key in (
        "replicate.prediction.status",
        "replicate.metrics.predict_time",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
    ):
        assert key not in values, key


class _ThrottleOnce(RecordingTransport):
    """Answers the first request of ``method`` with a 429 the way Replicate does."""

    def __init__(self, method, tracer=None):
        super().__init__(tracer)
        self._method = method
        self.throttled = 0

    def _throttle(self, request):
        if request.method == self._method and not self.throttled:
            self.throttled += 1
            self.requests.append((request.method, str(request.url)))
            return httpx.Response(
                429,
                headers={"Retry-After": "0", "Content-Type": "application/problem+json"},
                json={"title": "Request was throttled", "detail": "rate limited", "status": 429},
                request=request,
            )
        return None

    def handle_request(self, request):
        return self._throttle(request) or super().handle_request(request)

    async def handle_async_request(self, request):
        return self._throttle(request) or await super().handle_async_request(request)


# -- version "hidden" -----------------------------------------------------------------


@pytest.mark.parametrize("options", [{}, READ_ON], ids=["default", "read-on"])
def test_official_model_placeholder_version_is_not_recorded(options):
    with FakeReplicate() as fake, instrumented(**options) as traced:
        client = default_client(fake)
        assert client.run(OFFICIAL_MODEL, input={}) == TEXT_TOKENS
        assert _texts(client.stream(OFFICIAL_MODEL, input={})) == STREAM_CHUNKS

    run, stream = traced.replicate_spans()
    for span in (run, stream):
        values = attrs(span)
        assert "replicate.prediction.version" not in values
        assert "hidden" not in values.values()
    _final(attrs(run))  # run() polls the prediction to its end itself
    if options:
        _final(attrs(stream))  # from the opt-in read after done
    else:
        _no_final(attrs(stream))  # the event stream reports no status or tokens


def test_a_real_version_id_is_still_recorded():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        client.run("{0}:{1}".format(TEXT_MODEL, TEXT_VERSION), input={})

    assert attrs(traced.one())["replicate.prediction.version"] == TEXT_VERSION


def test_official_model_async_run_does_not_record_the_hidden_version():
    with FakeReplicate() as fake, instrumented() as traced:
        output = asyncio.run(make_client(fake).async_run(OFFICIAL_MODEL, input={}))

    assert output == TEXT_TOKENS
    values = attrs(traced.one())
    assert "replicate.prediction.version" not in values
    assert "hidden" not in values.values()
    _final(values)


def test_held_official_model_create_and_wait_does_not_record_the_hidden_version():
    # The held create span records the create-time snapshot and then the
    # waited prediction; neither may carry the placeholder.
    with FakeReplicate() as fake, instrumented() as traced:
        prediction = make_client(fake).models.predictions.create(OFFICIAL_MODEL, {"prompt": "x"})
        assert prediction.version == "hidden"  # the vendor object is untouched
        prediction.wait()

    values = attrs(traced.one())
    assert values["replicate.prediction.status"] == "succeeded"
    assert "replicate.prediction.version" not in values


def test_an_official_model_create_that_is_never_waited_on_drops_the_placeholder_too():
    with FakeReplicate() as fake, instrumented() as traced:
        prediction = make_client(fake).models.predictions.create(OFFICIAL_MODEL, {"prompt": "x"})
        del prediction  # the held span ends with its create-time snapshot

    assert "replicate.prediction.version" not in attrs(traced.one())


@pytest.mark.parametrize("version", ["hidden", "", None, 7])
def test_prediction_attributes_drop_the_placeholder_and_non_ids(version):
    from traceai_replicate._attributes import prediction_attributes

    prediction = types.SimpleNamespace(id="p1", status="succeeded", version=version, model=TEXT_MODEL, metrics=None)
    values = prediction_attributes(prediction)
    assert "replicate.prediction.version" not in values
    assert values["replicate.prediction.id"] == "p1"


@pytest.mark.parametrize(
    "ref",
    [OFFICIAL_MODEL + ":hidden", ("acme", "official-llm", "hidden")],
)
def test_a_hidden_ref_version_is_not_recorded(ref):
    from traceai_replicate._attributes import request_attributes

    def run(self, ref, input=None):  # noqa: A002 - mirrors Client.run
        return None

    values = request_attributes("run", run, None, (ref,), {"input": {}})
    assert values["gen_ai.request.model"] == OFFICIAL_MODEL
    assert "replicate.prediction.version" not in values


def test_a_version_object_with_the_placeholder_id_is_not_recorded():
    from traceai_replicate._attributes import request_attributes

    class Version:  # replicate.version.Version is recognised by its openapi_schema
        id = "hidden"
        openapi_schema = {}

    def create(self, version, input=None):  # noqa: A002 - mirrors Predictions.create
        return None

    assert "replicate.prediction.version" not in request_attributes("create", create, None, (Version(),), {})


# -- the read after ``done`` (opt-in) -----------------------------------------------------


def test_by_default_a_stream_is_not_read_after_done():
    with FakeReplicate() as fake, instrumented() as traced:
        transport = RecordingTransport(tracer=traced.tracer())
        assert _texts(make_client(fake, transport).stream(STREAM_MODEL, input={})) == STREAM_CHUNKS
        assert _texts(default_client(fake).stream(STREAM_MODEL, input={})) == STREAM_CHUNKS

    for span in traced.replicate_spans():
        values = attrs(span)
        assert span.status.status_code is StatusCode.OK
        assert values["output.value"] == STREAM_TEXT
        _no_final(values)
        assert "replicate.stream.read" not in values
    assert fake.paths("GET") == ["/stream/pred0001", "/stream/pred0002"]  # no read
    assert [method for method, _ in transport.requests] == ["POST", "GET"]  # create, event stream
    # Each span's own HTTP child spans are the create and the event stream; none for a read.
    assert [s.name for s in traced.spans() if s.name.startswith("HTTP ")] == ["HTTP POST", "HTTP GET"]


def test_stream_reads_the_prediction_once_after_done_and_ends_as_of_the_stream():
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        fake.read_delay = 0.3
        client = default_client(fake)
        assert _texts(client.stream(STREAM_MODEL, input={})) == STREAM_CHUNKS
        exited = time.time_ns()  # before the read returns
        traced.settle()

    span = traced.one()
    _final(attrs(span))
    assert span.status.status_code is StatusCode.OK
    assert fake.paths("GET") == ["/stream/pred0001", READ]  # one read, no new prediction
    assert fake.paths("POST") == ["/v1/models/acme/stream-model/predictions"]
    assert span.end_time <= exited  # the read does not stretch the span


def test_async_stream_reads_the_prediction_once_after_done():
    async def consume(client):
        stream = await client.async_stream(STREAM_MODEL, input={})
        return [event async for event in stream]

    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        events = asyncio.run(consume(default_client(fake)))
        traced.settle()

    assert _texts(events) == STREAM_CHUNKS
    _final(attrs(traced.one()))
    assert fake.paths("GET") == ["/stream/pred0001", READ]


def test_a_second_read_covers_an_api_that_lags_behind_done():
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        fake.lag_streams = True  # first read: "processing", second: "succeeded"
        list(default_client(fake).stream(STREAM_MODEL, input={}))
        traced.settle()

    _final(attrs(traced.one()))
    assert fake.paths("GET") == ["/stream/pred0001", READ, READ]


def test_the_read_is_bounded_by_its_budget():
    with FakeReplicate() as fake, instrumented(stream_reload_seconds=0.3) as traced:
        fake.read_delay = 5.0
        started = time.monotonic()
        assert _texts(default_client(fake).stream(STREAM_MODEL, input={})) == STREAM_CHUNKS
        elapsed = time.monotonic() - started

    assert elapsed < 2.0, elapsed
    span = traced.one()
    assert span.status.status_code is StatusCode.OK
    _no_final(attrs(span))
    assert attrs(span)["output.value"] == STREAM_TEXT
    assert attrs(span)["replicate.stream.read"] == "timeout"
    assert READ in fake.paths("GET")  # the read was made, and cut


def test_the_async_read_is_bounded_by_its_budget():
    async def consume(client):
        stream = await client.async_stream(STREAM_MODEL, input={})
        return [event async for event in stream]

    with FakeReplicate() as fake, instrumented(stream_reload_seconds=0.3) as traced:
        fake.read_delay = 5.0
        started = time.monotonic()
        assert _texts(asyncio.run(consume(default_client(fake)))) == STREAM_CHUNKS
        elapsed = time.monotonic() - started

    assert elapsed < 2.0, elapsed
    span = traced.one()
    assert span.status.status_code is StatusCode.OK
    _no_final(attrs(span))


@pytest.mark.parametrize("status", [404, 500])
def test_a_failed_read_leaves_the_span_as_before(status):
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        fake.read_status = status
        assert _texts(default_client(fake).stream(STREAM_MODEL, input={})) == STREAM_CHUNKS
        traced.settle()

    span = traced.one()
    assert span.status.status_code is StatusCode.OK
    _no_final(attrs(span))
    assert attrs(span)["replicate.prediction.id"] == "pred0001"
    assert attrs(span)["replicate.stream.read"] == "failed"


def test_the_read_can_be_turned_off():
    with FakeReplicate() as fake, instrumented(stream_reload_seconds=0) as traced:
        list(make_client(fake).stream(STREAM_MODEL, input={}))

    _no_final(attrs(traced.one()))
    assert fake.paths("GET") == ["/stream/pred0001"]


def test_no_read_after_a_stream_error_or_an_early_close():
    with FakeReplicate() as fake, instrumented(**READ_ON) as traced:
        client = default_client(fake)
        with pytest.raises(RuntimeError, match=STREAM_ERROR):
            list(client.stream(STREAM_ERROR_MODEL, input={}))
        stream = client.stream(STREAM_MODEL, input={})
        next(stream)
        stream.close()

    assert [path for path in fake.paths("GET") if path.startswith("/v1/predictions/")] == []
    assert [span.status.status_code for span in traced.replicate_spans()] == [StatusCode.ERROR] * 2


def test_hidden_outputs_stay_hidden_after_the_read():
    config = TraceConfig(hide_outputs=True)
    with FakeReplicate() as fake, instrumented(config=config, **READ_ON) as traced:
        list(default_client(fake).stream(STREAM_MODEL, input={}))
        traced.settle()

    values = attrs(traced.one())
    _final(values)
    assert values["output.value"] == "__REDACTED__"
    # The read returns the prediction's whole output; it never reaches the span.
    assert STREAM_TEXT not in traced.wire()
    assert TEXT_OUTPUT not in traced.wire()


@pytest.mark.parametrize(
    "value, error",
    [(-1, ValueError), (float("nan"), ValueError), (float("inf"), ValueError), (True, TypeError), ("2", TypeError)],
)
def test_stream_reload_seconds_is_validated(value, error):
    from opentelemetry.sdk.trace import TracerProvider
    from replicate.client import Client
    from traceai_replicate import ReplicateInstrumentor

    before = Client.__dict__["stream"]
    with pytest.raises(error, match="stream_reload_seconds must be"):
        ReplicateInstrumentor().instrument(tracer_provider=TracerProvider(), stream_reload_seconds=value)
    assert Client.__dict__["stream"] is before  # nothing was patched


# -- 429 ---------------------------------------------------------------------------------


def test_a_throttled_create_is_one_error_span_and_a_retry_by_the_caller_is_another():
    # replicate's RetryTransport retries only HEAD/GET/PUT/DELETE/OPTIONS/TRACE:
    # a POST create answered 429 reaches the caller as ReplicateError(429).
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake, _ThrottleOnce("POST"))
        with pytest.raises(ReplicateError) as raised:
            client.run(TEXT_MODEL, input={})
        assert client.run(TEXT_MODEL, input={}) == TEXT_TOKENS

    assert raised.value.status == 429
    failed, ok = traced.replicate_spans()
    assert failed.status.status_code is StatusCode.ERROR
    assert "429" in failed.status.description
    assert ok.status.status_code is StatusCode.OK
    assert fake.paths("POST") == ["/v1/models/acme/text-model/predictions"]  # the 429 never reached it


def test_a_throttled_stream_get_is_retried_inside_the_client_within_one_span():
    # The first GET is the SSE stream's: replicate's RetryTransport retries it.
    # (The opt-in read after ``done`` is never retried; see test_stream_read_offpath.)
    with FakeReplicate() as fake, instrumented() as traced:
        transport = _ThrottleOnce("GET", tracer=traced.tracer())
        client = make_client(fake, transport)
        assert _texts(client.stream(STREAM_MODEL, input={})) == STREAM_CHUNKS

    assert transport.throttled == 1
    span = traced.one()
    assert span.status.status_code is StatusCode.OK
    _no_final(attrs(span))  # the read after done is off by default
    assert fake.paths("GET") == ["/stream/pred0001"]
