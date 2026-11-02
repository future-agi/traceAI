"""Current ADK workflow/node APIs use the same native adapter."""

import asyncio

import pytest
from google.adk.runners import InMemoryRunner
from google.genai import types
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_google_adk import GoogleADKInstrumentor


@pytest.mark.asyncio
async def test_workflow_and_node_spans_after_late_import():
    instrumentor = GoogleADKInstrumentor()
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor.instrument(tracer_provider=provider)
    runner = None
    try:
        # Import after instrumentation exercises late-bound native tracer aliases.
        workflow = pytest.importorskip(
            "google.adk.workflow", reason="Workflow was added after ADK 1.2.1"
        )

        async def answer(node_input: str) -> str:
            return "synthetic workflow response"

        node = workflow.FunctionNode(name="answer", func=answer)
        root = workflow.Workflow(name="pipeline", edges=[(workflow.START, node)])
        runner = InMemoryRunner(node=root, app_name="workflow_test")
        await runner.session_service.create_session(
            app_name="workflow_test", user_id="u", session_id="s"
        )
        events = [
            event
            async for event in runner.run_async(
                user_id="u",
                session_id="s",
                new_message=types.Content(
                    role="user", parts=[types.Part(text="synthetic")]
                ),
            )
        ]
        assert events
        await asyncio.sleep(0)
        spans = exporter.get_finished_spans()
        roots = [s for s in spans if s.name == "invoke_workflow pipeline"]
        nodes = [s for s in spans if s.name == "invoke_node answer"]
        assert len(roots) == len(nodes) == 1
        assert nodes[0].parent == roots[0].context
        assert all(
            s.attributes["gen_ai.span.kind"] == "CHAIN" for s in [*roots, *nodes]
        )
    finally:
        if runner is not None:
            await runner.close()
        instrumentor.uninstrument()
        provider.shutdown()
