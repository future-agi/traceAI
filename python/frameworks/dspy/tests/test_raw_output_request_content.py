"""Retriever span output must not carry the retrieval query that input settings hide."""
import json

import dspy
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_dspy import DSPyInstrumentor

QUERY_SENTINEL = "SENTINEL_RETRIEVAL_QUERY_7741"
PASSAGE = "synthetic retrieved passage"


class OfflineRetriever:
    def __init__(self, fail):
        self.fail = fail

    def __call__(self, query, k=None, **kwargs):
        if self.fail:
            raise RuntimeError("synthetic retriever failure")
        return [dspy.Prediction(long_text=PASSAGE)]


def _run(config, fail):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = DSPyInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        dspy.configure(rm=OfflineRetriever(fail))
        try:
            dspy.Retrieve(k=1).forward(QUERY_SENTINEL)
        except RuntimeError:
            assert fail
        spans = [s for s in exporter.get_finished_spans() if s.name == "Retrieve.forward"]
        assert len(spans) == 1
        return json.dumps(dict(spans[0].attributes), default=str) + json.dumps(
            [(e.name, dict(e.attributes or {})) for e in spans[0].events], default=str
        )
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def test_control_output_recorded_without_settings():
    assert PASSAGE in _run(TraceConfig(), fail=False)


@pytest.mark.parametrize(
    "trace_config",
    [{"hide_inputs": True}, {"hide_input_messages": True}, {"hide_input_text": True}],
)
def test_retrieverOutputOmitsQueryUnderInputSettings(trace_config):
    assert QUERY_SENTINEL not in _run(TraceConfig(**trace_config), fail=True)
