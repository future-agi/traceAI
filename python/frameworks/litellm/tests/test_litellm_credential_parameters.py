"""T3-LITELLM-001/001b: provider authentication is not request telemetry.

Provider functions are mocked; spans are exported only to memory. Run with the
local LiteLLM package on PYTHONPATH and LITELLM_LOCAL_MODEL_COST_MAP=True.
"""

import asyncio
import copy
import json
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.fi_types import SpanAttributes


_FAKE_CREDENTIAL = "FAKE_PROVIDER_CREDENTIAL_ONLY"


@pytest.fixture
def instrumentation(monkeypatch):
    # LiteLLM must not download its model cost map even during import.
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import traceai_litellm

    return traceai_litellm


@pytest.fixture
def memory_tracer(clean_env):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer(__name__), config=TraceConfig())
    try:
        yield tracer, exporter
    finally:
        provider.shutdown()


def _wrapper_harness(instrumentation, tracer, functions):
    harness = SimpleNamespace(_tracer=tracer, original_litellm_funcs=functions)
    if hasattr(instrumentation.LiteLLMInstrumentor, "_get_call_arguments"):
        harness._get_call_arguments = MethodType(
            instrumentation.LiteLLMInstrumentor._get_call_arguments, harness
        )
    return harness


def _completion_kwargs(with_messages=True):
    kwargs = {
        "model": "gpt-4o-mini",
        "temperature": 0.7,
        "max_tokens": 20,
        "stop": ["END"],
        "api_key": _FAKE_CREDENTIAL,
        "extra_headers": {
            "Authorization": "Bearer " + _FAKE_CREDENTIAL,
            "X-API-Key": _FAKE_CREDENTIAL,
        },
        "azure_ad_token": _FAKE_CREDENTIAL,
        "aws_secret_access_key": _FAKE_CREDENTIAL,
        "extra_body": {
            "provider_options": [
                {"api_key": _FAKE_CREDENTIAL},
                {"extra_headers": {"Authorization": _FAKE_CREDENTIAL}},
            ]
        },
        "metadata": {"provider": {"credentials": _FAKE_CREDENTIAL}},
        "unknown_provider_option": _FAKE_CREDENTIAL,
    }
    if with_messages:
        kwargs["messages"] = [{"role": "user", "content": "hello"}]
    return kwargs


def _assert_no_credentials(values):
    assert not any(_FAKE_CREDENTIAL in str(value) for value in values), (
        "Provider authentication was captured in a span attribute"
    )


@pytest.mark.parametrize(
    "method,stream",
    [
        pytest.param("completion", False, id="sync"),
        pytest.param("acompletion", False, id="async"),
        pytest.param("completion_with_retries", False, id="sync-retry"),
        pytest.param("acompletion_with_retries", False, id="async-retry"),
        pytest.param("completion", True, id="sync-stream"),
        pytest.param("acompletion", True, id="async-stream"),
    ],
)
@pytest.mark.parametrize("with_messages", [True, False], ids=["messages", "no-messages"])
def test_completion_provider_credentials_not_exported(
    instrumentation, memory_tracer, method, stream, with_messages
):
    tracer, exporter = memory_tracer
    kwargs = _completion_kwargs(with_messages)
    kwargs["stream"] = stream
    before = copy.deepcopy(kwargs)
    response = {"choices": [{"message": {"role": "assistant", "content": "hello"}}]}
    chunk = {"choices": [{"delta": {"content": "hello"}}]}

    async def async_chunks():
        yield chunk

    if method.startswith("a"):
        original = AsyncMock(return_value=async_chunks() if stream else response)
    else:
        original = Mock(return_value=iter([chunk]) if stream else response)
    harness = _wrapper_harness(instrumentation, tracer, {method: original})
    wrapper = getattr(instrumentation.LiteLLMInstrumentor, "_" + method + "_wrapper")

    if method.startswith("a"):
        async def invoke():
            result = await wrapper(harness, **kwargs)
            if stream:
                chunks = [item async for item in result]
                assert len(chunks) == 1 and chunks[0] is chunk
            else:
                assert result is response

        asyncio.run(invoke())
        original.assert_awaited_once_with(**before)
    else:
        result = wrapper(harness, **kwargs)
        if stream:
            chunks = list(result)
            assert len(chunks) == 1 and chunks[0] is chunk
        else:
            assert result is response
        original.assert_called_once_with(**before)

    # Filtering is a telemetry-only projection, never a change to provider input.
    assert kwargs == before
    received = original.call_args.kwargs
    assert received["extra_headers"] is kwargs["extra_headers"]
    assert received["extra_body"] is kwargs["extra_body"]
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attributes = spans[0].attributes
    _assert_no_credentials(attributes.values())
    assert json.loads(attributes[SpanAttributes.GEN_AI_REQUEST_PARAMETERS]) == {
        "temperature": 0.7,
        "max_tokens": 20,
        "stop": ["END"],
        "stream": stream,
    }
    assert attributes[SpanAttributes.GEN_AI_REQUEST_MODEL] == "gpt-4o-mini"
    if with_messages:
        assert json.loads(attributes[SpanAttributes.INPUT_VALUE]) == kwargs["messages"]


