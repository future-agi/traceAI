"""Response visibility through the real SDK, with no vendor connections."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from _support import EXA_KEY, FakeExa, attrs, instrumented
from exa_py import AsyncExa, Exa
from fi_instrumentation import TraceConfig
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

TEXT = "retrieved document content"
ANSWER = "answer backed by citations"
URL = "https://example.com/private?token=opaque"


@pytest.fixture()
def response(monkeypatch):
    document = {
        "id": URL,
        "url": URL,
        "title": "source title",
        "text": TEXT,
        "summary": "document summary",
        "highlights": ["relevant passage"],
    }
    payload = {
        "results": [document],
        "answer": ANSWER,
        "citations": [document],
        "costDollars": {
            "total": 0.012,
            "search": {"neural": 0.005},
            "contents": {"text": 0.007},
        },
    }

    def request(*args, **kwargs):
        return payload

    async def async_request(*args, **kwargs):
        return payload

    monkeypatch.setattr(Exa, "request", request)
    monkeypatch.setattr(AsyncExa, "async_request", async_request)
    return payload


def call(method, asynchronous=False):
    argument = [URL] if method == "get_contents" else "question"
    if asynchronous:

        async def run():
            client = AsyncExa(api_key=EXA_KEY)
            try:
                return await getattr(client, method)(argument)
            finally:
                await client.client.aclose()

        return asyncio.run(run())
    return getattr(Exa(api_key=EXA_KEY), method)(argument)


@pytest.mark.parametrize(
    "method", ["search", "search_and_contents", "get_contents", "answer"]
)
@pytest.mark.parametrize("asynchronous", [False, True])
def test_response_content_and_vendor_cost(response, method, asynchronous):
    with instrumented(capture_outputs=True, capture_urls=True) as traced:
        result = call(method, asynchronous)
    values = attrs(traced.one())
    output = json.loads(values["output.value"])
    documents = output["citations" if method == "answer" else "results"]
    assert documents[0]["text"] == TEXT
    assert documents[0]["url"] == URL
    assert documents[0]["title"] == "source title"
    if method == "answer":
        assert output["answer"] == ANSWER
        assert result.answer == ANSWER
    else:
        assert documents[0]["highlights"] == ["relevant passage"]
        assert documents[0]["summary"] == "document summary"
    assert values["output.mime_type"] == "application/json"
    assert values["gen_ai.cost.total"] == 0.012
    assert values["exa.cost.search.neural"] == 0.005
    assert values["exa.cost.contents.text"] == 0.007
    assert "gen_ai.cost.input" not in values
    assert not any("token" in key or "model" in key for key in values)


@pytest.mark.parametrize(
    "options", [{}, {"capture_urls": True}, {"config": TraceConfig(hide_outputs=False)}]
)
def test_content_capture_requires_explicit_opt_in(response, options):
    with instrumented(**options) as traced:
        call("answer")
    wire = encode_spans(traced.spans()).SerializeToString()
    assert ANSWER.encode() not in wire
    assert TEXT.encode() not in wire
    assert "output.value" not in attrs(traced.one())
    assert attrs(traced.one())["gen_ai.cost.total"] == 0.012


@pytest.mark.parametrize("flag", ["hide_outputs", "hide_output_text"])
def test_hidden_output_overrides_opt_in_on_otlp_wire(response, flag):
    with instrumented(
        capture_outputs=True, capture_urls=True, config=TraceConfig(**{flag: True})
    ) as traced:
        call("answer")
    wire = encode_spans(traced.spans()).SerializeToString()
    assert ANSWER.encode() not in wire
    assert TEXT.encode() not in wire
    assert URL.encode() not in wire
    assert attrs(traced.one())["exa.result_count"] == 1
    assert attrs(traced.one())["gen_ai.cost.total"] == 0.012


def test_environment_output_privacy(response, monkeypatch):
    monkeypatch.setenv("FI_HIDE_OUTPUTS", "true")
    with instrumented(capture_outputs=True) as traced:
        call("answer")
    assert ANSWER not in traced.wire()


def test_returned_urls_require_separate_opt_in(response):
    with instrumented(capture_outputs=True) as traced:
        call("answer")
    output = json.loads(attrs(traced.one())["output.value"])
    assert "url" not in output["citations"][0]
    assert "id" not in output["citations"][0]  # Exa IDs can themselves be URLs.
    assert URL not in traced.wire()


def test_content_is_scrubbed_before_serialization(response):
    response["answer"] = f"{EXA_KEY} question jane.doe@example.com"
    response["citations"][0]["text"] = response["answer"]
    with instrumented(
        capture_outputs=True,
        capture_urls=True,
        config=TraceConfig(hide_inputs=True, pii_redaction=True),
    ) as traced:
        call("answer")
    wire = encode_spans(traced.spans()).SerializeToString()
    for secret in [EXA_KEY, "question", "jane.doe@example.com"]:
        assert secret.encode() not in wire
    output = json.loads(attrs(traced.one())["output.value"])
    assert "<EMAIL_ADDRESS>" in output["answer"]
    assert "url" not in output["citations"][0]


def test_capture_is_bounded_and_does_not_mutate_results(response):
    response["results"] *= 100
    response["results"][0]["text"] = "界" * 10000
    with instrumented(capture_outputs=True, capture_urls=True) as traced:
        result = call("search")
    values = attrs(traced.one())
    assert len(values["output.value"].encode()) <= 16384
    documents = json.loads(values["output.value"])["results"]
    assert 0 < len(documents) <= 20
    assert len(documents[0]["text"].encode()) <= 1024
    assert values["exa.output_truncated"] is True
    assert values["exa.result_count"] == 100
    assert len(result.results) == 100
    assert result.results[0].text == "界" * 10000


@pytest.mark.parametrize("cost", [None, True, -1, float("nan"), float("inf"), "0.1"])
def test_no_fabricated_or_invalid_cost(response, cost):
    response["costDollars"] = {"total": cost}
    with instrumented() as traced:
        call("search")
    assert "gen_ai.cost.total" not in attrs(traced.one())


def test_zero_cost_is_preserved(response):
    response["costDollars"] = {"total": 0}
    with instrumented() as traced:
        call("search")
    assert attrs(traced.one())["gen_ai.cost.total"] == 0


@pytest.mark.parametrize("method", ["stream_answer", "stream_search"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_stream_capture_preserves_lifecycle_and_citations(method, asynchronous):
    with (
        FakeExa() as fake,
        instrumented(capture_outputs=True, capture_urls=True) as traced,
    ):
        if asynchronous:

            async def run():
                client = AsyncExa(api_key=EXA_KEY, api_base=fake.origin)
                try:
                    stream = await getattr(client, method)("question")
                    chunks = [chunk async for chunk in stream]
                    await stream.aclose()
                    return chunks
                finally:
                    await client.client.aclose()

            chunks = asyncio.run(run())
        else:
            stream = getattr(Exa(api_key=EXA_KEY, base_url=fake.origin), method)(
                "question"
            )
            chunks = list(stream)
            stream.close()
        values = attrs(traced.one())
        output = json.loads(values["output.value"])
        assert output["answer"] == "".join(chunk.content or "" for chunk in chunks)
        assert len(output["citations"]) == values["exa.result_count"] == 3
        assert "gen_ai.cost.total" not in values  # StreamChunk exposes no cost.


@pytest.mark.parametrize("flag", ["hide_outputs", "hide_output_text"])
def test_stream_output_privacy(flag):
    with (
        FakeExa() as fake,
        instrumented(
            capture_outputs=True, config=TraceConfig(**{flag: True})
        ) as traced,
    ):
        chunks = list(
            Exa(api_key=EXA_KEY, base_url=fake.origin).stream_answer("question")
        )
    wire = encode_spans(traced.spans()).SerializeToString()
    for chunk in chunks:
        if chunk.content:
            assert chunk.content.encode() not in wire


def test_cancelled_stream_does_not_export_partial_output():
    with FakeExa() as fake, instrumented(capture_outputs=True) as traced:
        stream = Exa(api_key=EXA_KEY, base_url=fake.origin).stream_answer("question")
        next(stream)
        stream.close()
    assert "output.value" not in attrs(traced.one())
    assert attrs(traced.one())["exa.cancelled"] is True


def test_unexpected_response_property_cannot_break_call(monkeypatch, caplog):
    class Response:
        results = ()

        @property
        def answer(self):
            raise RuntimeError(EXA_KEY)

    response = Response()
    monkeypatch.setattr(Exa, "answer", lambda *args, **kwargs: response)
    with (
        caplog.at_level("DEBUG", logger="traceai_exa"),
        instrumented(capture_outputs=True) as traced,
    ):
        assert call("answer") is response
    assert traced.one().status.status_code.name == "OK"
    assert "Exa response inspection failed" in caplog.text
    assert EXA_KEY not in caplog.text


def test_stream_redacts_keys_split_across_chunks(monkeypatch):
    chunks = [
        SimpleNamespace(content=part, citations=None)
        for part in [EXA_KEY[:9], EXA_KEY[9:], " jane.doe@", "example.com"]
    ]
    monkeypatch.setattr(Exa, "stream_answer", lambda *args, **kwargs: iter(chunks))
    with instrumented(
        capture_outputs=True, config=TraceConfig(pii_redaction=True)
    ) as traced:
        assert list(call("stream_answer")) == chunks
    output = json.loads(attrs(traced.one())["output.value"])
    assert EXA_KEY not in output["answer"]
    assert "jane.doe@example.com" not in output["answer"]
    assert "<EMAIL_ADDRESS>" in output["answer"]


def test_structured_answer_is_a_bounded_text_preview(response):
    response["answer"] = {"facts": [f"{EXA_KEY} fact"] * 1000}
    with instrumented(capture_outputs=True) as traced:
        returned = call("answer")
    values = attrs(traced.one())
    answer = json.loads(values["output.value"])["answer"]
    assert "fact" in answer
    assert EXA_KEY not in answer
    assert len(answer.encode()) <= 8192
    assert len(values["output.value"].encode()) <= 16384
    assert values["exa.output_truncated"] is True
    assert len(returned.answer["facts"]) == 1000


def test_stream_budget_and_document_limit(monkeypatch):
    document = SimpleNamespace(title="title", text="text" * 1024, url=URL)
    chunk = SimpleNamespace(content="界" * 10000, citations=[document] * 30)
    monkeypatch.setattr(
        Exa, "stream_answer", lambda *args, **kwargs: iter([chunk] * 50)
    )
    with instrumented(capture_outputs=True) as traced:
        assert len(list(call("stream_answer"))) == 50
    values = attrs(traced.one())
    output = json.loads(values["output.value"])
    assert len(values["output.value"].encode()) <= 16384
    assert len(output["answer"].encode()) <= 8192
    assert len(output["citations"]) <= 20
    assert values["exa.result_count"] == 1500
    assert values["exa.output_truncated"] is True


@pytest.mark.parametrize(
    "options, visible",
    [
        ({"capture_outputs": True}, True),
        ({"capture_outputs": True, "config": TraceConfig(hide_outputs=True)}, False),
        (
            {"capture_outputs": True, "config": TraceConfig(hide_output_text=True)},
            False,
        ),
        ({}, False),
    ],
)
def test_response_reaches_real_otlp_http_receiver(response, options, visible):
    import sys
    from pathlib import Path

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
    from harness import Receiver, _flatten_attributes

    response["answer"] = f"{ANSWER} {EXA_KEY}"
    with Receiver() as receiver, instrumented(**options) as traced:
        traced.provider.add_span_processor(
            SimpleSpanProcessor(
                OTLPSpanExporter(endpoint=receiver.endpoint, headers={}, timeout=2)
            )
        )
        call("answer")
        traced.provider.force_flush()
        spans = receiver.spans()
        assert len(spans) == 1
        values = _flatten_attributes(spans[0]["attributes"])
        assert values["gen_ai.cost.total"] == 0.012
        assert int(values["exa.result_count"]) == 1
        wire = json.dumps(spans)
        assert (ANSWER in wire) is visible
        assert EXA_KEY not in wire
        assert URL not in wire
        if visible:
            output = json.loads(values["output.value"])
            assert output["citations"][0]["text"] == TEXT
        traced.provider.shutdown()


def test_json_escaping_respects_the_total_byte_budget(response):
    response["answer"] = "\x00" * 8192
    with instrumented(capture_outputs=True) as traced:
        call("answer")
    values = attrs(traced.one())
    assert len(values["output.value"].encode()) <= 16384
    assert json.loads(values["output.value"])["answer"]
    assert values["exa.output_truncated"] is True


def test_stream_inspection_failure_does_not_drop_user_chunks(monkeypatch, caplog):
    class Chunk:
        citations = ()

        @property
        def content(self):
            raise RuntimeError(EXA_KEY)

    chunks = [Chunk(), SimpleNamespace(content="next chunk", citations=[])]
    monkeypatch.setattr(Exa, "stream_answer", lambda *args, **kwargs: iter(chunks))
    with (
        caplog.at_level("DEBUG", logger="traceai_exa"),
        instrumented(capture_outputs=True) as traced,
    ):
        assert list(call("stream_answer")) == chunks
    assert traced.one().status.status_code.name == "OK"
    assert "output.value" not in attrs(traced.one())
    assert "Exa stream inspection failed" in caplog.text
    assert EXA_KEY not in caplog.text


def test_bad_cost_metadata_does_not_disable_content_capture(monkeypatch, caplog):
    class Response:
        results = ()

        @property
        def cost_dollars(self):
            raise RuntimeError(EXA_KEY)

    response = Response()
    monkeypatch.setattr(Exa, "search", lambda *args, **kwargs: response)
    with (
        caplog.at_level("DEBUG", logger="traceai_exa"),
        instrumented(capture_outputs=True) as traced,
    ):
        assert call("search") is response
    values = attrs(traced.one())
    assert json.loads(values["output.value"]) == {"results": []}
    assert "gen_ai.cost.total" not in values
    assert "Exa cost inspection failed" in caplog.text
    assert EXA_KEY not in caplog.text
