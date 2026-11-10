"""Exercise the documented registration route against a loopback OTLP receiver."""

import importlib.util
from pathlib import Path

import litellm
import pytest

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType, SpanAttributes
from fi_instrumentation.otel import HTTPSpanExporter
from traceai_litellm import LiteLLMInstrumentor

spec = importlib.util.spec_from_file_location(
    "litellm_test_receiver",
    Path(__file__).resolve().parents[3] / "tests" / "harness" / "__init__.py",
)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


@pytest.mark.parametrize("streaming", [False, True])
def test_registered_litellm_exports_to_collector(monkeypatch, streaming):
    monkeypatch.setenv("FI_API_KEY", "synthetic-api")
    monkeypatch.setenv("FI_SECRET_KEY", "synthetic-secret")
    with harness.Receiver() as receiver:
        provider = register(
            project_name="litellm-contract",
            project_type=ProjectType.OBSERVE,
            batch=False,
            verbose=False,
            span_exporter=HTTPSpanExporter(endpoint=receiver.collector_endpoint),
        )
        instrumentor = LiteLLMInstrumentor()
        instrumentor.instrument(tracer_provider=provider)
        try:
            result = litellm.completion(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Be helpful"},
                    {"role": "user", "content": "Hello"},
                ],
                mock_response="hello",
                api_key="synthetic-provider-key",
                stream=streaming,
            )
            if streaming:
                list(result)
            provider.force_flush()
        finally:
            instrumentor.uninstrument()
            provider.shutdown()
        spans = receiver.spans()
        assert len(spans) == 1
        assert spans[0]["status"]["code"] == "STATUS_CODE_OK"
        attrs = harness._flatten_attributes(spans[0]["attributes"])
        assert attrs[SpanAttributes.GEN_AI_SPAN_KIND] == "LLM"
        assert attrs[SpanAttributes.GEN_AI_REQUEST_MODEL] == "gpt-4o-mini"
        assert "Be helpful" in attrs[SpanAttributes.INPUT_VALUE]
        assert "Hello" in attrs[SpanAttributes.INPUT_VALUE]
        assert attrs[SpanAttributes.OUTPUT_VALUE] == "hello"
        request = receiver.requests()[0]
        assert request["path"] == "/tracer/v1/traces"
        assert request["headers"]["x-api-key"] == "synthetic-api"
        assert request["headers"]["x-secret-key"] == "synthetic-secret"
        resource = request["resource_attributes"][0]
        assert resource["project_name"] == "litellm-contract"
        assert resource["project_type"] == ProjectType.OBSERVE.value
