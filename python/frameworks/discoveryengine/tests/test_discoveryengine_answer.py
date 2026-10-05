"""J3, AC-04: ConversationalSearchServiceClient.answer_query is one RETRIEVER span."""

from __future__ import annotations

import pytest
from google.cloud.discoveryengine_v1 import AnswerQueryRequest, Query
from opentelemetry.trace import StatusCode

from _discoveryengine_support import (
    ANSWER_FAILED,
    ANSWER_MISSING,
    ANSWER_REFERENCES,
    ANSWER_TEXT,
    CONTENT_MARKERS,
    NEW_SESSION,
    SERVING_CONFIG,
    SESSION_NAME,
    FakeDiscoveryEngine,
    answer_client,
    answer_request,
    attrs,
    instrumented,
)

QUERY = "ANSWER-QUERY-OFF-BY-DEFAULT"


@pytest.fixture()
def fake():
    with FakeDiscoveryEngine() as server:
        yield server


def test_answer_query_is_one_retriever_span_with_counts_not_the_answer(fake):
    with instrumented() as traced:
        response = answer_client(fake).answer_query(request=answer_request(QUERY))

    assert response.answer.answer_text == ANSWER_TEXT
    span = traced.one()
    assert span.name == "discoveryengine.answer_query"
    assert attrs(span) == {
        "fi.span.kind": "RETRIEVER",
        "discoveryengine.serving_config": SERVING_CONFIG,
        "discoveryengine.result_count": ANSWER_REFERENCES,
        "discoveryengine.answer.length": len(ANSWER_TEXT),
        "discoveryengine.answer.state": "SUCCEEDED",
    }
    assert span.status.status_code is StatusCode.OK
    wire = traced.wire()
    assert QUERY not in wire
    for marker in CONTENT_MARKERS:
        assert marker not in wire, marker


def test_no_model_name_is_invented(fake):
    # AC-04: the 0.20.5 AnswerQueryResponse carries no model name, so the
    # span has none, even when the request names a model version.
    request = answer_request(answer_generation_spec={"model_spec": {"model_version": "MODEL-VERSION-MARKER"}})
    with instrumented(capture_query=True) as traced:
        answer_client(fake).answer_query(request=request)

    for key in attrs(traced.one()):
        assert "model" not in key and "token" not in key and "cost" not in key, key
    assert "MODEL-VERSION-MARKER" not in traced.wire()


def test_the_session_comes_from_the_request_then_the_response(fake):
    with instrumented() as traced:
        client = answer_client(fake)
        client.answer_query(request=answer_request(session=SESSION_NAME))
        # "sessions/-" starts a session; the response carries its real name.
        client.answer_query(request=answer_request(session=NEW_SESSION))
        client.answer_query(request=answer_request())

    named, created, none = traced.spans()
    assert attrs(named)["discoveryengine.session"] == SESSION_NAME
    assert attrs(created)["discoveryengine.session"] == SESSION_NAME
    assert "discoveryengine.session" not in attrs(none)


def test_a_failed_answer_state_is_an_error_span(fake):
    with instrumented() as traced:
        response = answer_client(fake).answer_query(request=answer_request(ANSWER_FAILED))

    assert response.answer.state.name == "FAILED"
    span = traced.one()
    assert attrs(span)["discoveryengine.answer.state"] == "FAILED"
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "answer state FAILED"
    # Nothing was raised, so there is no exception event.
    assert span.events == ()


def test_a_response_without_an_answer_omits_the_counts(fake):
    with instrumented() as traced:
        answer_client(fake).answer_query(request=answer_request(ANSWER_MISSING))

    values = attrs(traced.one())
    for key in ("discoveryengine.result_count", "discoveryengine.answer.length", "discoveryengine.answer.state"):
        assert key not in values, key
    assert traced.one().status.status_code is StatusCode.OK


@pytest.mark.parametrize(
    "request_factory",
    [
        pytest.param(lambda q: answer_request(q), id="dict"),
        pytest.param(lambda q: AnswerQueryRequest(answer_request(q)), id="message"),
        pytest.param(
            lambda q: {"serving_config": SERVING_CONFIG, "query": Query(text=q)}, id="dict-with-query-message"
        ),
    ],
)
def test_capture_query_records_the_query_text(fake, request_factory):
    with instrumented(capture_query=True) as traced:
        answer_client(fake).answer_query(request_factory("what is a retriever span"))

    values = attrs(traced.one())
    assert values["input.value"] == "what is a retriever span"
    assert values["gen_ai.retrieval.query"] == "what is a retriever span"
    assert ANSWER_TEXT not in traced.wire()
