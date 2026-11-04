"""Boundary cases for bounded capture and content-independent metrics."""

import copy
import json

import pytest
from fi_instrumentation import FITracer, TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

from traceai_cohere._request_attributes_extractor import (
    _ChatRequestAttributesExtractor,
    _RerankRequestAttributesExtractor,
)
from traceai_cohere._response_attributes_extractor import (
    _ChatResponseAttributesExtractor,
    _RerankResponseAttributesExtractor,
)
from traceai_cohere._stream import _Stream
from traceai_cohere._with_span import _WithSpan

from .test_telemetry_parity import CACHE_KEY, INPUT_DOC, OUTPUT_DOC, PRIVATE, TTFT_KEY


@pytest.mark.parametrize(
    "flag", ["hide_inputs", "hide_input_messages", "hide_input_text"]
)
def test_rerank_echo_and_query_selective_privacy(flag):
    config = TraceConfig(**{flag: True})
    request = {"query": PRIVATE, "documents": [PRIVATE]}
    response = {
        "results": [{"index": 0, "relevance_score": 0.9, "document": {"text": PRIVATE}}]
    }
    original = copy.deepcopy(response)
    attrs = dict(
        _RerankRequestAttributesExtractor(config).get_extra_attributes_from_request(
            request
        )
    )
    attrs.update(
        _RerankResponseAttributesExtractor(config).get_extra_attributes(
            response, request
        )
    )
    assert PRIVATE not in json.dumps(attrs)
    assert response == original
    assert attrs["cohere.rerank.0.relevance_score"] == 0.9


def test_rerank_capture_bounds_and_no_raw_bypass():
    documents = ["x" * 5000 + PRIVATE for _ in range(25)]
    request = {"query": "public query", "documents": documents}
    response = {
        "results": [
            {"index": i, "relevance_score": 0.9, "document": {"text": doc}}
            for i, doc in enumerate(documents)
        ]
    }
    original = copy.deepcopy((request, response))
    attrs = dict(
        _RerankRequestAttributesExtractor().get_extra_attributes_from_request(request)
    )
    attrs.update(
        _RerankResponseAttributesExtractor().get_extra_attributes(response, request)
    )
    assert attrs["cohere.documents_count"] == attrs["cohere.results_count"] == 25
    assert attrs[INPUT_DOC] == attrs[OUTPUT_DOC] == "x" * 4096
    assert attrs["reranker.input_documents.0.cohere.truncated"] is True
    assert attrs["reranker.output_documents.0.cohere.truncated"] is True
    assert attrs["cohere.rerank.input_documents_truncated"] is True
    assert attrs["cohere.rerank.output_documents_truncated"] is True
    assert not any("documents.10." in key or "rerank.10." in key for key in attrs)
    assert PRIVATE not in json.dumps(attrs)
    assert len(json.loads(attrs["output.value"])["results"]) == 10
    assert all(
        "document" not in result
        for result in json.loads(attrs["output.value"])["results"]
    )
    assert (request, response) == original


@pytest.mark.parametrize("index", [None, -1, 2, True, "0", 0.5])
def test_invalid_rerank_indices_never_map_to_candidates(index):
    response = {"results": [{"index": index, "relevance_score": 0.9}]}
    attrs = dict(
        _RerankResponseAttributesExtractor().get_extra_attributes(
            response, {"documents": ["first", "second"]}
        )
    )
    assert OUTPUT_DOC not in attrs
    assert attrs["cohere.rerank.0.relevance_score"] == 0.9


def test_rerank_does_not_drain_iterator():
    def documents():
        raise AssertionError("instrumentation consumed the caller's iterator")
        yield "unreachable"

    request = {"documents": documents()}
    attrs = dict(
        _RerankRequestAttributesExtractor().get_extra_attributes_from_request(request)
    )
    attrs.update(
        _RerankResponseAttributesExtractor().get_extra_attributes(
            {"results": [{"index": 0, "relevance_score": 0.9}]}, request
        )
    )
    assert INPUT_DOC not in attrs and OUTPUT_DOC not in attrs