@pytest.mark.parametrize("with_messages", [True, False], ids=["messages", "no-messages"])
def test_every_completion_attribute_write_excludes_credentials(instrumentation, with_messages):
    # A final input.value overwrite must not conceal an earlier unsafe write.
    span = Mock()
    kwargs = _completion_kwargs(with_messages)
    before = copy.deepcopy(kwargs)
    instrumentation._instrument_func_type_completion(span, kwargs)
    values = [call.args[1] for call in span.set_attribute.call_args_list]
    _assert_no_credentials(values)
    assert kwargs == before


def test_nested_options_and_clients_are_not_serialized(
    instrumentation, memory_tracer
):
    class CredentialClient:
        def __init__(self):
            self.render_count = 0

        def __str__(self):
            self.render_count += 1
            return _FAKE_CREDENTIAL

        __repr__ = __str__

    client = CredentialClient()
    kwargs = _completion_kwargs()
    kwargs.update({
        "client": client,
        "http_client": client,
        "extra_body": {"options": [{"client": client}]},
        # Even allowlisted names must not admit arbitrary nested data or objects.
        "temperature": {"api_key": _FAKE_CREDENTIAL},
        "max_tokens": client,
        "stop": [{"Authorization": _FAKE_CREDENTIAL}],
        "response_format": {
            "type": "json_object",
            "options": {"api_key": _FAKE_CREDENTIAL},
        },
        "seed": 7,
    })
    tracer, exporter = memory_tracer
    response = {"choices": []}
    original = Mock(return_value=response)
    harness = _wrapper_harness(instrumentation, tracer, {"completion": original})
    result = instrumentation.LiteLLMInstrumentor._completion_wrapper(harness, **kwargs)

    assert result is response
    original.assert_called_once_with(**kwargs)
    assert original.call_args.kwargs["client"] is client
    assert original.call_args.kwargs["extra_body"] is kwargs["extra_body"]
    assert client.render_count == 0
    attributes = exporter.get_finished_spans()[0].attributes
    _assert_no_credentials(attributes.values())
    assert json.loads(attributes[SpanAttributes.GEN_AI_REQUEST_PARAMETERS]) == {"seed": 7}


def test_safe_inference_parameters_remain_visible(instrumentation):
    span = Mock()
    parameters = {
        "temperature": 0.0,
        "top_p": 0.9,
        "top_k": 5,
        "n": 2,
        "max_tokens": 20,
        "max_completion_tokens": 30,
        "frequency_penalty": 0.0,
        "presence_penalty": 0.2,
        "seed": 0,
        "logprobs": False,
        "top_logprobs": 3,
        "stream": False,
        "parallel_tool_calls": True,
        "reasoning_effort": "low",
        "stop": ["END"],
    }
    instrumentation._instrument_func_type_completion(
        span, {"model": "gpt-4o-mini", **parameters}
    )
    attributes = dict(call.args for call in span.set_attribute.call_args_list)
    assert json.loads(attributes[SpanAttributes.GEN_AI_REQUEST_PARAMETERS]) == parameters


def _input_provider_options():
    return {
        "api_key": _FAKE_CREDENTIAL,
        "headers": {"Authorization": "Bearer " + _FAKE_CREDENTIAL},
        "extra_headers": {"X-API-Key": _FAKE_CREDENTIAL},
        "metadata": {"provider": {"credentials": _FAKE_CREDENTIAL}},
    }


