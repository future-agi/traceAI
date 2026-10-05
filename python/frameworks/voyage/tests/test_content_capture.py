"""Content: the rerank query and scores by default (PRD J2 / AC-03); texts and
documents only with capture_content=True; TraceConfig and key redaction apply."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("voyageai", reason="voyageai must be installed to test its instrumentor")

import voyageai  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402
from traceai_voyage import VoyageInstrumentor  # noqa: E402

from _support import (  # noqa: E402
    DOCUMENTS,
    EMBED_MODEL,
    OPT_IN_MARKERS,
    QUERY,
    RERANK_MODEL,
    SCORE_MARKERS,
    SCORES,
    TEXTS,
    VECTOR_MARKER,
    VOYAGE_KEY,
    FakeVoyage,
    attrs,
    client,
    instrumented,
)

OTHER_KEY = "al-placeholder-module-key-must-not-be-exported"
# The fake returns documents 1 and 0 for top_k=2, in that order.
TOP_2_SCORES = [
    {"index": 1, "relevance_score": SCORES[1]},
    {"index": 0, "relevance_score": SCORES[0]},
]


@pytest.fixture()
def fake():
    with FakeVoyage() as server:
        yield server


def _embed_and_rerank(fake, **options):
    with instrumented(**options) as traced:
        voyage = client(fake)
        voyage.embed(TEXTS, model=EMBED_MODEL, input_type="document")
        voyage.rerank(QUERY, DOCUMENTS, model=RERANK_MODEL, top_k=2)
    embed, rerank = traced.spans()
    return traced, attrs(embed), attrs(rerank)


def test_default_config_records_the_rerank_query_and_scores(fake):
    _traced, _embed, rerank = _embed_and_rerank(fake)

    # PRD J2 step 2 / AC-03: the RERANKER span carries the query and scores.
    assert rerank["reranker.query"] == QUERY
    assert rerank["input.value"] == QUERY
    assert rerank["input.mime_type"] == "text/plain"
    assert json.loads(rerank["output.value"]) == TOP_2_SCORES
    assert rerank["output.mime_type"] == "application/json"


def test_default_config_records_no_embed_texts_rerank_documents_or_vectors(fake):
    traced, embed, rerank = _embed_and_rerank(fake)

    for key in ("input.value", "input.mime_type", "output.value", "output.mime_type"):
        assert key not in embed, key
    for key in ("reranker.input_documents", "reranker.output_documents"):
        assert not any(name.startswith(key) for name in rerank), key
    wire = traced.wire()
    for marker in OPT_IN_MARKERS + (VECTOR_MARKER, VOYAGE_KEY):
        assert marker not in wire, marker
    # The counts stay exact.
    assert embed["voyage.embedding.count"] == 2
    assert rerank["voyage.rerank.document_count"] == 3


def test_the_default_query_is_key_redacted_and_capped(fake, monkeypatch):
    monkeypatch.setattr(voyageai, "api_key", OTHER_KEY)
    with instrumented() as traced:
        voyage = client(fake)
        voyage.rerank("q {0} {1}".format(VOYAGE_KEY, OTHER_KEY), DOCUMENTS, model=RERANK_MODEL)
        voyage.rerank("é" * 3000, DOCUMENTS, model=RERANK_MODEL)  # 6000 UTF-8 bytes

    keyed, long = (attrs(span) for span in traced.spans())
    assert keyed["reranker.query"] == keyed["input.value"] == "q [redacted] [redacted]"
    # Cut to 2 KB of UTF-8 on a character boundary.
    assert long["reranker.query"] == long["input.value"] == "é" * 1024
    wire = traced.wire()
    assert VOYAGE_KEY not in wire
    assert OTHER_KEY not in wire


_HIDE_CASES = [
    # (TraceConfig kwargs, environment, query kept, scores kept)
    pytest.param({"hide_inputs": True}, {}, False, True, id="hide_inputs"),
    pytest.param({"hide_input_text": True}, {}, False, True, id="hide_input_text"),
    pytest.param({"hide_outputs": True}, {}, True, False, id="hide_outputs"),
    pytest.param({"hide_inputs": True, "hide_outputs": True}, {}, False, False, id="hide_both"),
    pytest.param({}, {"FI_HIDE_INPUTS": "true"}, False, True, id="FI_HIDE_INPUTS"),
    pytest.param({}, {"FI_HIDE_OUTPUTS": "true"}, True, False, id="FI_HIDE_OUTPUTS"),
    pytest.param(
        {},
        {"FI_HIDE_INPUTS": "true", "FI_HIDE_OUTPUTS": "true"},
        False,
        False,
        id="FI_HIDE_both",
    ),
]


@pytest.mark.parametrize("capture_content", [False, True], ids=["default", "capture"])
@pytest.mark.parametrize(("config", "environment", "query_kept", "scores_kept"), _HIDE_CASES)
def test_hide_flags_drop_the_query_and_the_scores_independently(
    fake, monkeypatch, capture_content, config, environment, query_kept, scores_kept
):
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    options = {"capture_content": capture_content}
    if config:
        options["config"] = TraceConfig(**config)
    traced, embed, rerank = _embed_and_rerank(fake, **options)
    wire = traced.wire()

    if query_kept:
        assert rerank["reranker.query"] == QUERY
    else:
        for values in (embed, rerank):
            assert "reranker.query" not in values
            assert "input.value" not in values
            assert "input.mime_type" not in values
        assert QUERY not in wire
    # Scores are numbers, not input text: hiding inputs keeps them (PRD J2).
    if scores_kept:
        assert json.loads(rerank["output.value"]) == TOP_2_SCORES
    else:
        assert "output.value" not in rerank
        assert "output.mime_type" not in rerank
        for marker in SCORE_MARKERS:
            assert marker not in wire, marker
    # Texts and documents only with capture on and inputs not hidden.
    for marker in OPT_IN_MARKERS:
        assert (marker in wire) is (capture_content and query_kept), marker
    # Counts stay exact whatever is hidden; vectors and the key never appear.
    assert embed["voyage.embedding.count"] == 2
    assert rerank["voyage.rerank.document_count"] == 3
    assert rerank["voyage.rerank.result_count"] == 2
    assert VECTOR_MARKER not in wire
    assert VOYAGE_KEY not in wire


def test_capture_content_records_inputs_and_scores_but_never_vectors(fake):
    traced, embed, rerank = _embed_and_rerank(fake, capture_content=True)

    assert json.loads(embed["input.value"]) == TEXTS
    assert embed["input.mime_type"] == "application/json"
    assert embed["voyage.embedding.count"] == 2
    assert rerank["reranker.query"] == QUERY
    assert json.loads(rerank["input.value"]) == {"query": QUERY, "documents": DOCUMENTS}
    # Scores in the client's order, with the index of the document they rank.
    assert json.loads(rerank["output.value"]) == [
        {"index": 1, "relevance_score": SCORES[1]},
        {"index": 0, "relevance_score": SCORES[0]},
    ]
    assert rerank["output.mime_type"] == "application/json"
    # Vectors are never stored, even with capture on (PRD R-04).
    assert VECTOR_MARKER not in traced.wire()
    assert "output.value" not in embed


def test_hide_inputs_drops_texts_query_and_documents_but_keeps_counts_and_scores(fake):
    traced, embed, rerank = _embed_and_rerank(
        fake, capture_content=True, config=TraceConfig(hide_inputs=True)
    )

    for values in (embed, rerank):
        assert "input.value" not in values
        assert "input.mime_type" not in values
        assert "reranker.query" not in values
    assert embed["voyage.embedding.count"] == 2
    assert rerank["voyage.rerank.document_count"] == 3
    assert "output.value" in rerank
    wire = traced.wire()
    for marker in TEXTS + [QUERY] + DOCUMENTS:
        assert marker not in wire, marker
    assert SCORE_MARKERS[1] in wire


def test_hide_outputs_drops_scores_but_keeps_inputs(fake):
    traced, _embed, rerank = _embed_and_rerank(
        fake, capture_content=True, config=TraceConfig(hide_outputs=True)
    )

    assert "output.value" not in rerank
    assert "output.mime_type" not in rerank
    assert rerank["reranker.query"] == QUERY
    assert rerank["voyage.rerank.result_count"] == 2
    for marker in SCORE_MARKERS:
        assert marker not in traced.wire(), marker


def test_fi_hide_inputs_environment_variable_is_honoured(fake, monkeypatch):
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    traced, embed, rerank = _embed_and_rerank(fake, capture_content=True)

    assert "input.value" not in embed
    assert "reranker.query" not in rerank
    for marker in TEXTS + [QUERY] + DOCUMENTS:
        assert marker not in traced.wire(), marker


def test_the_api_key_is_redacted_from_captured_content(fake, monkeypatch):
    # The client's own key and a module-level key both count as secrets.
    monkeypatch.setattr(voyageai, "api_key", OTHER_KEY)
    with instrumented(capture_content=True) as traced:
        voyage = client(fake)
        voyage.embed(["prefix {0} suffix".format(VOYAGE_KEY), OTHER_KEY], model=EMBED_MODEL)
        voyage.rerank(
            "query {0}".format(VOYAGE_KEY), ["doc {0}".format(OTHER_KEY)], model=RERANK_MODEL
        )

    embed, rerank = (attrs(span) for span in traced.spans())
    assert json.loads(embed["input.value"]) == ["prefix [redacted] suffix", "[redacted]"]
    assert rerank["reranker.query"] == "query [redacted]"
    assert json.loads(rerank["input.value"])["documents"] == ["doc [redacted]"]
    wire = traced.wire()
    assert VOYAGE_KEY not in wire
    assert OTHER_KEY not in wire


def test_a_client_key_read_from_the_environment_is_redacted(fake, monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", OTHER_KEY)
    with instrumented(capture_content=True) as traced:
        voyageai.Client(base_url=fake.base_url).embed(
            ["text {0}".format(OTHER_KEY)], model=EMBED_MODEL
        )

    assert json.loads(attrs(traced.one())["input.value"]) == ["text [redacted]"]
    assert OTHER_KEY not in traced.wire()


def test_captured_content_is_capped(fake):
    texts = ["t{0}".format(i) for i in range(70)]
    long_text = "é" * 3000  # 6000 UTF-8 bytes
    with instrumented(capture_content=True) as traced:
        voyage = client(fake)
        voyage.embed(texts, model=EMBED_MODEL)
        voyage.embed([long_text], model=EMBED_MODEL)

    many, long = (attrs(span) for span in traced.spans())
    # The count is exact; the captured list stops at 64 items.
    assert many["voyage.embedding.count"] == 70
    assert json.loads(many["input.value"]) == texts[:64]
    # Each captured string is cut to 2 KB of UTF-8 on a character boundary.
    (captured,) = json.loads(long["input.value"])
    assert captured == "é" * 1024
    assert len(captured.encode("utf-8")) == 2048


@pytest.mark.parametrize("capture_content", [False, True], ids=["default", "capture"])
def test_rerank_scores_are_capped_and_the_result_count_stays_exact(fake, capture_content):
    documents = ["d{0}".format(i) for i in range(70)]
    with instrumented(capture_content=capture_content) as traced:
        result = client(fake).rerank(QUERY, documents, model=RERANK_MODEL)

    assert len(result.results) == 70
    values = attrs(traced.one())
    assert values["voyage.rerank.document_count"] == 70
    assert values["voyage.rerank.result_count"] == 70
    # The first 64 results, in the client's order.
    assert json.loads(values["output.value"]) == [
        {"index": item.index, "relevance_score": item.relevance_score}
        for item in result.results[:64]
    ]


def test_pii_redaction_in_trace_config_applies_to_captured_content(fake):
    email = "jane.doe@example.com"
    with instrumented(capture_content=True, config=TraceConfig(pii_redaction=True)) as traced:
        client(fake).embed(["contact {0} today".format(email)], model=EMBED_MODEL)

    assert email not in traced.wire()


def test_pii_redaction_applies_to_the_default_query_and_keeps_the_scores(fake):
    email = "jane.doe@example.com"
    with instrumented(config=TraceConfig(pii_redaction=True)) as traced:
        client(fake).rerank("mail {0}".format(email), DOCUMENTS, model=RERANK_MODEL, top_k=2)

    values = attrs(traced.one())
    assert values["reranker.query"] == values["input.value"] == "mail <EMAIL_ADDRESS>"
    # The fake's scores are short enough that no PII pattern matches them.
    assert json.loads(values["output.value"]) == TOP_2_SCORES
    assert email not in traced.wire()


@pytest.mark.parametrize(
    "options",
    [{"config": {"hide_inputs": True}}, {"capture_content": "yes"}, {"capture_content": 1}],
)
def test_invalid_options_raise_type_error_and_wrap_nothing(options):
    original = voyageai.Client.__dict__["embed"]
    instrumentor = VoyageInstrumentor()
    with pytest.raises(TypeError):
        instrumentor.instrument(**options)
    try:
        assert voyageai.Client.__dict__["embed"] is original
    finally:
        instrumentor.uninstrument()
