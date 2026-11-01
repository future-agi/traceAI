"""Real sync/async SDKs and loopback HTTP, with no provider credentials."""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from pathlib import Path

import pytest
import voyageai
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from PIL import Image

from _support import (
    DIMENSION,
    VECTOR_MARKER,
    VOYAGE_KEY,
    FakeVoyage,
    async_client,
    attrs,
    client,
    instrumented,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
from harness import Receiver, _flatten_attributes  # noqa: E402

METHODS = ["multimodal_embed", "contextualized_embed"]
INPUTS = [["PRIVATE-CHUNK-A", "PRIVATE-CHUNK-B"], ["PRIVATE-CHUNK-C"]]


def invoke(fake, method, asynchronous, inputs=None, model="embedding-test", **kwargs):
    inputs = INPUTS if inputs is None else inputs
    sdk = async_client(fake) if asynchronous else client(fake)
    result = getattr(sdk, method)(inputs, model, **kwargs)
    return asyncio.run(result) if asynchronous else result


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("asynchronous", [False, True])
def test_embedding_families_emit_one_span_and_preserve_results(method, asynchronous):
    with FakeVoyage() as fake, instrumented() as traced:
        with traced.provider.get_tracer("parent").start_as_current_span("parent") as parent:
            result = invoke(fake, method, asynchronous, input_type="document")
    span, parent_span = traced.spans()
    assert span.parent.span_id == parent.get_span_context().span_id == parent_span.context.span_id
    assert span.name == "voyage." + method
    assert span.status.status_code.name == "OK"
    values = attrs(span)
    assert values["gen_ai.span.kind"] == "EMBEDDING"
    assert values["gen_ai.operation.name"] == "embeddings"
    assert values["gen_ai.provider.name"] == "voyage"
    assert values["gen_ai.request.model"] == values["embedding.model_name"] == "embedding-test"
    assert values["server.address"] == "127.0.0.1"
    assert values["voyage.input_type"] == "document"
    assert values["voyage.embedding.dimension"] == DIMENSION
    assert values["gen_ai.embeddings.dimension.count"] == DIMENSION
    assert (
        values["gen_ai.usage.input_tokens"]
        == values["gen_ai.usage.total_tokens"]
        == result.total_tokens
    )
    assert "gen_ai.usage.output_tokens" not in values
    if method == "contextualized_embed":
        assert [len(item.embeddings) for item in result.results] == [2, 1]
        assert values["voyage.contextualized.document_count"] == 2
        assert values["voyage.embedding.count"] == 3
        assert result.total_tokens == 21
    else:
        assert len(result.embeddings) == 2
        assert values["voyage.embedding.count"] == 2
        assert values["voyage.usage.text_tokens"] == 3
        assert values["voyage.usage.image_pixels"] == 128
        assert values["voyage.usage.video_pixels"] == 256
        assert result.total_tokens == 11
    assert fake.paths() == ["/v1/" + method.replace("_embed", "embeddings")]
    wire = traced.wire()
    for marker in (VOYAGE_KEY, VECTOR_MARKER, "PRIVATE-CHUNK"):
        assert marker not in wire
    assert "input.value" not in values
    assert "output.value" not in values


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("dtype", ["float", "binary", "ubinary"])
def test_output_parameters_by_position_and_packed_dimensions(method, asynchronous, dtype):
    with FakeVoyage() as fake, instrumented() as traced:
        sdk = async_client(fake) if asynchronous else client(fake)
        args = [INPUTS, "embedding-test", "query"]
        if method == "multimodal_embed":
            args.append(True)  # truncation: contextualized_embed has no such parameter
        result = getattr(sdk, method)(*args, dtype, 32)
        if asynchronous:
            result = asyncio.run(result)
    values = attrs(traced.one())
    assert values["voyage.output_dtype"] == dtype
    assert values["voyage.output_dimension"] == 32
    assert values["voyage.embedding.dimension"] == 32
    vectors = result.embeddings if method == "multimodal_embed" else result.results[0].embeddings
    assert len(vectors[0]) == (4 if dtype in ("binary", "ubinary") else 32)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_contextualized_chunk_callback_runs_only_in_sdk(asynchronous):
    calls = []

    def chunk(text):
        calls.append(text)
        return text.split(" ")

    with FakeVoyage() as fake, instrumented(capture_content=True) as traced:
        result = invoke(
            fake,
            "contextualized_embed",
            asynchronous,
            inputs=[["one two"], ["three four five"]],
            chunk_fn=chunk,
        )
    assert calls == ["one two", "three four five"]
    assert [len(item.embeddings) for item in result.results] == [2, 3]
    values = attrs(traced.one())
    assert values["voyage.contextualized.document_count"] == 2
    assert values["voyage.embedding.count"] == 5
    assert json.loads(values["input.value"]) == [["one two"], ["three four five"]]
    assert "output.value" not in values


@pytest.mark.parametrize("inputs,count", [([[], []], 0), ([[], ["last chunk"]], 1)])
def test_empty_contextualized_batches_do_not_invent_dimensions(inputs, count):
    with FakeVoyage() as fake, instrumented(capture_content=True) as traced:
        result = invoke(fake, "contextualized_embed", False, inputs=inputs)
    values = attrs(traced.one())
    assert len(result.results) == 2
    assert values["voyage.contextualized.document_count"] == 2
    assert values["voyage.embedding.count"] == count
    assert values["gen_ai.usage.total_tokens"] == count * 7
    if count:
        assert values["voyage.embedding.dimension"] == DIMENSION
    else:
        assert "voyage.embedding.dimension" not in values
        assert "gen_ai.embeddings.dimension.count" not in values


@pytest.mark.parametrize("asynchronous", [False, True])
def test_auto_chunking_counts_returned_vectors_not_input_documents(asynchronous):
    if (
        "enable_auto_chunking"
        not in inspect.signature(voyageai.Client.contextualized_embed).parameters
    ):
        pytest.skip("Server-side auto-chunking is not in voyageai 0.3.7")
    with FakeVoyage() as fake, instrumented() as traced:
        result = invoke(
            fake,
            "contextualized_embed",
            asynchronous,
            inputs=["first document", "second document"],
            input_type="document",
            enable_auto_chunking=True,
        )
    assert len(result.results) == 2
    values = attrs(traced.one())
    assert values["voyage.contextualized.document_count"] == 2
    assert values["voyage.embedding.count"] == 4
    assert "SERVER-CHUNK-PRIVATE" not in traced.wire()


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize(
    "options,captured",
    [
        ({}, False),
        ({"capture_content": True}, True),
        ({"capture_content": True, "config": TraceConfig(hide_inputs=True)}, False),
        ({"capture_content": True, "config": TraceConfig(hide_input_text=True)}, False),
    ],
)
def test_nested_text_capture_is_opt_in_and_honors_privacy(method, options, captured):
    with FakeVoyage() as fake, instrumented(**options) as traced:
        invoke(fake, method, False)
    values = attrs(traced.one())
    if captured:
        assert json.loads(values["input.value"]) == INPUTS
    else:
        assert "input.value" not in values
        assert "PRIVATE-CHUNK" not in traced.wire()
    assert VECTOR_MARKER not in traced.wire()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("shape", ["dict", "pil"])
def test_multimodal_media_never_enters_otlp_even_with_capture(asynchronous, shape):
    if shape == "dict":
        inputs = [
            {
                "content": [
                    {"type": "text", "text": "TEXT-ON-WIRE"},
                    {
                        "type": "image_url",
                        "image_url": "https://example.invalid/PRIVATE-IMAGE?token=PRIVATE-TOKEN",
                    },
                    {"type": "video_base64", "video_base64": "data:video/mp4;base64,PRIVATE-VIDEO"},
                ]
            }
        ]
    else:
        inputs = [["TEXT-ON-WIRE", Image.new("RGB", (2, 2))]]
    with FakeVoyage() as fake, Receiver() as receiver, instrumented(capture_content=True) as traced:
        exporter = OTLPSpanExporter(endpoint=receiver.origin + "/v1/traces")
        traced.provider.add_span_processor(SimpleSpanProcessor(exporter))
        invoke(fake, "multimodal_embed", asynchronous, inputs=inputs)
        assert traced.provider.force_flush()
        spans = receiver.spans()
        traced.provider.shutdown()
    assert len(spans) == 1
    wire = json.dumps(spans) + json.dumps(receiver.requests())
    assert "TEXT-ON-WIRE" in wire
    assert "[media omitted]" in wire
    for marker in ("PRIVATE-", "data:", "PIL.", VECTOR_MARKER, VOYAGE_KEY):
        assert marker not in wire
    values = _flatten_attributes(spans[0]["attributes"])
    assert values["gen_ai.span.kind"] == "EMBEDDING"
    assert int(values["gen_ai.usage.input_tokens"]) == 11


@pytest.mark.parametrize("method", METHODS)
def test_nested_capture_has_global_item_and_text_limits(method):
    inputs = [["é" * 3000] * 40 for _ in range(4)]
    with FakeVoyage() as fake, instrumented(capture_content=True) as traced:
        invoke(fake, method, False, inputs=inputs)
    values = attrs(traced.one())
    captured = json.loads(values["input.value"])
    assert [len(group) for group in captured] == [40, 24]
    assert all(text == "é" * 1024 for group in captured for text in group)
    assert values["voyage.embedding.count"] == (160 if method == "contextualized_embed" else 4)


@pytest.mark.parametrize("method", METHODS)
def test_nested_capture_redacts_api_key_and_pii(method):
    with (
        FakeVoyage() as fake,
        instrumented(capture_content=True, config=TraceConfig(pii_redaction=True)) as traced,
    ):
        invoke(fake, method, False, inputs=[[VOYAGE_KEY, "jane.doe@example.com"]])
    wire = traced.wire()
    assert VOYAGE_KEY not in wire
    assert "jane.doe@example.com" not in wire
    assert "[redacted]" in wire
    assert "<EMAIL_ADDRESS>" in wire


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("asynchronous", [False, True])
def test_errors_keep_exception_type_without_echoing_nested_inputs(method, asynchronous):
    with FakeVoyage() as fake, instrumented() as traced:
        with pytest.raises(voyageai.error.InvalidRequestError) as caught:
            invoke(fake, method, asynchronous, model="echo-key")
    assert VOYAGE_KEY in str(caught.value)  # application error is unchanged
    span = traced.one()
    assert span.status.status_code.name == "ERROR"
    assert len(span.events) == 1
    assert span.events[0].attributes["exception.type"].endswith("InvalidRequestError")
    assert VOYAGE_KEY not in traced.wire()
    assert "gen_ai.usage.total_tokens" not in attrs(span)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_multimodal_validation_error_does_not_export_media(asynchronous):
    # Pydantic validation errors can echo complete binary/URL input dictionaries.
    inputs = [{"content": [{"type": "unknown", "image_base64": "PRIVATE-MEDIA-DATA"}]}]
    with FakeVoyage() as fake, Receiver() as receiver, instrumented(capture_content=True) as traced:
        traced.provider.add_span_processor(
            SimpleSpanProcessor(OTLPSpanExporter(endpoint=receiver.origin + "/v1/traces"))
        )
        with pytest.raises(voyageai.error.InvalidRequestError):
            invoke(fake, "multimodal_embed", asynchronous, inputs=inputs)
        assert traced.provider.force_flush()
        spans = receiver.spans()
        traced.provider.shutdown()
    assert fake.paths() == []
    assert traced.one().status.status_code.name == "ERROR"
    assert len(spans) == 1
    assert "PRIVATE-MEDIA-DATA" not in json.dumps(spans)
    assert "PRIVATE-MEDIA-DATA" not in traced.wire()


@pytest.mark.parametrize("method", METHODS)
def test_hide_inputs_otlp_keeps_shape_and_usage(method, monkeypatch):
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    with FakeVoyage() as fake, Receiver() as receiver, instrumented(capture_content=True) as traced:
        traced.provider.add_span_processor(
            SimpleSpanProcessor(OTLPSpanExporter(endpoint=receiver.origin + "/v1/traces"))
        )
        invoke(fake, method, False)
        assert traced.provider.force_flush()
        spans = receiver.spans()
        traced.provider.shutdown()
    assert len(spans) == 1
    values = _flatten_attributes(spans[0]["attributes"])
    assert values["embedding.model_name"] == "embedding-test"
    assert int(values["voyage.embedding.dimension"]) == DIMENSION
    assert int(values["gen_ai.usage.total_tokens"]) == (
        21 if method == "contextualized_embed" else 11
    )
    wire = json.dumps(spans)
    for marker in ("PRIVATE-CHUNK", VECTOR_MARKER, VOYAGE_KEY):
        assert marker not in wire
