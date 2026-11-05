"""Release regressions using real Haystack components and offline SDK transports."""

import asyncio
import json

import httpx
import openai
import pytest
from haystack import Document, Pipeline, component, tracing
from haystack.components.embedders import OpenAIDocumentEmbedder, OpenAITextEmbedder
from haystack.components.generators.chat import OpenAIChatGenerator
from haystack.dataclasses import ChatMessage
from haystack.utils import Secret
from opentelemetry import trace

from test_native_tracing import SECRET, execute, installed, pipeline_for
from traceai_haystack._bindings import pipeline_context, pipeline_is_async
from traceai_haystack._tracer import _active_span


@pytest.fixture
def offline_openai(monkeypatch):
    """Run the real OpenAI SDK without making any provider network requests."""
    state = {"requests": [], "status_code": 200}

    def respond(request):
        state["requests"].append(request)
        if state["status_code"] != 200:
            return httpx.Response(
                state["status_code"],
                json={"error": {"message": SECRET, "type": "invalid_request_error"}},
                request=request,
            )
        body = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            inputs = body["input"]
            count = len(inputs) if isinstance(inputs, list) else 1
            result = {
                "object": "list",
                "model": "text-embedding-3-small",
                "data": [
                    {"object": "embedding", "index": i, "embedding": [0.25, 0.75]}
                    for i in range(count)
                ],
                "usage": {"prompt_tokens": 11, "total_tokens": 11},
            }
        else:
            result = {
                "id": "chatcmpl-offline",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": SECRET},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                },
            }
        return httpx.Response(200, json=result, request=request)

    original_sync = openai.OpenAI.__init__
    original_async = openai.AsyncOpenAI.__init__

    def init_sync(self, *args, **kwargs):
        kwargs["http_client"] = httpx.Client(transport=httpx.MockTransport(respond))
        original_sync(self, *args, **kwargs)

    def init_async(self, *args, **kwargs):
        kwargs["http_client"] = httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        )
        original_async(self, *args, **kwargs)

    monkeypatch.setattr(openai.OpenAI, "__init__", init_sync)
    monkeypatch.setattr(openai.AsyncOpenAI, "__init__", init_async)
    return state


@pytest.mark.parametrize("mode", ["async", "generator"])
def test_cancelled_pipeline_records_outcome_and_preserves_privacy(mode):
    @component
    class Wait:
        def __init__(self, started):
            self.started = started

        @component.output_types(text=str)
        def run(self, text: str):
            return {"text": text}

        @component.output_types(text=str)
        async def run_async(self, text: str):
            self.started.set()
            await asyncio.Event().wait()
            return {"text": text}

    with installed(capture_content=False) as (_, _, exporter):

        async def cancel():
            started = asyncio.Event()
            pipeline = pipeline_for(mode)
            pipeline.add_component("wait", Wait(started))

            async def run():
                try:
                    if mode == "async":
                        await pipeline.run_async({"wait": {"text": SECRET}})
                    else:
                        async for _ in pipeline.run_async_generator(
                            {"wait": {"text": SECRET}}
                        ):
                            pass
                finally:
                    assert pipeline_context.get() is None
                    assert pipeline_is_async.get() is False
                    assert _active_span.get() is None

            task = asyncio.create_task(run())
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel(SECRET)
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(cancel())
        spans = exporter.get_finished_spans()
        assert {s.name for s in spans} == {"Wait.run", "Pipeline.run"}
        assert len(spans) == 2
        for span in spans:
            assert span.status.status_code == trace.StatusCode.ERROR
            assert span.attributes["error.type"] == "asyncio.CancelledError"
        assert SECRET not in str(
            [(dict(s.attributes), s.status, s.events) for s in spans]
        )


@pytest.mark.parametrize("capture_content", [False, True])
def test_native_cancellation_preserves_exception_identity(capture_content):
    cancellation = asyncio.CancelledError(SECRET)
    with installed(capture_content=capture_content) as (_, _, exporter):
        with pytest.raises(asyncio.CancelledError) as raised:
            with tracing.tracer.trace("haystack.component.run"):
                raise cancellation
        assert raised.value is cancellation
        assert _active_span.get() is None
        (span,) = exporter.get_finished_spans()
        assert span.attributes["error.type"] == "asyncio.CancelledError"
        assert span.status.status_code == trace.StatusCode.ERROR
        assert SECRET not in str((dict(span.attributes), span.status, span.events))


