"""Chain output must not carry the request input that input settings hide."""
import json

import pytest
from fi_instrumentation import TraceConfig
from langchain_core.runnables import RunnableLambda, RunnablePassthrough
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_langchain import LangChainInstrumentor

from ._core_version import assert_hidden, dump

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
        return spans
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def test_control_output_recorded_without_settings():
    assert REPLY in dump(_run(TraceConfig()))


@pytest.mark.parametrize(
    "trace_config",
    [{"hide_inputs": True}, {"hide_input_messages": True}, {"hide_input_text": True}],
)
def test_rawOutputOmitsRequestInputUnderInputSettings(trace_config):
    assert_hidden(_run(TraceConfig(**trace_config)), INPUT_SENTINEL)
