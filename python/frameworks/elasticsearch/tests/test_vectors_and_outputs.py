import pytest
from fi_instrumentation import TraceConfig

from traceai_elasticsearch import ElasticsearchInstrumentor

from .conftest import VECTOR

np = pytest.importorskip("numpy")

DOC_TEXT = "SENTINEL-DOC-TEXT-41"


class ArrayLike:
    """Minimal array-like (what numpy and torch arrays expose): tolist, iteration and length."""

    def __init__(self, values):
        self._values = list(values)

    def tolist(self):
        return list(self._values)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


def _instrument(provider, config=None):
    instrumentor = ElasticsearchInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config or TraceConfig())
    return instrumentor


def _dump(exporter):
    return repr([(dict(s.attributes), [(e.name, dict(e.attributes or {})) for e in s.events])
                 for s in exporter.get_finished_spans()])


def _vector_forms():
    return [np.array(VECTOR), np.array(VECTOR, dtype=np.float32).astype(np.float64), [np.float32(v) for v in VECTOR],
            [np.float64(v) for v in VECTOR], ArrayLike(VECTOR)]


@pytest.mark.parametrize("form", range(4))
def test_array_like_vectors_are_never_recorded_by_index_bulk_and_search(provider, exporter, clients, form):
    client, _ = clients
    vector = _vector_forms()[form]
    instrumentor = _instrument(provider)
    try:
        client.search(index="docs", query={"script_score": {"query": {"match_all": {}},
                                                             "script": {"source": "cosineSimilarity(params.q, 'embedding') + 1.0",
                                                                        "params": {"q": vector}}}})
        for call in ("index", "bulk"):
            body = {"title": "doc", "embedding": vector}
            if call == "index":
                client.index(index="docs", id="d1", document=json_ready(body))
            else:
                client.bulk(operations=[{"index": {"_index": "docs", "_id": "d1"}}, json_ready(body)])
    finally:
        instrumentor.uninstrument()
    dump = _dump(exporter)
    for value in VECTOR:
        for text in (str(value), str(float(np.float32(value))), "%.4f" % value):
            assert text not in dump, (form, text)
    assert "[vector omitted]" in dump


def json_ready(body):
    """The client serializes numpy and array-likes only when they are lists; keep the request itself valid."""
    out = dict(body)
    vector = out["embedding"]
    out["embedding"] = vector.tolist() if hasattr(vector, "tolist") else list(vector)
    return out


def test_array_likes_are_omitted_as_vectors_in_any_request_content():
    from traceai_elasticsearch import _dump

    dumped = _dump({"q": ArrayLike(VECTOR), "nested": [ArrayLike(VECTOR)]}, 1000)
    assert dumped == '{"nested": ["[vector omitted]"], "q": "[vector omitted]"}'


def test_unknown_objects_in_documents_become_a_type_note_not_their_text():
    from traceai_elasticsearch import _dump

    class Secret:
        def __str__(self):
            return "SENTINEL-OBJECT-TEXT-42"

    dumped = _dump({"x": Secret(), "when": __import__("datetime").date(2026, 11, 19)}, 1000)
    assert "SENTINEL-OBJECT-TEXT-42" not in dumped
    assert "[Secret]" in dumped and "2026-11-19" in dumped


OUTPUT_FLAGS = ["hide_outputs", "hide_output_messages", "hide_output_text"]


@pytest.mark.parametrize("flag", OUTPUT_FLAGS)
def test_result_ids_scores_and_top_score_follow_the_output_settings(provider, exporter, clients, flag):
    client, _ = clients
    instrumentor = _instrument(provider, TraceConfig(**{flag: True}))
    try:
        client.search(index="docs", query={"match_all": {}})
    finally:
        instrumentor.uninstrument()
    attributes = dict(exporter.get_finished_spans()[0].attributes)
    for key in ("db.vector.results.ids", "db.vector.results.scores", "elasticsearch.top_score"):
        assert key not in attributes, (flag, key)
    assert attributes["db.vector.results.count"] == 2
    assert attributes["elasticsearch.total_hits"] == 2
    assert not any(k.startswith("retrieval.documents") for k in attributes)