@pytest.mark.parametrize("mode", ["sync", "async", "generator"])
@pytest.mark.parametrize("documents", [False, True])
def test_real_embedding_usage_survives_content_opt_out(offline_openai, documents, mode):
    embedder_type = OpenAIDocumentEmbedder if documents else OpenAITextEmbedder
    embedder = embedder_type(
        api_key=Secret.from_token("synthetic-key"), model="text-embedding-3-small"
    )
    data = {"documents": [Document(content=SECRET)]} if documents else {"text": SECRET}
    with installed(capture_content=False) as (_, _, exporter):
        pipeline = pipeline_for(mode)
        pipeline.add_component("embedder", embedder)
        response = execute(pipeline, mode, {"embedder": data})["embedder"]
        assert response["meta"]["usage"] == {"prompt_tokens": 11, "total_tokens": 11}
        span = next(
            s for s in exporter.get_finished_spans() if s.name != "Pipeline.run"
        )
        assert span.attributes["gen_ai.span.kind"] == "EMBEDDING"
        assert span.attributes["gen_ai.provider.name"] == "openai"
        assert span.attributes["embedding.model_name"] == "text-embedding-3-small"
        assert span.attributes["gen_ai.usage.input_tokens"] == 11
        assert span.attributes["gen_ai.usage.total_tokens"] == 11
        assert "gen_ai.usage.output_tokens" not in span.attributes
        assert "embedding.embeddings.0.embedding.vector" not in span.attributes
        assert SECRET not in str(dict(span.attributes))
    assert len(offline_openai["requests"]) == 1


def test_real_openai_generator_provider_without_content(offline_openai):
    generator = OpenAIChatGenerator(
        api_key=Secret.from_token("synthetic-key"), model="gpt-4o-mini"
    )
    with installed(capture_content=False) as (_, _, exporter):
        pipeline = Pipeline()
        pipeline.add_component("generator", generator)
        pipeline.run({"generator": {"messages": [ChatMessage.from_user(SECRET)]}})
        # The effective SDK URL, not the offline transport, determines provider.
        assert generator.client.base_url.host == "api.openai.com"
        span = next(
            s for s in exporter.get_finished_spans() if s.name != "Pipeline.run"
        )
        assert span.attributes["gen_ai.request.model"] == "gpt-4o-mini"
        assert span.attributes["gen_ai.usage.total_tokens"] == 18
        assert span.attributes["gen_ai.provider.name"] == "openai"
        assert SECRET not in str(dict(span.attributes))
    assert len(offline_openai["requests"]) == 1


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("error", [False, True])
@pytest.mark.parametrize(
    "endpoint,provider",
    [
        (None, "openai"),
        ("https://resource.openai.azure.com/openai/v1", "azure"),
        ("https://api.openai.com.example.invalid/v1", "openai_compatible"),
        ("https://resource.openai.azure.com.example.invalid/v1", "openai_compatible"),
        ("https://api.openai.com@custom.example.invalid/v1", "openai_compatible"),
        ("https://API.OPENAI.COM./v1", "openai"),
        ("http://localhost:9999/v1", "openai_compatible"),
    ],
)
def test_effective_provider_on_success_and_error(
    offline_openai, mode, error, endpoint, provider
):
    generator = OpenAIChatGenerator(
        api_key=Secret.from_token("synthetic-key"),
        model="gpt-4o-mini",
        api_base_url=endpoint,
        max_retries=0,
    )
    offline_openai["status_code"] = 400 if error else 200
    with installed(capture_content=False) as (_, _, exporter):
        pipeline = pipeline_for(mode)
        pipeline.add_component("generator", generator)
        if error:
            with pytest.raises(Exception):
                execute(
                    pipeline,
                    mode,
                    {"generator": {"messages": [ChatMessage.from_user(SECRET)]}},
                )
        else:
            execute(
                pipeline,
                mode,
                {"generator": {"messages": [ChatMessage.from_user(SECRET)]}},
            )
        spans = exporter.get_finished_spans()
        span = next(s for s in spans if s.name != "Pipeline.run")
        assert span.attributes["gen_ai.provider.name"] == provider
        assert (span.status.status_code == trace.StatusCode.ERROR) == error
        assert SECRET not in str(
            [(dict(s.attributes), s.status, s.events) for s in spans]
        )
    assert len(offline_openai["requests"]) == 1


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("error", [False, True])
def test_real_azure_generator_attribution(offline_openai, mode, error):
    from haystack.components.generators.chat import AzureOpenAIChatGenerator

    generator = AzureOpenAIChatGenerator(
        api_key=Secret.from_token("synthetic-key"),
        azure_endpoint="https://resource.openai.azure.com/",
        azure_deployment="gpt-4o-mini",
        api_version="2024-10-21",
        max_retries=0,
    )
    offline_openai["status_code"] = 400 if error else 200
    with installed(capture_content=False) as (_, _, exporter):
        pipeline = pipeline_for(mode)
        pipeline.add_component("generator", generator)
        if error:
            with pytest.raises(Exception):
                execute(
                    pipeline,
                    mode,
                    {"generator": {"messages": [ChatMessage.from_user(SECRET)]}},
                )
        else:
            execute(
                pipeline,
                mode,
                {"generator": {"messages": [ChatMessage.from_user(SECRET)]}},
            )
        span = next(
            s for s in exporter.get_finished_spans() if s.name != "Pipeline.run"
        )
        assert span.attributes["gen_ai.provider.name"] == "azure"
        assert (span.status.status_code == trace.StatusCode.ERROR) == error
    assert len(offline_openai["requests"]) == 1


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_provider_uses_selected_client_not_component_configuration(
    offline_openai, mode
):
    generator = OpenAIChatGenerator(api_key=Secret.from_token("synthetic-key"))
    # New Haystack versions create clients lazily. Populate both to also catch
    # choosing the sync endpoint when run_async uses a different endpoint.
    if hasattr(generator, "warm_up"):
        generator.warm_up()
    if hasattr(generator, "warm_up_async"):
        asyncio.run(generator.warm_up_async())
    generator.client.base_url = "https://resource.openai.azure.com/openai/v1"
    if hasattr(generator, "async_client"):
        generator.async_client.base_url = "https://custom.example.invalid/v1"
    uses_async = mode == "async" and generator.__haystack_supports_async__
    with installed(capture_content=False) as (_, _, exporter):
        pipeline = pipeline_for(mode)
        pipeline.add_component("generator", generator)
        execute(
            pipeline, mode, {"generator": {"messages": [ChatMessage.from_user(SECRET)]}}
        )
        span = next(
            s for s in exporter.get_finished_spans() if s.name != "Pipeline.run"
        )
        assert span.attributes["gen_ai.provider.name"] == (
            "openai_compatible" if uses_async else "azure"
        )


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_provider_honors_sdk_environment_endpoint(offline_openai, monkeypatch, mode):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://custom.example.invalid/v1")
    generator = OpenAIChatGenerator(api_key=Secret.from_token("synthetic-key"))
    with installed(capture_content=False) as (_, _, exporter):
        pipeline = pipeline_for(mode)
        pipeline.add_component("generator", generator)
        execute(
            pipeline, mode, {"generator": {"messages": [ChatMessage.from_user(SECRET)]}}
        )
        span = next(
            s for s in exporter.get_finished_spans() if s.name != "Pipeline.run"
        )
        assert span.attributes["gen_ai.provider.name"] == "openai_compatible"


