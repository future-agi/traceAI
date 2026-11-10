"""Offline regressions for native records, retrieval context and model attributes."""

import asyncio
import importlib.util
import json
import os
from inspect import getattr_static
from types import SimpleNamespace
from unittest.mock import patch

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

import dspy
import pytest
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry import context, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_dspy import DSPyInstrumentor, _llm_model_name
from traceai_dspy._privacy import sanitize

HAS_CALLBACKS = importlib.util.find_spec("dspy.utils.callback") is not None


@pytest.fixture
def setup():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    original = list(dspy.settings.get("callbacks", []))
    token = context.attach(context.Context())
    instrumentor = DSPyInstrumentor()
    yield instrumentor, provider, exporter
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    if HAS_CALLBACKS:
        dspy.configure(callbacks=original)
    context.detach(token)
    provider.shutdown()


@pytest.mark.parametrize("direction", ["input", "output"])
@pytest.mark.parametrize("shape", ["record", "nested", "sequence"])
def test_record_credentials_are_redacted_without_mutation(setup, direction, shape):
    instrumentor, provider, exporter = setup
    instrumentor.instrument(tracer_provider=provider)
    record_type = dspy.Example if direction == "input" else dspy.Prediction
    record = record_type(text="local", api_key="SYNTHETIC_RECORD_CREDENTIAL")
    value = record
    if shape == "nested":
        value = dspy.Example(child=dspy.Prediction(record=record))
    elif shape == "sequence":
        value = {"records": [record]}

    class Program(dspy.Module):
        def forward(self, record):
            assert record is (value if direction == "input" else None)
            return value if direction == "output" else dspy.Prediction(text="local")

    result = Program()(record=value if direction == "input" else None)
    assert record.api_key == "SYNTHETIC_RECORD_CREDENTIAL"
    if direction == "output":
        assert result is value
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert not any(
        "SYNTHETIC_RECORD_CREDENTIAL" in str(item)
        for span in spans
        for item in span.attributes.values()
    )
    captured = json.loads(spans[0].attributes[f"{direction}.value"])
    if direction == "input":
        # Newer Module callbacks bind __call__(*args, **kwargs); the legacy
        # wrapper binds forward(record) directly.
        captured = captured.get("kwargs", captured)["record"]
    if shape == "nested":
        captured = captured["child"]["record"]
    elif shape == "sequence":
        captured = captured["records"][0]
    assert captured == {"text": "local", "api_key": "__REDACTED__"}


def test_credential_matching_preserves_noncredential_fields():
    fields = {
        "monkey": "visible",
        "output_tokens": 12,
        "token_count": 9,
        "azureApiKey": "hidden",
        "openai.api-key": "hidden",
    }
    assert sanitize(fields) == {
        **fields,
        "azureApiKey": "__REDACTED__",
        "openai.api-key": "__REDACTED__",
    }
    assert fields["azureApiKey"] == "hidden"


@pytest.mark.parametrize(
    "credential_field",
    [
        "azure_api_key",
        "openai_api_key",
        "azure_access_token",
        "provider_client_secret",
        "provider_token",
        "provider_key",
    ],
)
@pytest.mark.parametrize("nested", [False, True])
def test_provider_prefixed_credentials_are_redacted_in_records(setup, credential_field, nested):
    instrumentor, provider, exporter = setup
    instrumentor.instrument(tracer_provider=provider)
    secret = "SYNTHETIC_PROVIDER_SECRET"
    record = dspy.Example(**{credential_field: secret, "text": "safe"})
    argument = dspy.Example(child=dspy.Prediction(record=record)) if nested else record

    class Program(dspy.Module):
        def forward(self, data):
            assert data is argument
            return dspy.Prediction(result=argument)

    result = Program()(data=argument)
    assert result.result is argument
    assert getattr(record, credential_field) == secret
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    for attr in ("input.value", "output.value"):
        captured = json.loads(spans[0].attributes[attr])
        assert secret not in json.dumps(captured)
        if attr == "input.value":
            captured = captured.get("kwargs", captured)["data"]
        else:
            captured = captured["result"]
        if nested:
            captured = captured["child"]["record"]
        assert captured[credential_field] == "__REDACTED__"


@pytest.mark.skipif(not hasattr(dspy, "Tool"), reason="DSPy has no Tool")
def test_provider_prefixed_tool_credentials_are_redacted(setup):
    instrumentor, provider, exporter = setup
    instrumentor.instrument(tracer_provider=provider)
    secret = "SYNTHETIC_TOOL_SECRET"

    def offline_tool(openai_api_key: str):
        assert openai_api_key == secret
        return "safe"

    assert dspy.Tool(offline_tool)(openai_api_key=secret) == "safe"
    spans = exporter.get_finished_spans()
    assert spans
    assert all(secret not in str(value) for span in spans for value in span.attributes.values())


