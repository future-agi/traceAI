import pytest
from fi_instrumentation import TraceConfig

from traceai_elasticsearch import ElasticsearchInstrumentor

from .conftest import VECTOR

QUERY = "SENTINEL-QUERY-TEXT-31"
FILTER = "SENTINEL-FILTER-VALUE-32"
DOC_TEXT = "SENTINEL-DOC-TEXT-33"
INDEXED = "SENTINEL-INDEXED-TEXT-34"


def _run(provider, exporter, clients, server, config):
    client, _ = clients
    server_response = server.respond  # keep the default answers, then add the sentinel document

    def respond(method, target, body):
        status, payload = server_response(method, target, body)
        if target.split("?")[0].endswith("/_search"):
            payload["hits"]["hits"][0]["_source"]["title"] = DOC_TEXT
        return status, payload

    server.respond = respond
    instrumentor = ElasticsearchInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        client.search(index="docs", query={"bool": {"must": [{"match": {"title": QUERY}}],
                                                    "filter": [{"term": {"tag": FILTER}}]}}, size=5)
        client.index(index="docs", id="d1", document={"title": INDEXED, "embedding": VECTOR})
        client.bulk(operations=[{"index": {"_index": "docs", "_id": "d9"}}, {"title": INDEXED}])
    finally:
        instrumentor.uninstrument()
    return repr([(dict(s.attributes), [(e.name, dict(e.attributes or {})) for e in s.events], s.status.description)
                 for s in exporter.get_finished_spans()])


def test_content_is_recorded_when_no_hide_setting_is_on(provider, exporter, clients, server):
    dump = _run(provider, exporter, clients, server, TraceConfig())
    for value in (QUERY, FILTER, DOC_TEXT, INDEXED):
        assert value in dump, value


@pytest.mark.parametrize("flag", ["hide_inputs", "hide_input_messages", "hide_input_text"])
def test_input_settings_remove_query_filter_and_indexed_documents(provider, exporter, clients, server, flag):
    dump = _run(provider, exporter, clients, server, TraceConfig(**{flag: True}))
    for value in (QUERY, FILTER, INDEXED):
        assert value not in dump, (flag, value)
    assert DOC_TEXT in dump


@pytest.mark.parametrize("flag", ["hide_outputs", "hide_output_messages", "hide_output_text"])
def test_output_settings_remove_document_content_and_scores(provider, exporter, clients, server, flag):
    dump = _run(provider, exporter, clients, server, TraceConfig(**{flag: True}))
    assert DOC_TEXT not in dump, flag
    assert "4242" not in dump, "numeric document fields are content too"
    assert "retrieval.documents.0.document.score': 1.5" not in dump, "numeric scores are content too"
    assert QUERY in dump


def test_every_setting_together_leaves_no_content(provider, exporter, clients, server):
    dump = _run(provider, exporter, clients, server,
                TraceConfig(hide_inputs=True, hide_outputs=True, hide_input_messages=True, hide_output_messages=True,
                            hide_input_text=True, hide_output_text=True))
    for value in (QUERY, FILTER, DOC_TEXT, INDEXED, "4242"):
        assert value not in dump, value
    assert "elasticsearch" in dump and "docs" in dump


@pytest.mark.parametrize("config", [TraceConfig(), TraceConfig(hide_inputs=True), TraceConfig(hide_outputs=True),
                                    TraceConfig(hide_inputs=True, hide_outputs=True)])
def test_raw_payloads_and_whole_responses_are_never_recorded(provider, exporter, clients, server, config):
    _run(provider, exporter, clients, server, config)
    for span in exporter.get_finished_spans():
        attributes = dict(span.attributes)
        for key in ("raw.input", "raw.output", "fi.raw_input", "fi.raw_output", "output.value"):
            assert key not in attributes, (span.name, key)


def test_the_error_message_is_not_recorded_under_any_setting(provider, exporter, clients, server):
    client, _ = clients
    server.fail_with = 400
    instrumentor = ElasticsearchInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(hide_inputs=True))
    try:
        with pytest.raises(Exception):
            client.search(index="docs", query={"match": {"title": QUERY}})
    finally:
        instrumentor.uninstrument()
    dump = repr([(dict(s.attributes), s.status.description, [(e.name, dict(e.attributes or {})) for e in s.events])
                 for s in exporter.get_finished_spans()])
    assert QUERY not in dump and "failed synthetic" not in dump
