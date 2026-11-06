"""Generated image content follows output-message privacy across response formats."""
import json

import litellm
import pytest
from fi_instrumentation import TraceConfig
from litellm.types.utils import ImageResponse
from openai.types.image import Image
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_litellm import LiteLLMInstrumentor


@pytest.mark.parametrize("flag", ["hide_output_text", "hide_output_messages", None])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("format", ["url", "b64_json"])
@pytest.mark.parametrize("object_response", [False, True])
@pytest.mark.asyncio
async def test_image_generation_output_privacy(flag, asynchronous, format, object_response, monkeypatch):
    marker = "synthetic-private-generated-image-data"
    item = {format: marker}
    response = ImageResponse(data=[Image(**item) if object_response else item])
    async def async_image_generation(*args, **kwargs):
        return response
    def image_generation(*args, **kwargs):
        return response
    monkeypatch.setattr(litellm, "image_generation", image_generation)
    monkeypatch.setattr(litellm, "aimage_generation", async_image_generation)
    provider, exporter = TracerProvider(), InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LiteLLMInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**({flag: True} if flag else {})))
    try:
        actual = (await litellm.aimage_generation(model="synthetic-model", prompt="public-prompt")
                  if asynchronous else litellm.image_generation(model="synthetic-model", prompt="public-prompt"))
        assert actual is response
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attributes = dict(spans[0].attributes)
    if flag == "hide_output_messages":
        assert marker not in json.dumps(attributes), attributes
    if flag is not None:
        assert marker not in attributes.get("output.value", ""), attributes
    else:
        assert marker in attributes.get("output.value", ""), attributes
