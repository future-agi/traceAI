"""Offline real-SDK coverage for embedding modality counts and private images."""
import json

import cohere
import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_cohere import CohereInstrumentor


IMAGE = "data:image/png;base64,c3ludGhldGljLWltYWdl"


@pytest.mark.parametrize("mode", ["images", "mixed"])
def test_embed_multimodal_sdk_counts_without_image_data(mode):
    if not hasattr(cohere, "ClientV2"):
        pytest.skip("Cohere 5.0.0 predates v2 images")
    if mode == "mixed":
        import inspect
        if "inputs" not in inspect.signature(cohere.ClientV2.embed).parameters:
            pytest.skip("This SDK predates mixed embedding inputs")
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = CohereInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "synthetic", "response_type": "embeddings_by_type",
            "embeddings": {"float": [[0.1, 0.2]]},
        })

    try:
        kwargs = {"images": [IMAGE]} if mode == "images" else {
            "inputs": [{"content": [
                {"type": "text", "text": "synthetic caption"},
                {"type": "image_url", "image_url": {"url": IMAGE}},
            ]}],
        }
        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            client = cohere.ClientV2(api_key="synthetic-only", httpx_client=http)
            response = client.embed(model="embed-v4.0", input_type="search_document", embedding_types=["float"], **kwargs)
        assert response.embeddings.float == [[0.1, 0.2]]
        assert len(sent) == 1 and ("images" if mode == "images" else "inputs") in sent[0]
        attrs = exporter.get_finished_spans()[0].attributes
        assert attrs["cohere.images_count"] == 1
        assert attrs["cohere.inputs_count"] == 1
        if mode == "images":
            assert "cohere.texts_count" not in attrs
            assert attrs.get("input.value") != "[]"
        else:
            assert attrs["cohere.texts_count"] == 1
        assert IMAGE not in json.dumps(dict(attrs))
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
