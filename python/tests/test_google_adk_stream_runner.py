"""Offline integration with ADK's actual Runner and BaseAgent methods."""

import asyncio
from typing import ClassVar

import pytest
from google.adk.agents import BaseAgent
from google.adk.events import Event
from google.adk.runners import InMemoryRunner
from google.genai import types
from opentelemetry import context as context_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_google_adk import GoogleADKInstrumentor

from fi_instrumentation.fi_types import SpanAttributes


@pytest.mark.asyncio
@pytest.mark.parametrize("close_early", [False, True])
async def test_runner_stream_lifecycle(close_early):
    cleaned = []
    cleanup_finished = asyncio.Event()

    class StreamingAgent(BaseAgent):
        outputs: ClassVar[list[Event]] = [
            Event(
                author="agent",
                partial=True,
                content=types.Content(role="model", parts=[types.Part(text="partial")]),
            ),
            Event(
                author="agent",
                content=types.Content(
                    role="model", parts=[types.Part(text="complete")]
                ),
            ),
        ]

        async def _run_async_impl(self, ctx):
            try:
                for result in self.outputs:
                    yield result
            finally:
                cleaned.append(True)
                cleanup_finished.set()

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)
    instrumentor = GoogleADKInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    runner = InMemoryRunner(agent=StreamingAgent(name="agent"), app_name="stream-test")
    try:
        await runner.session_service.create_session(
            app_name="stream-test", user_id="user", session_id="session"
        )
        with tracer.start_as_current_span("consumer") as parent:
            before = context_api.get_current()
            stream = runner.run_async(
                user_id="user",
                session_id="session",
                new_message=types.Content(
                    role="user", parts=[types.Part(text="hello")]
                ),
            )
            first = await stream.__anext__()
            assert first.content.parts[0].text == "partial"
            assert context_api.get_current() is before
            if close_early:
                await stream.aclose()
                assert any(
                    span.name == "invocation [stream-test]"
                    for span in exporter.get_finished_spans()
                )
                # Older ADK runners rely on async-generator finalization for
                # nested streams rather than closing them before returning.
                await asyncio.wait_for(cleanup_finished.wait(), timeout=2)
            else:
                remaining = [result async for result in stream]
                assert remaining[-1].content.parts[0].text == "complete"
            assert cleaned == [True]
            assert context_api.get_current() is before
            spans = {s.name: s for s in exporter.get_finished_spans()}
            assert set(spans) == {"invocation [stream-test]", "agent_run [agent]"}
            invocation = spans["invocation [stream-test]"]
            agent = spans["agent_run [agent]"]
            assert invocation.parent == parent.get_span_context()
            assert agent.parent == invocation.context
            assert agent.attributes[SpanAttributes.SESSION_ID] == "session"
            if not close_early:
                assert invocation.status.status_code == StatusCode.OK
                assert agent.status.status_code == StatusCode.OK
    finally:
        instrumentor.uninstrument()
        if hasattr(runner, "close"):
            await runner.close()
        provider.shutdown()
