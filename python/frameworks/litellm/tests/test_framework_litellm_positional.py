"""T3-LITELLM-008: positional requests must retain their tracing attributes."""

import asyncio
import json
from functools import wraps

import pytest
from opentelemetry import context as context_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fi_instrumentation.fi_types import (
    EmbeddingAttributes,
    MessageAttributes,
    SpanAttributes,
)


MARKER = "POSITIONAL_QUESTION_MARKER"
API_NAMES = (
    "completion",
    "acompletion",
    "completion_with_retries",
    "acompletion_with_retries",
    "embedding",
    "aembedding",
    "image_generation",
    "aimage_generation",
)


@pytest.fixture
def positional_instrumentor(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    from traceai_litellm import LiteLLMInstrumentor

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LiteLLMInstrumentor()
    # Exercise the actual wrappers without changing global LiteLLM functions or
    # the instrumentor's shared registry. No provider or collector is contacted.
    monkeypatch.setattr(instrumentor, "original_litellm_funcs", {})
    monkeypatch.setattr(
        instrumentor, "_tracer", provider.get_tracer(__name__), raising=False
    )
    context_token = context_api.attach(
        context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, False)
    )
    try:
        yield instrumentor, exporter
    finally:
        context_api.detach(context_token)
        provider.shutdown()


def _install_fake(instrumentor, api_name, response):
    def completion(model, messages, stream=False, callback=None, **kwargs):
        return response

    def embedding(model, input, **kwargs):
        return response

    def image_generation(prompt, model=None, **kwargs):
        return response

    if "completion" in api_name:
        base_name, signature_source = "completion", completion
    elif "embedding" in api_name:
        base_name, signature_source = "embedding", embedding
    else:
        base_name, signature_source = "image_generation", image_generation
    instrumentor.original_litellm_funcs[base_name] = signature_source
    calls = []

    def sync_original(*args, **kwargs):
        calls.append((args, kwargs))
        return signature_source(*args, **kwargs)

    async def async_original(*args, **kwargs):
        calls.append((args, kwargs))
        return signature_source(*args, **kwargs)

    original = async_original if api_name.startswith("a") else sync_original
    # Forwarding APIs can expose only (*args, **kwargs). Other decorators retain
    # the underlying API signature via __wrapped__.
    if api_name not in (
        "completion_with_retries",
        "acompletion_with_retries",
        "aembedding",
        "aimage_generation",
    ):
        original = wraps(signature_source)(original)
    instrumentor.original_litellm_funcs[api_name] = original
    return calls


def _invoke(instrumentor, api_name, args, kwargs):
    result = getattr(instrumentor, f"_{api_name}_wrapper")(*args, **kwargs)
    return asyncio.run(result) if api_name.startswith("a") else result


@pytest.mark.parametrize("api_name", API_NAMES)
@pytest.mark.parametrize("call_style", ("positional", "mixed", "keyword"))
def test_request_attributes_and_passthrough(
    positional_instrumentor, api_name, call_style
):
    instrumentor, exporter = positional_instrumentor
    response = {"result": "hello"}
    calls = _install_fake(instrumentor, api_name, response)
    if "completion" in api_name:
        request = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": MARKER}]}
    elif "embedding" in api_name:
        request = {"model": "embedding-test-model", "input": [MARKER]}
    else:
        # Image APIs take prompt before model, unlike completion/embedding.
        request = {"prompt": MARKER, "model": "image-test-model"}
    items = list(request.items())
    positional_count = {"positional": 2, "mixed": 1, "keyword": 0}[call_style]
    args = tuple(value for _, value in items[:positional_count])
    kwargs = dict(items[positional_count:])
    kwargs["request_marker"] = "fake-provider-option"

    assert _invoke(instrumentor, api_name, args, kwargs) is response
    assert calls == [(args, kwargs)]
    assert all(actual is expected for actual, expected in zip(calls[0][0], args))
    assert all(calls[0][1][name] is value for name, value in kwargs.items())
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attributes = spans[0].attributes
    model_attribute = (
        SpanAttributes.EMBEDDING_MODEL_NAME
        if "embedding" in api_name
        else SpanAttributes.GEN_AI_REQUEST_MODEL
    )
    assert attributes[model_attribute] == request["model"]
    assert MARKER in attributes[SpanAttributes.INPUT_VALUE]
    if "completion" in api_name:
        message_prefix = f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.0"
        assert attributes[f"{message_prefix}.{MessageAttributes.MESSAGE_ROLE}"] == "user"
        assert attributes[f"{message_prefix}.{MessageAttributes.MESSAGE_CONTENT}"] == MARKER
        # Unknown provider options are forwarded, never exported as telemetry.
        assert json.loads(attributes[SpanAttributes.GEN_AI_REQUEST_PARAMETERS]) == {}
    elif "embedding" in api_name:
        text_attribute = (
            f"{SpanAttributes.EMBEDDING_EMBEDDINGS}.0."
            f"{EmbeddingAttributes.EMBEDDING_TEXT}"
        )
        assert attributes[text_attribute] == str(request["input"])


