"""Real marqo SDK calls with the HTTP layer stubbed: no Marqo server, no credentials, no network."""
import json
import socket

import marqo
import pytest
import requests
from fi_instrumentation import TraceConfig
from marqo.errors import MarqoWebError
from marqo.index import Index
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_marqo import MarqoInstrumentor

URL = "http://marqo.invalid:8882"
SECRET = "synthetic-api-key-value"
VECTOR = [0.1111, 0.2222, 0.3333]


class Server:
    """Records requests and answers like a Marqo server."""

    def __init__(self):
        self.requests = []
        self.fail_with = None

    def respond(self, request):
        path = request.url.split("?")[0].replace(URL, "")
        self.requests.append(request)
        if self.fail_with is not None:
            return self.fail_with, {"message": "boom " + SECRET, "code": "internal_error", "type": "x"}
        if path.endswith("/search"):
            return 200, {
                "hits": [
                    {"_id": "d1", "_score": 0.9, "title": "private doc one", "_highlights": [{"title": "private"}],
                     "_tensor_facets": [{"_embedding": VECTOR}]},
                    {"_id": "d2", "_score": 0.5, "title": "private doc two"},
                ],
                "processingTimeMs": 5, "query": "q", "limit": 10, "offset": 0,
            }
        if path.endswith("/documents") and request.method == "POST":
            return 200, {"errors": False, "processingTimeMs": 3.0, "index_name": "idx",
                         "items": [{"_id": "d1", "status": 200}, {"_id": "d2", "status": 200}]}
        if path.endswith("/documents"):
            return 200, {"results": [{"_id": "d1", "_found": True, "title": "private doc one"},
                                     {"_id": "gone", "_found": False}]}
        if "/documents/" in path:
            return 200, {"_id": "d1", "title": "private doc one"}
        return 200, {}