def test_result_ids_and_scores_are_recorded_when_no_output_setting_is_on(provider, exporter, clients):
    client, _ = clients
    instrumentor = _instrument(provider, TraceConfig(hide_inputs=True))
    try:
        client.search(index="docs", query={"match_all": {}})
    finally:
        instrumentor.uninstrument()
    attributes = dict(exporter.get_finished_spans()[0].attributes)
    assert list(attributes["db.vector.results.ids"]) == ["d1", "d2"]
    assert list(attributes["db.vector.results.scores"]) == [1.5, 0.5]
    assert attributes["elasticsearch.top_score"] == 1.5


def test_number_and_boolean_content_is_recorded_when_no_hide_setting_is_on(provider, exporter, clients, server):
    client, _ = clients
    instrumentor = _instrument(provider)
    try:
        client.search(index="docs", query={"bool": {"filter": [{"term": {"in_stock": True}}, {"range": {"price": {"gte": 4242}}}]}})
        client.index(index="docs", id="d1", document={"price": 4243, "in_stock": True, "ratio": 0.4244})
    finally:
        instrumentor.uninstrument()
    spans = {s.name: dict(s.attributes) for s in exporter.get_finished_spans()}
    query = spans["elasticsearch.search"]["gen_ai.retrieval.query"]
    assert "4242" in query and "true" in query
    document = spans["elasticsearch.index"]["input.value"]
    assert "4243" in document and "true" in document and "0.4244" in document
    content = spans["elasticsearch.search"]["retrieval.documents.0.document.content"]
    assert "4242" in content and "true" in content
    assert spans["elasticsearch.search"]["retrieval.documents.0.document.score"] == 1.5


def test_the_deprecated_body_document_is_input_content_not_a_request_parameter(provider, exporter, clients):
    client, _ = clients
    instrumentor = _instrument(provider, TraceConfig(hide_inputs=True))
    try:
        client.index(index="docs", id="d1", body={"title": DOC_TEXT, "size": 99, "timeout": "SENTINEL-FIELD-43"})
    finally:
        instrumentor.uninstrument()
    dump = _dump(exporter)
    assert DOC_TEXT not in dump and "SENTINEL-FIELD-43" not in dump
    assert "99" not in dump


def test_a_bulk_request_given_as_body_counts_its_writes_and_gates_its_documents(provider, exporter, clients):
    client, _ = clients
    instrumentor = _instrument(provider, TraceConfig(hide_inputs=True))
    try:
        client.bulk(body=[{"index": {"_index": "docs", "_id": "d1"}}, {"title": DOC_TEXT},
                          {"index": {"_index": "docs", "_id": "d2"}}, {"title": DOC_TEXT}])
    finally:
        instrumentor.uninstrument()
    span = exporter.get_finished_spans()[0]
    assert dict(span.attributes)["db.vector.upsert.count"] == 2
    assert DOC_TEXT not in _dump(exporter)


def test_a_retriever_search_records_its_query_and_top_k_under_the_input_gate(provider, exporter, clients):
    from elasticsearch import Elasticsearch

    if "retriever" not in Elasticsearch.search.__wrapped__.__code__.co_varnames if hasattr(Elasticsearch.search, "__wrapped__") else False:
        pytest.skip("this client release has no retriever parameter")
    client, _ = clients
    retriever = {"knn": {"field": "embedding", "query_vector": VECTOR, "k": 6, "num_candidates": 30,
                         "filter": {"term": {"tag": "SENTINEL-RETRIEVER-44"}}}}
    instrumentor = _instrument(provider)
    try:
        client.search(index="docs", retriever=retriever)
    finally:
        instrumentor.uninstrument()
    attributes = dict(exporter.get_finished_spans()[0].attributes)
    assert attributes["db.vector.query.top_k"] == 6
    assert "SENTINEL-RETRIEVER-44" in attributes["gen_ai.retrieval.query"]
    for value in VECTOR:
        assert str(value) not in _dump(exporter)
    exporter.clear()
    instrumentor = _instrument(provider, TraceConfig(hide_inputs=True))
    try:
        client.search(index="docs", retriever=retriever)
    finally:
        instrumentor.uninstrument()
    assert "SENTINEL-RETRIEVER-44" not in _dump(exporter)
