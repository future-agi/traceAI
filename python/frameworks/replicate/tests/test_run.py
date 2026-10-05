"""Client.run and the module-level replicate.run (PRD J1, AC-01, AC-05, AC-06)."""

from __future__ import annotations

import gc
import json
import types

import pytest

pytest.importorskip("replicate", reason="replicate must be installed to test its instrumentor")

import replicate  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402
from opentelemetry.trace import StatusCode  # noqa: E402
from replicate.exceptions import ModelError  # noqa: E402

from _support import (  # noqa: E402
    FAIL_MODEL,
    FILE_URL,
    FILE_URLS,
    FILES_MODEL,
    IMAGE_MODEL,
    ITERATOR_VERSION,
    MODEL_ERROR,
    PREDICT_TIME,
    PROMPT,
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


def test_run_text_model_is_one_llm_span():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        output = client.run(TEXT_MODEL, input={"prompt": PROMPT, "max_tokens": 8})

    assert output == TEXT_TOKENS  # the vendor's result, unchanged
    span = traced.one()
    assert span.name == "replicate.run"
    values = attrs(span)
    assert values["gen_ai.provider.name"] == "replicate"
    assert values["gen_ai.request.model"] == TEXT_MODEL
    assert values["gen_ai.span.kind"] == "LLM"
    assert values["replicate.prediction.id"] == "pred0001"
    assert values["replicate.prediction.status"] == "succeeded"
    assert values["replicate.prediction.version"] == "v-acme-text-model"
    assert values["replicate.metrics.predict_time"] == PREDICT_TIME
    assert values["replicate.output.type"] == "text"
    assert values["output.value"] == TEXT_OUTPUT
    assert values["output.mime_type"] == "text/plain"
    assert json.loads(values["input.value"]) == {"prompt": PROMPT, "max_tokens": 8}
    assert values["input.mime_type"] == "application/json"
    assert span.status.status_code is StatusCode.OK
    # R-08: no token usage and no cost, ever.
    assert not [key for key in values if "usage" in key or "cost" in key or "token" in key]


def test_polling_inside_run_is_one_span_and_http_spans_nest_under_it():
    with FakeReplicate() as fake, instrumented() as traced:
        transport = RecordingTransport(tracer=traced.tracer())
        client = make_client(fake, transport)
        # wait=False: no Prefer header, so the client polls GET /v1/predictions/{id}.
        assert client.run(TEXT_MODEL, input={"prompt": PROMPT}, wait=False) == TEXT_TOKENS

    assert fake.paths("GET").count("/v1/predictions/pred0001") == 2
    span = traced.one()
    http = [s for s in traced.spans() if s.name.startswith("HTTP ")]
    assert [s.name for s in http] == ["HTTP POST", "HTTP GET", "HTTP GET"]
    for child in http:
        assert child.parent is not None
        assert child.parent.span_id == span.context.span_id
    assert attrs(span)["replicate.prediction.status"] == "succeeded"
    params = json.loads(attrs(span)["gen_ai.request.parameters"])
    assert params["wait"] is False


def test_run_is_a_child_of_the_callers_span():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        with traced.tracer().start_as_current_span("caller") as caller:
            client.run(TEXT_MODEL, input={})

    span = traced.one()
    assert span.parent.span_id == caller.get_span_context().span_id


def test_file_output_is_a_url_string_and_never_fetched():
    with FakeReplicate() as fake, instrumented() as traced:
        transport = RecordingTransport()
        client = make_client(fake, transport)
        output = client.run(IMAGE_MODEL, input={"prompt": PROMPT})

    assert str(output) == FILE_URL
    values = attrs(traced.one())
    assert values["replicate.output.type"] == "url"
    assert values["output.value"] == FILE_URL
    assert values["gen_ai.span.kind"] == "CHAIN"  # LLM only for text output
    # AC-06: the URL is stored as a string; nothing ever requested it.
    assert not [url for _, url in transport.requests if "files.example.invalid" in url]


def test_file_list_output_records_the_urls_and_fetches_nothing():
    with FakeReplicate() as fake, instrumented() as traced:
        transport = RecordingTransport()
        client = make_client(fake, transport)
        output = client.run(FILES_MODEL, input={})

    assert [str(item) for item in output] == FILE_URLS
    values = attrs(traced.one())
    assert values["replicate.output.type"] == "list"
    assert json.loads(values["output.value"]) == FILE_URLS
    assert values["gen_ai.span.kind"] == "CHAIN"
    assert not [url for _, url in transport.requests if "files.example.invalid" in url]


def test_failed_prediction_raises_model_error_and_marks_the_span():
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        with pytest.raises(ModelError) as raised:
            client.run(FAIL_MODEL, input={"prompt": PROMPT})

    assert str(raised.value) == MODEL_ERROR
    span = traced.one()
    values = attrs(span)
    assert values["replicate.prediction.status"] == "failed"
    assert values["replicate.prediction.id"] == "pred0001"
    assert span.status.status_code is StatusCode.ERROR
    assert MODEL_ERROR in span.status.description
    (event,) = exception_events(span)
    assert event.attributes["exception.type"].endswith("ModelError")
    assert "output.value" not in values


def test_failed_error_string_is_redacted_when_outputs_are_hidden():
    with FakeReplicate() as fake, instrumented(config=TraceConfig(hide_outputs=True)) as traced:
        client = make_client(fake)
        with pytest.raises(ModelError):
            client.run(FAIL_MODEL, input={})

    span = traced.one()
    # AC-05: still an error, the error string is redacted rather than dropped.
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "__REDACTED__"
    (event,) = exception_events(span)
    assert event.attributes["exception.message"] == "__REDACTED__"
    assert MODEL_ERROR not in traced.wire()


def test_iterator_output_keeps_the_span_open_until_the_iterator_ends():
    ref = "{0}:{1}".format(TEXT_MODEL, ITERATOR_VERSION)
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        iterator = client.run(ref, input={"prompt": PROMPT}, wait=False)
        assert isinstance(iterator, types.GeneratorType)
        assert traced.replicate_spans() == []
        chunks = list(iterator)
        assert len(traced.replicate_spans()) == 1

    assert chunks == TEXT_TOKENS
    values = attrs(traced.one())
    assert values["replicate.prediction.version"] == ITERATOR_VERSION
    assert values["gen_ai.request.model"] == TEXT_MODEL
    assert values["output.value"] == TEXT_OUTPUT
    assert values["replicate.prediction.status"] == "succeeded"
    assert traced.one().status.status_code is StatusCode.OK


def test_closing_an_iterator_early_ends_the_span_once_as_cancelled():
    ref = "{0}:{1}".format(TEXT_MODEL, ITERATOR_VERSION)
    with FakeReplicate() as fake, instrumented() as traced:
        client = make_client(fake)
        iterator = client.run(ref, input={}, wait=False)
        next(iterator)
        iterator.close()
        # Asserted while the test still holds the iterator: close() ended it.
        span = traced.one()
        del iterator
        gc.collect()
        assert len(traced.replicate_spans()) == 1

    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "cancelled"
    assert attrs(span)["replicate.cancelled"] is True
    assert exception_events(span) == []


def test_module_level_run_is_traced_and_restored(monkeypatch):
    with FakeReplicate() as fake:
        client = make_client(fake)
        original = client.run
        monkeypatch.setattr(replicate, "run", original)
        with instrumented() as traced:
            assert replicate.run is not original
            assert replicate.run(TEXT_MODEL, input={"prompt": PROMPT}) == TEXT_TOKENS
        assert replicate.run is original

    assert traced.one().name == "replicate.run"


def test_default_client_functions_are_rebound_and_restored():
    names = ("run", "async_run", "stream", "async_stream")
    originals = {name: getattr(replicate, name) for name in names}
    with instrumented():
        for name in names:
            current = getattr(replicate, name)
            assert current is not originals[name], name
            assert current.__self__ is replicate.default_client, name
    for name in names:
        assert getattr(replicate, name) is originals[name], name