@pytest.fixture
def server(monkeypatch):
    server = Server()

    def send(self, request, **kwargs):
        status, body = server.respond(request)
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(body).encode()
        response.headers["content-type"] = "application/json"
        response.url = request.url
        response.request = request
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)

    def no_network(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    return server


@pytest.fixture
def index(server):
    return marqo.Client(url=URL, api_key=SECRET).index("idx")


def make_spans(config=None):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = MarqoInstrumentor()
    instrumentor.instrument(tracer_provider=provider, **({"config": config} if config else {}))
    return exporter, provider, instrumentor


@pytest.fixture
def spans():
    exporter, provider, instrumentor = make_spans()
    yield exporter
    instrumentor.uninstrument()
    provider.shutdown()


def one(spans):
    finished = spans.get_finished_spans()
    assert len(finished) == 1, [s.name for s in finished]
    return finished[0], dict(finished[0].attributes)


def test_search_records_retriever_span(spans, index):
    result = index.search("red shoes", limit=3, search_method="TENSOR", searchable_attributes=["title"])
    assert [h["_id"] for h in result["hits"]] == ["d1", "d2"]
    span, a = one(spans)
    assert a["gen_ai.span.kind"] == "RETRIEVER"
    assert a["gen_ai.provider.name"] == "marqo"
    assert a["db.system"] == "marqo"
    assert a["db.operation.name"] == "search"
    assert a["db.namespace"] == "idx"
    assert a["db.vector.index.name"] == "idx"
    assert a["db.vector.query.top_k"] == 3
    assert a["gen_ai.retrieval.query"] == "red shoes"
    params = json.loads(a["gen_ai.request.parameters"])
    assert params["limit"] == 3 and params["search_method"] == "TENSOR" and params["searchable_attributes"] == ["title"]
    assert a["db.vector.results.count"] == 2
    assert list(a["db.vector.results.ids"]) == ["d1", "d2"]
    assert list(a["db.vector.results.scores"]) == [0.9, 0.5]
    assert a["retrieval.documents.0.document.id"] == "d1"
    assert "private doc one" in a["retrieval.documents.0.document.content"]
    assert span.status.status_code != StatusCode.ERROR


def test_search_never_exports_vectors_or_credentials(spans, index):
    index.search("q", context={"tensor": [{"vector": VECTOR, "weight": 1}]})
    _, a = one(spans)
    dumped = json.dumps(a, default=str)
    assert "0.1111" not in dumped and "_tensor_facets" not in dumped
    content = a["retrieval.documents.0.document.content"]
    assert json.loads(content) == {"title": "private doc one"}  # no highlights, facets or vectors
    assert SECRET not in dumped
    assert "context" not in json.loads(a["gen_ai.request.parameters"])


def test_weighted_query_dict_is_captured_as_json(spans, index):
    index.search({"red": 1.0, "blue": -0.5})
    _, a = one(spans)
    assert json.loads(a["gen_ai.retrieval.query"]) == {"red": 1.0, "blue": -0.5}


def test_add_documents_records_count_params_and_status(spans, index):
    docs = [{"_id": "d1", "title": "private doc one"}, {"_id": "d2", "title": "private doc two"}]
    result = index.add_documents(docs, tensor_fields=["title"], client_batch_size=10)
    assert result[0]["errors"] is False if isinstance(result, list) else result["errors"] is False
    span, a = one(spans)
    assert a["gen_ai.span.kind"] == "CHAIN"
    assert a["db.operation.name"] == "add_documents"
    assert a["db.vector.index.name"] == "idx"
    assert a["db.vector.upsert.count"] == 2
    params = json.loads(a["gen_ai.request.parameters"])
    assert params["tensor_fields"] == ["title"] and params["client_batch_size"] == 10
    assert "private doc one" in a["input.value"]
    assert a["marqo.errors"] is False
    assert span.status.status_code != StatusCode.ERROR


def test_add_documents_omits_custom_vectors(spans, index):
    index.add_documents(
        [{"_id": "d1", "title": "t", "my_vector": {"vector": VECTOR, "content": "x"}, "raw": VECTOR * 8}],
        mappings={"my_vector": {"type": "custom_vector"}}, tensor_fields=["my_vector"],
    )
    _, a = one(spans)
    assert "0.1111" not in json.dumps(a, default=str)
    assert "mappings" not in json.loads(a["gen_ai.request.parameters"])


def test_get_documents_records_found_count(spans, index):
    out = index.get_documents(["d1", "gone"])
    assert len(out["results"]) == 2
    _, a = one(spans)
    assert a["gen_ai.span.kind"] == "RETRIEVER"
    assert a["db.operation.name"] == "get_documents"
    assert a["db.vector.results.count"] == 1
    assert list(a["db.vector.results.ids"]) == ["d1"]
    assert "private doc one" in a["retrieval.documents.0.document.content"]


def test_get_document_single(spans, index):
    assert index.get_document("d1")["_id"] == "d1"
    _, a = one(spans)
    assert a["db.operation.name"] == "get_document"
    assert a["db.vector.results.count"] == 1


def test_one_span_per_call(spans, index):
    index.search("a")
    index.add_documents([{"_id": "d1", "title": "t"}], tensor_fields=["title"])
    index.get_documents(["d1"])
    index.get_document("d1")
    assert [s.name for s in spans.get_finished_spans()] == [
        "marqo.search", "marqo.add_documents", "marqo.get_documents", "marqo.get_document"]


def test_error_status_type_no_message_and_reraised(spans, index, server):
    server.fail_with = 500
    with pytest.raises(MarqoWebError) as raised:
        index.search("q")
    span, a = one(spans)
    assert span.status.status_code == StatusCode.ERROR
    assert a["error.type"] == type(raised.value).__name__
    assert SECRET not in json.dumps(a, default=str) and "boom" not in json.dumps(a, default=str)
    assert not span.events
    assert "db.vector.results.count" not in a


def test_hidden_content_removes_query_documents_and_results(server):
    exporter, provider, instrumentor = make_spans(TraceConfig(
        hide_inputs=True, hide_outputs=True, hide_input_messages=True, hide_output_messages=True,
        hide_input_text=True, hide_output_text=True))
    try:
        index = marqo.Client(url=URL).index("idx")
        index.search("my private query")
        index.add_documents([{"_id": "d1", "title": "private body"}], tensor_fields=["title"])
        index.get_documents(["d1"])
        index.get_document("d1")
        finished = exporter.get_finished_spans()
        assert len(finished) == 4
        for span in finished:
            dumped = json.dumps(dict(span.attributes), default=str)
            assert "private" not in dumped, dict(span.attributes)
            assert span.attributes["db.vector.index.name"] == "idx"
        search = finished[0]
        assert search.attributes["db.vector.results.count"] == 2
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def test_hidden_output_text_alone_removes_document_bodies(server):
    exporter, provider, instrumentor = make_spans(TraceConfig(hide_output_text=True))
    try:
        marqo.Client(url=URL).index("idx").search("visible query")
        (span,) = exporter.get_finished_spans()
        dumped = json.dumps(dict(span.attributes), default=str)
        assert "private doc" not in dumped
        assert span.attributes["gen_ai.retrieval.query"] == "visible query"
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def test_uninstrument_restores_originals_and_stops_tracing(server):
    before = Index.search
    exporter, provider, instrumentor = make_spans()
    assert Index.search is not before
    instrumentor.uninstrument()
    provider.shutdown()
    assert Index.search is before
    marqo.Client(url=URL).index("idx").search("q")
    assert exporter.get_finished_spans() == ()


def test_large_add_documents_batch_bounds_capture(spans, index, monkeypatch):
    import traceai_marqo

    scrubbed = []
    real = traceai_marqo._scrub

    def spy(value, *rest):
        if isinstance(value, dict) and "_id" in value:  # a document, wherever it is reached from
            scrubbed.append(value)
        return real(value, *rest)

    monkeypatch.setattr(traceai_marqo, "_scrub", spy)
    docs = [{"_id": f"d{i}", "title": "t" * 50} for i in range(10000)]
    index.add_documents(docs, tensor_fields=["title"])
    _, a = one(spans)
    assert len(scrubbed) <= traceai_marqo._MAX_DOCUMENTS  # never the whole batch
    assert len(a["input.value"]) <= traceai_marqo._LIMIT
    assert a["db.vector.upsert.count"] == 10000


def test_add_documents_capture_stops_at_limit(spans, index):
    import traceai_marqo

    docs = [{"_id": f"d{i}", "title": "x" * 100000} for i in range(5)]
    index.add_documents(docs, tensor_fields=["title"])
    _, a = one(spans)
    assert len(a["input.value"]) <= traceai_marqo._LIMIT
    assert a["db.vector.upsert.count"] == 5


def test_start_failure_does_not_break_the_call(spans, index, monkeypatch):
    import traceai_marqo

    def broken(*a, **k):
        raise RuntimeError("tracing exploded")

    monkeypatch.setattr(traceai_marqo, "_start", broken)
    result = index.add_documents([{"_id": "d1", "title": "t"}], tensor_fields=["title"])
    assert result["items"][0]["_id"] == "d1"
    assert index.search("q")["hits"][0]["_id"] == "d1"
    assert spans.get_finished_spans() == ()