@pytest.mark.skipif(not HAS_CALLBACKS, reason="DSPy predates callbacks")
@pytest.mark.parametrize("placement", ["before", "after", "instance"])
@pytest.mark.parametrize("kind", ["native", "loaded", "late"])
@pytest.mark.parametrize("fail", [False, True])
def test_retrieve_restores_context_and_owned_hooks(setup, placement, kind, fail):
    from dspy.utils.callback import BaseCallback, with_callbacks

    instrumentor, provider, exporter = setup
    tracer = provider.get_tracer("application")
    pending = {}

    class UserCallback(BaseCallback):
        def on_module_start(self, call_id, instance, inputs):
            span = tracer.start_span("user-callback")
            pending[call_id] = (span, context.attach(trace.set_span_in_context(span)))

        def on_module_end(self, call_id, outputs, exception=None):
            span, token = pending.pop(call_id)
            context.detach(token)
            span.end()

    def subclass():
        class CustomRetrieve(dspy.Retrieve):
            @with_callbacks
            def __call__(self, *args, **kwargs):
                return self.forward(*args, **kwargs)

        return CustomRetrieve

    retrieve_class = subclass() if kind == "loaded" else dspy.Retrieve
    original_call = getattr_static(retrieve_class, "__call__")
    original_hook = getattr_static(dspy.Retrieve, "__init_subclass__")
    user = UserCallback()
    dspy.configure(callbacks=[user] if placement == "before" else [])
    instrumentor.instrument(tracer_provider=provider)
    if kind == "late":
        retrieve_class = subclass()
    if placement == "after":
        dspy.configure(callbacks=[*dspy.settings.callbacks, user])
    retriever = retrieve_class(k=1, callbacks=[user] if placement == "instance" else [])

    def retrieve(*args, **kwargs):
        if fail:
            raise ValueError("local retrieval failure")
        return [SimpleNamespace(long_text="local")]

    with dspy.context(rm=retrieve):
        with tracer.start_as_current_span("application") as parent:
            before = context.get_current()
            if fail:
                with pytest.raises(ValueError, match="local retrieval failure"):
                    retriever("query")
            else:
                assert retriever("query").passages == ["local"]
            assert not pending and not instrumentor._callback._calls
            assert context.get_current() is before
            with tracer.start_as_current_span("sibling") as sibling:
                assert sibling.parent.span_id == parent.get_span_context().span_id
    retrieval_spans = [
        span
        for span in exporter.get_finished_spans()
        if span.attributes.get(SpanAttributes.GEN_AI_SPAN_KIND) == "RETRIEVER"
    ]
    assert len(retrieval_spans) == 1
    instrumentor.uninstrument()
    assert getattr_static(dspy.Retrieve, "__init_subclass__") is original_hook
    if kind != "late":
        assert getattr_static(retrieve_class, "__call__") is original_call
    else:
        assert not hasattr(retrieve_class.__call__, "_self_wrapper")
    subclass()
    assert not instrumentor._patches


def test_lm_model_is_present_on_real_call(setup):
    from litellm import ModelResponse

    instrumentor, provider, exporter = setup
    instrumentor.instrument(tracer_provider=provider)
    response = ModelResponse(
        model="openai/gpt-4o-mini",
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", "content": "local"},
                "finish_reason": "stop",
            }
        ],
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )
    lm = dspy.LM("openai/gpt-4o-mini", engine="litellm", cache=False)
    with patch("litellm.completion", return_value=response):
        assert lm(prompt="local") == ["local"]
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert (
        spans[0].attributes[SpanAttributes.GEN_AI_REQUEST_MODEL] == "openai/gpt-4o-mini"
    )


def test_legacy_model_name_fallback():
    assert dict(_llm_model_name(SimpleNamespace(model_name="legacy"))) == {
        SpanAttributes.GEN_AI_REQUEST_MODEL: "legacy"
    }


@pytest.mark.skipif(not hasattr(dspy, "Embedder"), reason="DSPy has no Embedder")
@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.parametrize("hosted", [False, True])
def test_embedding_model_uses_canonical_attribute(setup, async_call, hosted):
    if async_call and not hasattr(dspy.Embedder, "acall"):
        pytest.skip("DSPy has no asynchronous Embedder")
    instrumentor, provider, exporter = setup
    instrumentor.instrument(tracer_provider=provider)
    model = "openai/text-embedding-3-small" if hosted else lambda texts: [[1.0, 2.0]]
    embedder = dspy.Embedder(model, caching=False)
    if hosted:
        transport = "aembedding" if async_call else "embedding"
        response = SimpleNamespace(data=[{"embedding": [1.0, 2.0]}])
        with patch(f"litellm.{transport}", return_value=response):
            result = (
                asyncio.run(embedder.acall(["local"]))
                if async_call
                else embedder(["local"])
            )
    else:
        result = (
            asyncio.run(embedder.acall(["local"]))
            if async_call
            else embedder(["local"])
        )
    assert result.tolist() == [[1.0, 2.0]]
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs[SpanAttributes.GEN_AI_SPAN_KIND] == "EMBEDDING"
    assert "embedder.model_name" not in attrs
    if hosted:
        assert attrs[SpanAttributes.EMBEDDING_MODEL_NAME] == model
    else:
        assert SpanAttributes.EMBEDDING_MODEL_NAME not in attrs
