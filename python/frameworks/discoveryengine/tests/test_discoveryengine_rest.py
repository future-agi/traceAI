"""The REST transport (``transport="rest"``): same spans as gRPC, and REST error statuses.

A loopback HTTP server answers the documented v1 REST routes
(``POST /v1/{serving_config}:search``, ``:searchLite`` and ``:answer``) with
JSON written by the library's own response types, and a Google JSON error
(``{"error": {"code", "message", "status"}}``) for failures. The real
clients build the requests, send them over HTTP and parse the responses.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

import pytest
from google.api_core import exceptions as core_exceptions
from google.auth.credentials import AnonymousCredentials
from opentelemetry.trace import StatusCode

from _discoveryengine_support import (
    ANSWER_REFERENCES,
    ANSWER_TEXT,
    CONTENT_MARKERS,
    DOCUMENT_TITLE,
    SERVING_CONFIG,
    SESSION_NAME,
    SNIPPET,
    attrs,
    event,
    instrumented,
)

REDACTED_VALUE = "__REDACTED__"
QUERY = "REST-QUERY-MARKER"
API_KEY = "placeholder-rest-api-key-must-not-be-exported"
ROUTE = re.compile(r"^/v1/(projects/.+/servingConfigs/[^/:]+):(search|searchLite|answer)$")


def _search_body() -> str:
    from google.cloud.discoveryengine_v1.types import SearchResponse

    results = [
        {
            "id": "doc-{0}".format(index),
            "document": {
                "id": "doc-{0}".format(index),
                "derived_struct_data": {"title": DOCUMENT_TITLE, "snippets": [{"snippet": SNIPPET}]},
            },
        }
        for index in range(3)
    ]
    return SearchResponse.to_json(SearchResponse(results=results, total_size=3))


def _answer_body() -> str:
    from google.cloud.discoveryengine_v1.types import Answer, AnswerQueryResponse, Session

    response = AnswerQueryResponse(
        answer=Answer(
            state=Answer.State.SUCCEEDED,
            answer_text=ANSWER_TEXT,
            references=[{"chunk_info": {"content": "chunk"}}] * ANSWER_REFERENCES,
        ),
        session=Session(name=SESSION_NAME),
    )
    return AnswerQueryResponse.to_json(response)


class _Handler(BaseHTTPRequestHandler):
    calls: List[Dict[str, Any]] = []

    def log_message(self, *args: Any) -> None:
        pass

    def _send(self, status: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        type(self).calls.append({"path": self.path, "headers": dict(self.headers), "body": body})
        match = ROUTE.match(self.path.split("?", 1)[0])
        if not match:
            self._send(404, json.dumps({"error": {"code": 404, "message": "no route", "status": "NOT_FOUND"}}))
            return
        query = body.get("query")
        text = query.get("text", "") if isinstance(query, dict) else (query or "")
        if text.startswith("fail-denied"):
            self._send(
                403,
                json.dumps(
                    {
                        "error": {
                            "code": 403,
                            "message": "Permission denied for query '{0}' with key {1}".format(
                                text, self.headers.get("x-goog-api-key")
                            ),
                            "status": "PERMISSION_DENIED",
                        }
                    }
                ),
            )
        elif text.startswith("fail-odd-status"):
            self._send(
                400, json.dumps({"error": {"code": 400, "message": "bad", "status": "free text " + text}})
            )
        elif match.group(2) == "answer":
            self._send(200, _answer_body())
        else:
            self._send(200, _search_body())


@pytest.fixture()
def endpoint():
    _Handler.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:{0}".format(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def _clients(endpoint: str, **options: Any):
    from google.cloud.discoveryengine_v1 import ConversationalSearchServiceClient, SearchServiceClient

    client_options = dict({"api_endpoint": endpoint}, **options)
    credentials = None if "api_key" in options else AnonymousCredentials()
    return (
        SearchServiceClient(credentials=credentials, transport="rest", client_options=client_options),
        ConversationalSearchServiceClient(
            credentials=credentials, transport="rest", client_options=client_options
        ),
    )


def test_rest_calls_get_the_same_spans_as_grpc(endpoint):
    search, answers = _clients(endpoint)
    with instrumented() as traced:
        pager = search.search(request={"serving_config": SERVING_CONFIG, "query": QUERY})
        assert len(pager.results) == 3
        search.search_lite(request={"serving_config": SERVING_CONFIG, "query": QUERY})
        response = answers.answer_query(
            request={"serving_config": SERVING_CONFIG, "query": {"text": QUERY}}
        )
        assert response.answer.answer_text == ANSWER_TEXT

    assert [call["path"].split("?")[0].rsplit(":", 1)[1] for call in _Handler.calls] == [
        "search",
        "searchLite",
        "answer",
    ]
    search_span, lite_span, answer_span = traced.spans()
    for span, name in ((search_span, "search"), (lite_span, "search_lite")):
        assert span.name == "discoveryengine." + name
        assert attrs(span) == {
            "gen_ai.span.kind": "RETRIEVER",
            "discoveryengine.serving_config": SERVING_CONFIG,
            "discoveryengine.result_count": 3,
            "gen_ai.retrieval.query": QUERY,
            "input.value": QUERY,
        }
        assert span.status.status_code is StatusCode.OK
    assert attrs(answer_span) == {
        "gen_ai.span.kind": "RETRIEVER",
        "discoveryengine.serving_config": SERVING_CONFIG,
        "discoveryengine.result_count": ANSWER_REFERENCES,
        "discoveryengine.answer.length": len(ANSWER_TEXT),
        "discoveryengine.answer.state": "SUCCEEDED",
        "discoveryengine.session": SESSION_NAME,
        "gen_ai.retrieval.query": QUERY,
        "input.value": QUERY,
    }
    wire = traced.wire()
    for marker in CONTENT_MARKERS:
        assert marker not in wire, marker


def test_a_rest_error_records_the_status_from_the_json_body(endpoint):
    # google-api-core maps an HTTP 403 to Forbidden, which has no gRPC
    # status; the server's "status" field is recorded instead.
    search, _ = _clients(endpoint)
    with instrumented() as traced:
        with pytest.raises(core_exceptions.Forbidden) as raised:
            search.search(request={"serving_config": SERVING_CONFIG, "query": "fail-denied " + QUERY})

    assert raised.value.grpc_status_code is None
    span = traced.one()
    values = attrs(span)
    assert values["discoveryengine.error.status"] == "PERMISSION_DENIED"
    assert values["discoveryengine.error.code"] == 403
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description.startswith("Forbidden: 403 POST ")
    assert sorted(event(span, "exception")) == ["exception.message", "exception.type"]
    # Default: the query is recorded, so the server's echo of it stays.
    assert QUERY in span.status.description


def test_with_hide_inputs_the_query_is_removed_from_rest_error_text(endpoint, monkeypatch):
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    search, _ = _clients(endpoint)
    with instrumented() as traced:
        with pytest.raises(core_exceptions.Forbidden):
            search.search(request={"serving_config": SERVING_CONFIG, "query": "fail-denied " + QUERY})

    span = traced.one()
    assert attrs(span)["input.value"] == REDACTED_VALUE
    assert "gen_ai.retrieval.query" not in attrs(span)
    assert REDACTED_VALUE in span.status.description
    assert QUERY not in traced.wire()


def test_a_rest_api_key_is_never_exported(endpoint):
    # search_lite with client_options.api_key, the set-up Google documents
    # for public website search; the key travels in the x-goog-api-key header.
    search, _ = _clients(endpoint, api_key=API_KEY)
    with instrumented() as traced:
        search.search_lite(request={"serving_config": SERVING_CONFIG, "query": QUERY})
        with pytest.raises(core_exceptions.Forbidden) as raised:
            search.search_lite(request={"serving_config": SERVING_CONFIG, "query": "fail-denied " + QUERY})

    assert API_KEY in str(raised.value)
    assert all(call["headers"].get("x-goog-api-key") == API_KEY for call in _Handler.calls)
    assert all(API_KEY not in call["path"] for call in _Handler.calls)
    assert API_KEY not in traced.wire()
    assert "[redacted]" in traced.spans()[1].status.description


def test_only_a_canonical_rest_status_name_is_recorded(endpoint):
    search, _ = _clients(endpoint)
    with instrumented() as traced:
        with pytest.raises(core_exceptions.BadRequest):
            search.search(request={"serving_config": SERVING_CONFIG, "query": "fail-odd-status " + QUERY})

    values = attrs(traced.one())
    assert "discoveryengine.error.status" not in values
    assert values["discoveryengine.error.code"] == 400
