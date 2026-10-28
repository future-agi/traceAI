"""Family consistency: span kinds, the retrieval query key, and PII in error text."""

from __future__ import annotations

import pytest

pytest.importorskip("tavily", reason="tavily-python must be installed to test its instrumentor")

from _support import attrs, instrumented  # noqa: E402
from _tavily_fake import FakeTavily  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402
from tavily import TavilyClient  # noqa: E402

KEY = "tvly-placeholder-consistency-key"


@pytest.fixture()
def fake():
    with FakeTavily() as server:
        yield server


def _client(fake: FakeTavily) -> TavilyClient:
    return TavilyClient(api_key=KEY, api_base_url=fake.origin)


def test_search_is_a_retriever_with_the_repo_query_key_and_extract_is_a_tool(fake):
    with instrumented() as traced:
        client = _client(fake)
        client.search("open telemetry")
        client.extract(["https://example.com/a"], query="rerank me")

    search, extract = traced.spans()
    assert attrs(search)["gen_ai.span.kind"] == "RETRIEVER"
    assert attrs(search)["gen_ai.retrieval.query"] == "open telemetry"
    assert attrs(extract)["gen_ai.span.kind"] == "TOOL"
    assert "gen_ai.retrieval.query" not in attrs(extract)
    assert attrs(extract)["input.value"] == "rerank me"


def test_hidden_inputs_drop_the_retrieval_query(fake):
    with instrumented(config=TraceConfig(hide_inputs=True)) as traced:
        _client(fake).search("private question")

    values = attrs(traced.one())
    assert values["input.value"] == "__REDACTED__"
    assert "gen_ai.retrieval.query" not in values
    assert "private question" not in traced.wire()


def test_pii_redaction_also_covers_error_text(fake):
    from _tavily_fake import ECHO_400  # the fake's 400 echoes the query

    with instrumented(config=TraceConfig(pii_redaction=True)) as traced:
        with pytest.raises(Exception):
            _client(fake).search("{0} mail jane.doe@example.com".format(ECHO_400))

    span = traced.one()
    (event,) = [event for event in span.events if event.name == "exception"]
    for text in (span.status.description, event.attributes["exception.message"],
                 event.attributes["exception.stacktrace"]):
        assert "jane.doe@example.com" not in text
    assert "<EMAIL_ADDRESS>" in event.attributes["exception.message"]
