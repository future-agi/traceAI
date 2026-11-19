"""Chain output must not carry the request input that input settings hide."""
import json

import pytest
from fi_instrumentation import TraceConfig
from langchain_core.runnables import RunnableLambda, RunnablePassthrough
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_langchain import LangChainInstrumentor

from ._core_version import requires_current_core

pytestmark = requires_current_core

INPUT_SENTINEL = "SENTINEL_CHAIN_INPUT_4402"
REPLY = "synthetic chain answer"


def _run(config):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        # RunnablePassthrough.assign returns the request input plus new keys.
        chain = RunnablePassthrough.assign(answer=RunnableLambda(lambda _: REPLY))
        result = chain.invoke({"input": INPUT_SENTINEL})
        assert result == {"input": INPUT_SENTINEL, "answer": REPLY}
        spans = exporter.get_finished_spans()
        assert spans
        return json.dumps([dict(s.attributes) for s in spans], default=str) + json.dumps(
            [[(e.name, dict(e.attributes or {})) for e in s.events] for s in spans],
            default=str,
        )
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def test_control_output_recorded_without_settings():
    assert REPLY in _run(TraceConfig())


@pytest.mark.parametrize(
    "trace_config",
    [{"hide_inputs": True}, {"hide_input_messages": True}, {"hide_input_text": True}],
)
def test_rawOutputOmitsRequestInputUnderInputSettings(trace_config):
    assert INPUT_SENTINEL not in _run(TraceConfig(**trace_config))