@pytest.mark.parametrize("sdk_object", [False, True])
def test_structured_documents_preserve_all_fields(sdk_object):
    class Document(BaseModel):
        text: str
        title: str

    document = {"text": "synthetic body", "title": "synthetic title"}
    supplied = Document(**document) if sdk_object else document
    attrs = dict(
        _RerankRequestAttributesExtractor().get_extra_attributes_from_request(
            {"documents": (supplied,)}
        )
    )
    assert json.loads(attrs[INPUT_DOC]) == document


def test_v1_tool_schema_and_iterator_safety():
    import cohere

    schema = {
        "name": "lookup",
        "description": PRIVATE,
        "parameter_definitions": {
            "city": {"type": "str", "description": "city name", "required": True}
        },
    }
    for tool in (schema, cohere.Tool(**schema)):
        attrs = dict(
            _ChatRequestAttributesExtractor().get_extra_attributes_from_request(
                {"tools": [tool]}
            )
        )
        captured = json.loads(attrs["gen_ai.tool.definitions.0.tool.json_schema"])
        assert captured["description"] == PRIVATE
        assert captured["parameter_definitions"]["city"]["required"] is True

    def tools():
        raise AssertionError("instrumentation consumed the caller's iterator")
        yield schema

    assert not dict(
        _ChatRequestAttributesExtractor().get_extra_attributes_from_request(
            {"tools": tools()}
        )
    )


@pytest.mark.parametrize("cached", [None, 0, 6])
def test_cache_subset_does_not_change_actual_totals(cached):
    attrs = dict(
        _ChatResponseAttributesExtractor().get_attributes(
            {
                "usage": {
                    "cached_tokens": cached,
                    "tokens": {"input_tokens": 0, "output_tokens": 0},
                    "billed_units": {"input_tokens": 99, "output_tokens": 99},
                }
            }
        )
    )
    if cached is None:
        assert CACHE_KEY not in attrs
    else:
        assert attrs[CACHE_KEY] == cached
    assert attrs["gen_ai.usage.total_tokens"] == 0


@pytest.mark.parametrize("kind", ["text", "tool", "v1_tool", "empty"])
def test_ttft_ignores_metadata_and_empty_deltas(monkeypatch, kind):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(
        provider.get_tracer(__name__), config=TraceConfig(hide_outputs=True)
    )
    span = tracer.start_span("cohere.chat_stream")
    now = [10.0]
    monkeypatch.setattr("traceai_cohere._stream.perf_counter", lambda: now[0])
    metadata = {"type": "message-start", "delta": {"message": {"role": "assistant"}}}
    empty = {
        "type": "content-start",
        "index": 0,
        "delta": {"message": {"content": {"text": ""}}},
    }
    content = {
        "text": {
            "type": "content-delta",
            "delta": {"message": {"content": {"text": PRIVATE}}},
        },
        "tool": {
            "type": "tool-call-delta",
            "delta": {"message": {"tool_calls": {"function": {"arguments": PRIVATE}}}},
        },
        "v1_tool": {
            "event_type": "tool-calls-generation",
            "tool_calls": [{"name": "lookup", "parameters": {"query": PRIVATE}}],
        },
        "empty": {"type": "message-end", "delta": {"finish_reason": "COMPLETE"}},
    }[kind]
    stream = _Stream(
        iter([metadata, empty, content]),
        _WithSpan(span),
        {},
        config=TraceConfig(hide_outputs=True),
        started_at=10.0,
    )
    assert next(stream) is metadata
    now[0] = 11.0
    assert next(stream) is empty
    now[0] = 12.5
    assert next(stream) is content
    now[0] = 20.0
    assert list(stream) == []
    attrs = exporter.get_finished_spans()[0].attributes
    if kind == "empty":
        assert TTFT_KEY not in attrs
    else:
        assert attrs[TTFT_KEY] == 2500.0
    assert PRIVATE not in str(dict(attrs))
    provider.shutdown()
