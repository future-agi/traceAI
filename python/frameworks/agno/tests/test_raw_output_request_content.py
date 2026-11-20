"""Workflow output must not carry the run input that input settings hide."""
import json

import pytest
from agno.workflow import Workflow
from agno.workflow.step import Step
from agno.workflow.types import StepInput, StepOutput
from openinference.instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_agno import configure_agno_tracing

INPUT_SENTINEL = "SENTINEL_WORKFLOW_INPUT_6163"
REPLY = "synthetic workflow result"


def _run(config, content, stream):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    instrumentation = configure_agno_tracing(tracer_provider=provider, config=config)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        def step(step_input: StepInput):
            return StepOutput(content=content)

        workflow = Workflow(name="offline", steps=[Step(name="step", executor=step)], telemetry=False)
        if stream:
            for _ in workflow.run(INPUT_SENTINEL, stream=True):
                pass
        else:
            result = workflow.run(INPUT_SENTINEL)
            # WorkflowRunOutput.input is the run's request input.
            assert result.input == INPUT_SENTINEL
            assert result.content == content
        provider.force_flush()
        spans = exporter.get_finished_spans()
        assert spans
        return json.dumps([dict(s.attributes) for s in spans], default=str) + json.dumps(
            [[dict(e.attributes or {}) for e in s.events] for s in spans], default=str
        )
    finally:
        instrumentation.uninstrument()
        provider.shutdown()


@pytest.mark.parametrize("stream", [False, True])
def test_control_output_recorded_without_settings(stream):
    assert REPLY in _run(None, REPLY, stream)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "trace_config",
    [{"hide_inputs": True}, {"hide_input_messages": True}, {"hide_input_text": True}],
)
def test_rawOutputOmitsRunInputUnderInputSettings(trace_config, stream):
    assert INPUT_SENTINEL not in _run(TraceConfig(**trace_config), None, stream)