@pytest.mark.parametrize("api_name", ("completion", "acompletion"))
@pytest.mark.parametrize("stream_style", ("positional", "keyword"))
def test_streaming_positional_request(positional_instrumentor, api_name, stream_style):
    instrumentor, exporter = positional_instrumentor
    chunk = {"choices": [{"delta": {"content": "hello"}}]}

    async def async_chunks():
        yield chunk

    response = async_chunks() if api_name.startswith("a") else iter([chunk])
    calls = _install_fake(instrumentor, api_name, response)
    args = ("gpt-4o-mini", [{"role": "user", "content": MARKER}])
    kwargs = {}
    if stream_style == "positional":
        args += (True,)
    else:
        kwargs["stream"] = True

    async def call_and_consume():
        result = await instrumentor._acompletion_wrapper(*args, **kwargs)
        assert not exporter.get_finished_spans()
        return [item async for item in result]

    if api_name.startswith("a"):
        chunks = asyncio.run(call_and_consume())
    else:
        result = instrumentor._completion_wrapper(*args, **kwargs)
        assert not exporter.get_finished_spans()
        chunks = list(result)
    assert calls == [(args, kwargs)]
    assert len(chunks) == 1
    assert chunks[0] is chunk
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes[SpanAttributes.GEN_AI_REQUEST_MODEL] == "gpt-4o-mini"
    assert json.loads(spans[0].attributes[SpanAttributes.INPUT_VALUE]) == args[1]


def test_non_json_positional_option_preserves_call(positional_instrumentor):
    instrumentor, exporter = positional_instrumentor
    response = {"result": "hello"}
    calls = _install_fake(instrumentor, "completion", response)

    def callback():
        pass

    args = ("gpt-4o-mini", [{"role": "user", "content": MARKER}], False, callback)
    assert _invoke(instrumentor, "completion", args, {}) is response
    assert calls == [(args, {})]
    attributes = exporter.get_finished_spans()[0].attributes
    assert attributes[SpanAttributes.GEN_AI_REQUEST_MODEL] == "gpt-4o-mini"
    assert json.loads(attributes[SpanAttributes.INPUT_VALUE]) == args[1]
    assert json.loads(attributes[SpanAttributes.GEN_AI_REQUEST_PARAMETERS]) == {
        "stream": False,
    }


def test_unavailable_signature_preserves_call(positional_instrumentor):
    instrumentor, exporter = positional_instrumentor
    response = {"result": "hello"}
    calls = _install_fake(instrumentor, "completion", response)
    # inspect.signature rejects this, but the original callable remains valid.
    instrumentor.original_litellm_funcs["completion"].__signature__ = object()
    args = ("gpt-4o-mini", [{"role": "user", "content": MARKER}])
    kwargs = {"request_marker": "fake-provider-option"}

    assert _invoke(instrumentor, "completion", args, kwargs) is response
    assert calls == [(args, kwargs)]
    assert len(exporter.get_finished_spans()) == 1


def test_binding_error_is_left_to_original_call(positional_instrumentor):
    instrumentor, _ = positional_instrumentor
    calls = _install_fake(instrumentor, "completion", {"result": "hello"})
    args = ("gpt-4o-mini", [{"role": "user", "content": MARKER}])
    kwargs = {"model": "duplicate-model"}

    with pytest.raises(TypeError, match="multiple values for argument 'model'"):
        _invoke(instrumentor, "completion", args, kwargs)
    assert calls == [(args, kwargs)]
