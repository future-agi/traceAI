"""T3-LITELLM-003: successful image calls must survive usage finalization."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from opentelemetry import context as context_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode


IMAGE_URL = "https://example.invalid/generated.png"


@pytest.fixture
def litellm_module(monkeypatch):
    # LiteLLM must not fetch its model cost map during import.
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import traceai_litellm

    return traceai_litellm


@pytest.fixture
def image_harness(monkeypatch, litellm_module):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = litellm_module.LiteLLMInstrumentor()
    # Exercise the real wrappers without mutating LiteLLM's global functions or
    # the singleton instrumentor's shared original-function dictionary.
    monkeypatch.setattr(instrumentor, "original_litellm_funcs", {})
    monkeypatch.setattr(
        instrumentor, "_tracer", provider.get_tracer(__name__), raising=False
    )
    context = context_api.attach(
        context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, False)
    )
    try:
        yield instrumentor, exporter, litellm_module
    finally:
        context_api.detach(context)
        provider.shutdown()


def _call_image(image_harness, asynchronous, response):
    instrumentor, _, _ = image_harness
    kwargs = {"model": "dall-e-3", "prompt": "a garden"}
    if asynchronous:
        original = AsyncMock(return_value=response)
        instrumentor.original_litellm_funcs["aimage_generation"] = original
        result = asyncio.run(instrumentor._aimage_generation_wrapper(**kwargs))
        original.assert_awaited_once_with(**kwargs)
    else:
        original = Mock(return_value=response)
        instrumentor.original_litellm_funcs["image_generation"] = original
        result = instrumentor._image_generation_wrapper(**kwargs)
        original.assert_called_once_with(**kwargs)
    assert result is response


def _image_span(image_harness):
    _, exporter, module = image_harness
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.OK
    assert not span.events
    assert span.attributes[module.SpanAttributes.OUTPUT_VALUE] == IMAGE_URL
    return span


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_image_response_default_usage(image_harness, asynchronous):
    """Real LiteLLM 1.81.3 supplies image-style usage even when it is omitted."""
    _, _, module = image_harness
    response = module.ImageResponse(data=[{"url": IMAGE_URL}])

    _call_image(image_harness, asynchronous, response)

    _image_span(image_harness)


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_image_response_native_usage(image_harness, asynchronous):
    """Keep the native LiteLLM image usage representation, not just a dict fake."""
    _, _, module = image_harness
    response = module.ImageResponse(
        data=[{"url": IMAGE_URL}],
        usage={
            "input_tokens": 3,
            "input_tokens_details": {"text_tokens": 3, "image_tokens": 0},
            "output_tokens": 5,
            "total_tokens": 8,
        },
    )

    _call_image(image_harness, asynchronous, response)

    span = _image_span(image_harness)
    attributes = module.SpanAttributes
    assert span.attributes[attributes.GEN_AI_USAGE_INPUT_TOKENS] == 3
    assert span.attributes[attributes.GEN_AI_USAGE_OUTPUT_TOKENS] == 5
    assert span.attributes[attributes.GEN_AI_USAGE_TOTAL_TOKENS] == 8


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    "usage, expected",
    [
        pytest.param(
            {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
            (3, 5, 8),
            id="image",
        ),
        pytest.param(
            {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
            (3, 5, 8),
            id="legacy",
        ),
        pytest.param(
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "prompt_tokens": 99,
                "completion_tokens": 99,
            },
            (0, 0, 0),
            id="image-zero-preferred",
        ),
        pytest.param(
            {
                "input_tokens": None,
                "output_tokens": None,
                "prompt_tokens": 3,
                "completion_tokens": 5,
                "total_tokens": 8,
            },
            (3, 5, 8),
            id="legacy-fallback",
        ),
        pytest.param({"input_tokens": 3}, (3, None, None), id="input-only"),
        pytest.param({"output_tokens": 5}, (None, 5, None), id="output-only"),
        pytest.param({"total_tokens": 8}, (None, None, 8), id="total-only"),
        pytest.param(None, (None, None, None), id="none"),
        pytest.param({}, (None, None, None), id="empty"),
        pytest.param({"provider_units": 7}, (None, None, None), id="unknown-fields"),
        pytest.param("provider-specific", (None, None, None), id="unknown-shape"),
    ],
)
def test_image_response_usage_shapes(image_harness, asynchronous, usage, expected):
    _, _, module = image_harness
    response = module.ImageResponse(data=[{"url": IMAGE_URL}])
    # Preserve the exact provider usage shape, rather than letting a particular
    # LiteLLM version normalize partial, missing, or legacy fields for us.
    object.__setattr__(response, "usage", usage)

    _call_image(image_harness, asynchronous, response)

    span = _image_span(image_harness)
    attributes = module.SpanAttributes
    for name, value in zip(
        (
            attributes.GEN_AI_USAGE_INPUT_TOKENS,
            attributes.GEN_AI_USAGE_OUTPUT_TOKENS,
            attributes.GEN_AI_USAGE_TOTAL_TOKENS,
        ),
        expected,
    ):
        if value is None:
            assert name not in span.attributes
        else:
            assert span.attributes[name] == value


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_image_response_without_usage(image_harness, asynchronous):
    _, _, module = image_harness
    response = module.ImageResponse(data=[{"url": IMAGE_URL}])
    if hasattr(response, "usage"):
        object.__delattr__(response, "usage")

    _call_image(image_harness, asynchronous, response)

    span = _image_span(image_harness)
    attributes = module.SpanAttributes
    assert attributes.GEN_AI_USAGE_INPUT_TOKENS not in span.attributes
    assert attributes.GEN_AI_USAGE_OUTPUT_TOKENS not in span.attributes
    assert attributes.GEN_AI_USAGE_TOTAL_TOKENS not in span.attributes


@pytest.mark.parametrize("response_kind", ["chat", "embedding"])
def test_non_image_usage_is_preserved(litellm_module, response_kind):
    module = litellm_module
    usage = {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}
    if response_kind == "chat":
        response = module.ModelResponse(choices=[], usage=usage)
    else:
        response = module.EmbeddingResponse(data=[], usage=usage)
    span = Mock()

    module._finalize_span(span, response)

    attributes = module.SpanAttributes
    span.set_attribute.assert_any_call(attributes.GEN_AI_USAGE_INPUT_TOKENS, 3)
    span.set_attribute.assert_any_call(attributes.GEN_AI_USAGE_OUTPUT_TOKENS, 5)
    span.set_attribute.assert_any_call(attributes.GEN_AI_USAGE_TOTAL_TOKENS, 8)
