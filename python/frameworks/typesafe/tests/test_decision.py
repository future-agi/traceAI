"""AC-01..AC-05, AC-10: one EVALUATOR span per system_one call, typed answers as numbers."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("typesafe_sdk")

from opentelemetry.trace import StatusCode  # noqa: E402

from tests._support import (  # noqa: E402
    API_KEY,
    BOX,
    CHOICE_CONFIDENCE,
    INPUT_TOKENS,
    NOUL,
    OUTPUT_TOKENS,
    PROBABILITY_MARKERS,
    REQUEST_MODEL,
    RESOLVED_MODEL,
    SCORE,
    SCORE_CONFIDENCE,
    STATE,
    FakeSystemOne,
    client,
    has_box,
    everything,
    instrumented,
    only_span,
    questions,
)

Q = "typesafe.question."


def _call(**kwargs):
    fake = FakeSystemOne()
    with instrumented() as exporter:
        with client(fake) as typesafe:
            response = typesafe.system_one(STATE, questions(), **kwargs)
    return fake, response, only_span(exporter)


def test_one_evaluator_span_for_every_question():
    fake, response, span = _call(model=REQUEST_MODEL)
    attributes = dict(span.attributes)
    assert len(fake.requests) == 1
    assert span.name == "typesafe.system_one"
    assert attributes["gen_ai.span.kind"] == "EVALUATOR"
    assert attributes["gen_ai.provider.name"] == "typesafe"
    assert attributes["typesafe.question_count"] == len(questions()) == (4 if has_box() else 3)
    assert span.status.status_code is StatusCode.OK
    # The caller still gets the SDK's own typed response.
    assert response.nouls["billing"].noul == NOUL
    assert response.choices["tone"].choice == "angry"


def test_each_answer_type_is_its_own_number_or_label():
    _, _, span = _call()
    a = dict(span.attributes)
    assert a[Q + "billing.type"] == "noul"
    assert a[Q + "billing.noul"] == NOUL
    assert a[Q + "tone.type"] == "choice"
    assert a[Q + "tone.choice"] == "angry"
    assert a[Q + "tone.confidence"] == CHOICE_CONFIDENCE
    assert a[Q + "urgency.type"] == "score"
    assert a[Q + "urgency.score"] == SCORE
    assert a[Q + "urgency.confidence"] == SCORE_CONFIDENCE
    if has_box():
        assert a[Q + "logo.type"] == "box"
        for edge, value in BOX.items():
            assert a[Q + "logo.box." + edge] == value
    # AC-02: never a Pass/Fail string, never a boolean.
    for key, value in a.items():
        if key.startswith(Q):
            assert value not in ("Pass", "Fail", "pass", "fail", True, False), key


def test_noul_and_null_box_confidence_are_absent_not_zero():
    _, _, span = _call()
    a = dict(span.attributes)
    assert Q + "billing.confidence" not in a  # AC-03
    assert Q + "logo.confidence" not in a  # the API sent null


def test_no_probability_map_no_legend_no_explanation():
    _, _, span = _call()
    dump = everything(span)
    for marker in PROBABILITY_MARKERS:  # AC-10
        assert repr(marker) not in dump
    assert "probabilities" not in dump
    assert "legend" not in dump
    for key in span.attributes:
        assert "explanation" not in key and "reason" not in key  # AC-04
    assert "gen_ai.evaluation.explanation" not in span.attributes


def test_request_model_is_kept_and_response_model_recorded_separately():
    _, _, span = _call()  # no model=: the SDK default jev-latest is sent
    a = dict(span.attributes)
    assert a["gen_ai.request.model"] == REQUEST_MODEL
    assert a["gen_ai.response.model"] == RESOLVED_MODEL
    assert a["typesafe.response.model"] == RESOLVED_MODEL


def test_response_model_attribute_only_when_it_differs():
    _, _, span = _call(model="jev-1.13.0")
    a = dict(span.attributes)
    assert a["gen_ai.request.model"] == "jev-1.13.0"
    assert a["gen_ai.response.model"] == "jev-1.13.0"
    assert "typesafe.response.model" not in a


def test_client_default_model_and_extra_body_override(monkeypatch):
    monkeypatch.setenv("TYPESAFE_DEFAULT_MODEL", "jev-1.13.0")
    fake = FakeSystemOne()
    with instrumented() as exporter:
        with client(fake) as typesafe:
            typesafe.system_one(STATE, questions())
            typesafe.system_one(STATE, questions(), model="ignored", extra_body={"model": REQUEST_MODEL})
    first, second = exporter.get_finished_spans()
    assert first.attributes["gen_ai.request.model"] == "jev-1.13.0"
    assert json.loads(fake.requests[1].content)["model"] == REQUEST_MODEL
    assert second.attributes["gen_ai.request.model"] == REQUEST_MODEL


def test_usage_tokens_request_id_and_host_no_cost():
    _, _, span = _call()
    a = dict(span.attributes)
    assert a["gen_ai.usage.input_tokens"] == INPUT_TOKENS
    assert a["gen_ai.usage.output_tokens"] == OUTPUT_TOKENS
    assert a["gen_ai.usage.total_tokens"] == INPUT_TOKENS + OUTPUT_TOKENS
    assert a["typesafe.request_id"] == "req_fake_1"
    assert a["server.address"] == "api.typesafe.ai"
    assert not any("cost" in key or "price" in key for key in a)


def test_base_url_is_the_callers_choice():
    fake = FakeSystemOne()
    with instrumented() as exporter:
        with client(fake, base_url="https://openrouter.ai/api") as typesafe:
            typesafe.system_one(STATE, questions())
    span = only_span(exporter)
    assert str(fake.requests[0].url) == "https://openrouter.ai/api/v1/systemone"
    assert span.attributes["server.address"] == "openrouter.ai"


def test_api_key_is_never_on_the_span():  # AC-05
    fake = FakeSystemOne()
    with instrumented() as exporter:
        with client(fake) as typesafe:
            typesafe.system_one("my key is {0}".format(API_KEY), questions())
    dump = everything(only_span(exporter))
    assert API_KEY not in dump
    assert "[redacted]" in dump


def test_raw_dictionary_questions_are_read_too():
    fake = FakeSystemOne()
    with instrumented() as exporter:
        with client(fake) as typesafe:
            typesafe.system_one(
                state={"message": STATE},
                questions={
                    "spam": {"type": "noul", "instructions": "Is this spam?"},
                    "lang": {"type": "choice", "criteria": {"en": None, "fr": None}},
                },
            )
    a = dict(only_span(exporter).attributes)
    assert a[Q + "spam.noul"] == NOUL
    assert a[Q + "lang.choice"] == "en"
    assert json.loads(a["input.value"])["state"] == {"message": STATE}