@pytest.mark.parametrize(
    "meta,expected",
    [
        (
            {"usage": {"prompt_tokens": 0, "total_tokens": 0}},
            {"input_tokens": 0, "total_tokens": 0},
        ),
        ({"usage": {"input_tokens": 4}}, {"input_tokens": 4}),
        (
            {"billed_units": {"input_tokens": 5, "output_tokens": 2}},
            {"input_tokens": 5, "output_tokens": 2},
        ),
        (
            {"usage": {"prompt_tokens": 3}, "billed_units": {"input_tokens": 9}},
            {"input_tokens": 3},
        ),
        ({"usage": {"prompt_tokens": True, "total_tokens": "11"}}, {}),
        ({"usage": {"prompt_tokens": -1, "total_tokens": float("inf")}}, {}),
        ({"usage": "malformed"}, {}),
        ({"usage": {"unrecognized": SECRET}}, {}),
        ({}, {}),
        (None, {}),
    ],
)
def test_embedding_usage_mapping_does_not_invent_counts(meta, expected):
    @component
    class MetadataEmbedder:
        @component.output_types(embedding=list[float], meta=dict)
        def run(self, text: str):
            return {"embedding": [0.25, 0.75], "meta": meta}

    with installed(capture_content=False) as (_, _, exporter):
        pipeline = Pipeline()
        pipeline.add_component("embedder", MetadataEmbedder())
        pipeline.run({"embedder": {"text": SECRET}})
        span = next(
            s for s in exporter.get_finished_spans() if s.name != "Pipeline.run"
        )
        actual = {
            key.removeprefix("gen_ai.usage."): value
            for key, value in span.attributes.items()
            if key.startswith("gen_ai.usage.")
        }
        assert actual == expected
        assert "gen_ai.provider.name" not in span.attributes
        assert SECRET not in str(dict(span.attributes))


@pytest.mark.parametrize("error", [False, True])
def test_legacy_openai_generator_provider(offline_openai, error):
    from haystack.components import generators

    generator_type = getattr(generators, "OpenAIGenerator", None)
    if generator_type is None:
        pytest.skip("OpenAIGenerator was removed in Haystack 3")
    generator = generator_type(
        api_key=Secret.from_token("synthetic-key"), max_retries=0
    )
    offline_openai["status_code"] = 400 if error else 200
    with installed(capture_content=False) as (_, _, exporter):
        pipeline = Pipeline()
        pipeline.add_component("generator", generator)
        if error:
            with pytest.raises(Exception):
                pipeline.run({"generator": {"prompt": SECRET}})
        else:
            pipeline.run({"generator": {"prompt": SECRET}})
        span = next(
            s for s in exporter.get_finished_spans() if s.name != "Pipeline.run"
        )
        assert span.attributes["gen_ai.provider.name"] == "openai"
        assert (span.status.status_code == trace.StatusCode.ERROR) == error
    assert len(offline_openai["requests"]) == 1
