import asyncio

import pytest
from elasticsearch import Elasticsearch
from fi_instrumentation import TraceConfig
from opentelemetry.trace import StatusCode

from traceai_elasticsearch import ElasticsearchInstrumentor

from .conftest import SECRET_TEXT, VECTOR


@pytest.fixture
def instrumented(provider):
    instrumentor = ElasticsearchInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig())
    yield instrumentor
    instrumentor.uninstrument()


def _only(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, [s.name for s in spans]
    return spans[0]


def _everything(exporter):
    return repr([(dict(s.attributes), [(e.name, dict(e.attributes or {})) for e in s.events]) for s in exporter.get_finished_spans()])


def test_search_is_a_retriever_span_with_index_query_and_documents(instrumented, clients, exporter):
    client, _ = clients
    client.search(index="docs", query={"match": {"title": "find me"}}, size=3)
    span = _only(exporter)
    a = dict(span.attributes)
    assert span.name == "elasticsearch.search"
    assert a["fi.span.kind"] == "RETRIEVER"
    assert a["db.system"] == "elasticsearch"
    assert a["db.operation.name"] == "search"
    assert a["db.vector.index.name"] == "docs" and a["db.namespace"] == "docs"
    assert a["db.vector.query.top_k"] == 3
    assert "find me" in a["gen_ai.retrieval.query"]
    assert a["db.vector.results.count"] == 2
    assert list(a["db.vector.results.ids"]) == ["d1", "d2"]
    assert list(a["db.vector.results.scores"]) == [1.5, 0.5]
    assert a["retrieval.documents.0.document.id"] == "d1"
    assert a["retrieval.documents.0.document.score"] == 1.5
    assert "private doc one" in a["retrieval.documents.0.document.content"]
    assert span.status.status_code != StatusCode.ERROR


def test_knn_search_records_k_candidates_field_and_dimensions_but_never_the_vector(instrumented, clients, exporter):
    client, _ = clients
    client.search(index="docs", knn={"field": "embedding", "query_vector": VECTOR, "k": 4, "num_candidates": 20,
                                     "filter": {"term": {"tag": "visible"}}})
    span = _only(exporter)
    a = dict(span.attributes)
    assert a["db.vector.query.top_k"] == 4
    assert a["elasticsearch.num_candidates"] == 20
    assert a["elasticsearch.field"] == "embedding"
    assert a["elasticsearch.query_vector_dimensions"] == len(VECTOR)
    assert a["fi.span.kind"] == "RETRIEVER"
    dump = _everything(exporter)
    for value in VECTOR:
        assert str(value) not in dump, "embedding values must never be recorded"


def test_documents_never_carry_embedding_vectors(instrumented, clients, exporter):
    client, _ = clients
    client.search(index="docs", query={"match_all": {}})
    dump = _everything(exporter)
    assert "private doc one" in dump
    for value in VECTOR:
        assert str(value) not in dump


def test_async_search_is_traced_once(instrumented, clients, exporter):
    _, make_async = clients

    async def run():
        client = make_async()
        try:
            return await client.search(index="docs", query={"match": {"title": "async find"}})
        finally:
            await client.close()

    response = asyncio.run(run())
    assert response["hits"]["hits"][0]["_id"] == "d1"
    span = _only(exporter)
    assert "async find" in dict(span.attributes)["gen_ai.retrieval.query"]


def test_index_bulk_and_delete_are_vector_db_spans(instrumented, clients, exporter):
    client, _ = clients
    client.index(index="docs", id="d1", document={"title": "doc", "embedding": VECTOR})
    client.bulk(operations=[{"index": {"_index": "docs", "_id": "d1"}}, {"title": "one", "embedding": VECTOR},
                            {"index": {"_index": "docs", "_id": "d2"}}, {"title": "two", "embedding": VECTOR}])
    client.delete(index="docs", id="d1")
    spans = {s.name: dict(s.attributes) for s in exporter.get_finished_spans()}
    assert set(spans) == {"elasticsearch.index", "elasticsearch.bulk", "elasticsearch.delete"}
    assert all(a["fi.span.kind"] == "VECTOR_DB" for a in spans.values())
    assert spans["elasticsearch.bulk"]["db.vector.upsert.count"] == 2
    assert spans["elasticsearch.bulk"]["elasticsearch.has_errors"] is False
    assert spans["elasticsearch.bulk"]["elasticsearch.indexed_count"] == 2
    assert spans["elasticsearch.index"]["elasticsearch.document_id"] == "d1"
    assert spans["elasticsearch.delete"]["elasticsearch.document_id"] == "d1"
    dump = _everything(exporter)
    for value in VECTOR:
        assert str(value) not in dump


def test_a_server_error_marks_the_span_and_records_only_the_error_type(instrumented, clients, server, exporter):
    client, _ = clients
    server.fail_with = 500
    with pytest.raises(Exception):
        client.search(index="docs", query={"match_all": {}})
    span = _only(exporter)
    assert span.status.status_code == StatusCode.ERROR
    assert dict(span.attributes)["error.type"]
    assert SECRET_TEXT not in _everything(exporter)


def test_each_call_ends_exactly_one_span_even_when_it_fails(instrumented, clients, server, exporter):
    client, _ = clients
    client.search(index="docs", query={"match_all": {}})
    server.fail_with = 404
    with pytest.raises(Exception):
        client.search(index="docs", query={"match_all": {}})
    assert len(exporter.get_finished_spans()) == 2


def test_uninstrument_restores_the_client_methods(provider, clients, exporter):
    client, _ = clients
    original = Elasticsearch.search
    instrumentor = ElasticsearchInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    assert Elasticsearch.search is not original
    instrumentor.uninstrument()
    assert Elasticsearch.search is original
    client.search(index="docs", query={"match_all": {}})
    assert exporter.get_finished_spans() == ()


def test_instrumenting_twice_does_not_duplicate_spans(provider, clients, exporter):
    client, _ = clients
    first = ElasticsearchInstrumentor()
    first.instrument(tracer_provider=provider)
    try:
        ElasticsearchInstrumentor().instrument(tracer_provider=provider)
        client.search(index="docs", query={"match_all": {}})
        assert len(exporter.get_finished_spans()) == 1
    finally:
        first.uninstrument()


def test_knn_search_method_is_traced_when_the_sdk_still_has_it(instrumented, clients, exporter):
    client, _ = clients
    if not hasattr(Elasticsearch, "knn_search"):
        pytest.skip("this elasticsearch-py release has no knn_search")
    client.knn_search(index="docs", knn={"field": "embedding", "query_vector": VECTOR, "k": 2, "num_candidates": 5})
    span = _only(exporter)
    assert span.name == "elasticsearch.knn_search"
    assert dict(span.attributes)["db.vector.query.top_k"] == 2
