"""Standalone Workflows uses the same dispatcher as LlamaIndex core.

No model API or collector is needed: the real workflow and MockLLM emit spans,
which are checked again after OTLP protobuf serialization.
"""

import asyncio

import pytest
from fi_instrumentation import TraceConfig
from llama_index.core.instrumentation import get_dispatcher as core_dispatcher
from llama_index.core.llms import MockLLM
from llama_index_instrumentation import get_dispatcher
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_llamaindex import LlamaIndexInstrumentor
from workflows import Workflow, step
from workflows.events import Event, StartEvent, StopEvent


PROMPT = "synthetic-workflow-input"
RESULT = "synthetic-workflow-output"


class PreparedEvent(Event):
    text: str


class TwoStepWorkflow(Workflow):
    @step
    async def prepare(self, ev: StartEvent) -> PreparedEvent:
        return PreparedEvent(text=ev.text)

    @step
    async def answer(self, ev: PreparedEvent) -> StopEvent:
        response = await MockLLM(max_tokens=2).acomplete(ev.text)
        assert response.text == "text text"
        return StopEvent(result=RESULT)


def run_workflow():
    async def run():
        return await TwoStepWorkflow(timeout=10).run(text=PROMPT)

    return asyncio.run(run())


@pytest.mark.parametrize("hide_content", [False, True])
def test_standalone_workflow_parentage_and_privacy(hide_content):
    assert core_dispatcher() is get_dispatcher()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LlamaIndexInstrumentor()
    try:
        instrumentor.instrument(
            tracer_provider=provider,
            config=TraceConfig(hide_inputs=hide_content, hide_outputs=hide_content),
        )
        # Reusing the public entry point must not attach a second handler.
        instrumentor.instrument(tracer_provider=provider)
        assert run_workflow() == RESULT
        assert provider.force_flush()
        spans = exporter.get_finished_spans()
        # MockLLM.acomplete delegates to complete; Workflows 1.x also emits _done.
        assert len(spans) in (5, 6)

        payload = encode_spans(spans).SerializeToString()
        request = ExportTraceServiceRequest.FromString(payload)
        wire_spans = [
            span
            for resource in request.resource_spans
            for scope in resource.scope_spans
            for span in scope.spans
        ]
        by_name = {span.name: span for span in wire_spans}
        expected = {
            "TwoStepWorkflow.run", "TwoStepWorkflow.prepare",
            "TwoStepWorkflow.answer", "MockLLM.acomplete", "MockLLM.complete",
        }
        assert expected <= set(by_name) <= expected | {"TwoStepWorkflow._done"}
        assert len(by_name) == len(wire_spans) == len(spans)
        root = by_name["TwoStepWorkflow.run"]
        assert not root.parent_span_id
        for name in ("TwoStepWorkflow.prepare", "TwoStepWorkflow.answer"):
            assert by_name[name].parent_span_id == root.span_id
        assert by_name["MockLLM.acomplete"].parent_span_id == by_name["TwoStepWorkflow.answer"].span_id
        assert by_name["MockLLM.complete"].parent_span_id == by_name["MockLLM.acomplete"].span_id
        if "TwoStepWorkflow._done" in by_name:
            assert by_name["TwoStepWorkflow._done"].parent_span_id == root.span_id
        assert len({span.span_id for span in wire_spans}) == len(wire_spans)
        assert all(span.trace_id == root.trace_id for span in wire_spans)
        for name, span in by_name.items():
            assert span.status.code == 1  # OTLP STATUS_CODE_OK
            attrs = {attr.key: attr.value.string_value for attr in span.attributes}
            assert attrs["gen_ai.span.kind"] == ("LLM" if name.startswith("MockLLM.") else "CHAIN")
        for value in (PROMPT, RESULT, "text text"):
            assert (value.encode() in payload) is not hide_content

        instrumentor.uninstrument()
        exporter.clear()
        assert run_workflow() == RESULT
        assert not exporter.get_finished_spans()
    finally:
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
        provider.shutdown()
