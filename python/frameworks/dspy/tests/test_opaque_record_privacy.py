"""Ensure application records do not turn credentials into opaque repr strings."""

import json
from dataclasses import dataclass

import dspy
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_dspy import DSPyInstrumentor


@dataclass
class ProviderConfig:
    api_key: str
    model: str = "local"


class OpaqueConfig:
    def __init__(self, secret):
        self.api_key = secret

    def __repr__(self):
        return f"OpaqueConfig(api_key={self.api_key!r})"


@pytest.mark.parametrize("record_type", [ProviderConfig, OpaqueConfig])
@pytest.mark.parametrize("nested", [False, True])
def test_record_credentials_not_exported_or_mutated(record_type, nested):
    secret = "SYNTHETIC_CREDENTIAL_445"
    record = record_type(secret)
    argument = {"child": [record]} if nested else record

    class Program(dspy.Module):
        def forward(self, config):
            assert config is argument
            assert record.api_key == secret
            return dspy.Prediction(config=argument)

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = DSPyInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        assert Program()(config=argument).config is argument
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
    assert record.api_key == secret
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert all(secret not in str(value) for span in spans for value in span.attributes.values())
    for direction in ("input", "output"):
        captured = json.loads(spans[0].attributes[f"{direction}.value"])
        if direction == "input":
            captured = captured.get("kwargs", captured)["config"]
        else:
            captured = captured["config"]
        if nested:
            captured = captured["child"][0]
        if record_type is ProviderConfig:
            assert captured == {"api_key": "__REDACTED__", "model": "local"}
        else:
            assert captured == "[object omitted]"