@pytest.mark.parametrize("method", ["image_generation", "aimage_generation"])
@pytest.mark.parametrize("positional", [True, False], ids=["positional", "keyword"])
@pytest.mark.parametrize("prompt", ["draw a blue circle", ""], ids=["text", "empty"])
def test_image_provider_credentials_not_exported(
    instrumentation, memory_tracer, method, positional, prompt
):
    tracer, exporter = memory_tracer
    kwargs = {"model": "dall-e-2", **_input_provider_options()}
    args = (prompt,) if positional else ()
    if not positional:
        kwargs["prompt"] = prompt
    before = copy.deepcopy(kwargs)
    response = {"data": [{"url": "https://example.invalid/image.png"}]}

    # Prompt is positional-or-keyword in LiteLLM; only the provider body is fake.
    def generate(prompt, model=None, **options):
        return response

    original = (
        AsyncMock(side_effect=generate)
        if method.startswith("a")
        else Mock(side_effect=generate)
    )
    harness = _wrapper_harness(instrumentation, tracer, {method: original})
    wrapper = getattr(instrumentation.LiteLLMInstrumentor, "_" + method + "_wrapper")
    if method.startswith("a"):
        result = asyncio.run(wrapper(harness, *args, **kwargs))
        original.assert_awaited_once_with(*args, **before)
    else:
        result = wrapper(harness, *args, **kwargs)
        original.assert_called_once_with(*args, **before)

    assert result is response
    assert kwargs == before
    for name in ("headers", "extra_headers", "metadata"):
        assert original.call_args.kwargs[name] is kwargs[name]
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attributes = spans[0].attributes
    _assert_no_credentials(attributes.values())
    assert attributes[SpanAttributes.GEN_AI_REQUEST_MODEL] == "dall-e-2"
    if prompt:
        assert attributes[SpanAttributes.INPUT_VALUE] == prompt
    else:
        assert SpanAttributes.INPUT_VALUE not in attributes


@pytest.mark.parametrize("method", ["embedding", "aembedding"])
def test_embedding_provider_credentials_not_exported(
    instrumentation, memory_tracer, method
):
    # Control: these helpers already overwrite the raw kwargs in ended spans.
    tracer, exporter = memory_tracer
    kwargs = {
        "model": "text-embedding-3-small",
        "input": ["hello"],
        **_input_provider_options(),
    }
    before = copy.deepcopy(kwargs)
    response = {"data": [{"embedding": [0.1, 0.2]}]}
    original = (
        AsyncMock(return_value=response)
        if method.startswith("a")
        else Mock(return_value=response)
    )
    harness = _wrapper_harness(instrumentation, tracer, {method: original})
    wrapper = getattr(instrumentation.LiteLLMInstrumentor, "_" + method + "_wrapper")
    if method.startswith("a"):
        result = asyncio.run(wrapper(harness, **kwargs))
        original.assert_awaited_once_with(**before)
    else:
        result = wrapper(harness, **kwargs)
        original.assert_called_once_with(**before)

    assert result is response
    assert kwargs == before
    for name in ("input", "headers", "extra_headers", "metadata"):
        assert original.call_args.kwargs[name] is kwargs[name]
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attributes = spans[0].attributes
    _assert_no_credentials(attributes.values())
    assert attributes[SpanAttributes.INPUT_VALUE] == str(kwargs["input"])


@pytest.mark.parametrize(
    "helper,content",
    [
        ("_instrument_func_type_image_generation", {"prompt": "draw a blue circle"}),
        ("_instrument_func_type_image_generation", {}),
        ("_instrument_func_type_embedding", {"input": ["hello"]}),
    ],
    ids=["image-keyword", "image-missing-prompt", "embedding"],
)
def test_every_input_attribute_write_excludes_credentials(
    instrumentation, helper, content
):
    # Neither a later overwrite nor a truthy prompt may hide a raw-kwargs write.
    span = Mock()
    kwargs = {"model": "example-model", **content, **_input_provider_options()}
    before = copy.deepcopy(kwargs)
    getattr(instrumentation, helper)(span, kwargs)
    _assert_no_credentials(call.args[1] for call in span.set_attribute.call_args_list)
    assert kwargs == before


@pytest.mark.parametrize(
    "helper,content",
    [
        ("_instrument_func_type_image_generation", {"prompt": "draw a blue circle"}),
        ("_instrument_func_type_embedding", {"input": ["hello"]}),
    ],
)
def test_input_helpers_do_not_render_provider_clients(instrumentation, helper, content):
    class CredentialClient:
        def __str__(self):
            raise AssertionError("Provider clients must not be rendered for telemetry")

        __repr__ = __str__

    client = CredentialClient()
    options = {"provider": {"client": client, "api_key": _FAKE_CREDENTIAL}}
    kwargs = {
        "model": "example-model",
        **content,
        "client": client,
        "extra_body": options,
        "metadata": options,
    }
    span = Mock()
    getattr(instrumentation, helper)(span, kwargs)
    _assert_no_credentials(call.args[1] for call in span.set_attribute.call_args_list)
    assert kwargs["client"] is client
    assert kwargs["extra_body"] is options
    assert kwargs["metadata"] is options
