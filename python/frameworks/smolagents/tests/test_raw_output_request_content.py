"""Whole-run output must not carry the task and request messages that input settings hide."""
import json

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from smolagents import ToolCallingAgent
from smolagents.models import (
    ChatMessage,
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
    Model,
)
from traceai_smolagents import SmolagentsInstrumentor

TASK_SENTINEL = "SENTINEL_AGENT_TASK_2290"
ANSWER = "synthetic final answer"


class OfflineModel(Model):
    def generate(self, messages, stop_sequences=None, response_format=None,
                 tools_to_call_from=None, **kwargs):
        return ChatMessage(
            role="assistant", content="",
            tool_calls=[ChatMessageToolCall(
                id="call-1", type="function",
                function=ChatMessageToolCallFunction(
                    name="final_answer", arguments={"answer": ANSWER}),
            )],
        )


def _run(config):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = SmolagentsInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        agent = ToolCallingAgent(tools=[], model=OfflineModel())
        # RunResult.steps carries the task and the model input messages.
        result = agent.run(TASK_SENTINEL, return_full_result=True)
        assert result.output == ANSWER
        assert result.steps[0]["task"] == TASK_SENTINEL
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
    assert ANSWER in _run(TraceConfig())


@pytest.mark.parametrize(
    "trace_config",
    [{"hide_inputs": True}, {"hide_input_messages": True}, {"hide_input_text": True}],
)
def test_rawOutputOmitsRequestTaskUnderInputSettings(trace_config):
    assert TASK_SENTINEL not in _run(TraceConfig(**trace_config))
