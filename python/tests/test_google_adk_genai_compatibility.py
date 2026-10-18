"""Run the real ADK model/tool pipeline without credentials or provider calls."""

import json
from importlib import import_module

import pytest
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner, Runner
from google.genai import types
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes
from traceai_google_adk import (
    GoogleADKInstrumentor,
    _PassthroughTracer,
    _SelectiveExecuteToolTracer,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_model", [False, True])
@pytest.mark.parametrize("parallel", [False, True])
async def test_real_model_tool_pipeline(fail_model, parallel):
    requests, calls = [], []
    problem = RuntimeError("offline model failure")

    class ScriptedModel(BaseLlm):
        turn: int = 0

        async def generate_content_async(self, llm_request, stream=False):
            requests.append(
                [
                    content.model_dump(exclude_none=True)
                    for content in llm_request.contents
                ]
            )
            self.turn += 1
            if self.turn == 1:
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                function_call=types.FunctionCall(
                                    id="call-" + str(value),
                                    name="add_one",
                                    args={"value": value},
                                )
                            )
                            for value in ([41, 42] if parallel else [41])
                        ],
                    )
                )
            elif self.turn == 2:
                if fail_model:
                    raise problem
                yield LlmResponse(
                    content=types.Content(role="model", parts=[types.Part(text="42")]),
                    usage_metadata=types.GenerateContentResponseUsageMetadata(
                        prompt_token_count=3,
                        candidates_token_count=2,
                        total_token_count=5,
                    ),
                )
            else:
                pytest.fail("the model/tool loop must terminate after two model turns")

    def add_one(value: int) -> dict:
        """Add one to a number."""
        calls.append(value)
        return {"value": value + 1}

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    original_run_async = Runner.run_async
    from google.adk.flows.llm_flows import base_llm_flow, functions

    llm_module = (
        base_llm_flow
        if hasattr(base_llm_flow, "trace_call_llm")
        else import_module("google.adk.flows.llm_flows.core._model_call")
    )
    merged_module = (
        functions
        if hasattr(functions, "tracer")
        else import_module("google.adk.flows.llm_flows.tools._batch_executor")
    )
    original_llm_tracer = llm_module.tracer
    original_trace_call = llm_module.trace_call_llm
    original_merged_tracer = merged_module.tracer
    instrumentor = GoogleADKInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    runner = InMemoryRunner(
        app_name="compatibility",
        agent=LlmAgent(
            name="agent", model=ScriptedModel(model="offline-model"), tools=[add_one]
        ),
    )
    try:
        await runner.session_service.create_session(
            app_name="compatibility", user_id="user", session_id="session"
        )
        stream = runner.run_async(
            user_id="user",
            session_id="session",
            new_message=types.Content(
                role="user", parts=[types.Part(text="increment 41")]
            ),
        )
        if fail_model:
            with pytest.raises(RuntimeError) as caught:
                _ = [event async for event in stream]
            assert caught.value is problem
        else:
            events = [event async for event in stream]
            assert events[-1].content.parts[0].text == "42"
        assert sorted(calls) == ([41, 42] if parallel else [41])
        assert len(requests) == 2
        responses = [
            part["function_response"]
            for content in requests[1]
            for part in content.get("parts", [])
            if "function_response" in part
        ]
        assert sorted(response["response"]["value"] for response in responses) == (
            [42, 43] if parallel else [42]
        )
        spans = exporter.get_finished_spans()
        invocation = next(s for s in spans if s.name == "invocation [compatibility]")
        agent = next(s for s in spans if s.name == "agent_run [agent]")
        assert agent.parent == invocation.context
        assert agent.attributes[SpanAttributes.SESSION_ID] == "session"
        tool_spans = [
            s
            for s in spans
            if s.attributes.get(SpanAttributes.GEN_AI_SPAN_KIND)
            == FiSpanKindValues.TOOL.value
        ]
        assert len(tool_spans) == (2 if parallel else 1)
        for tool in tool_spans:
            assert tool.attributes[SpanAttributes.GEN_AI_TOOL_NAME] == "add_one"
        assert sorted(
            json.loads(tool.attributes[SpanAttributes.INPUT_VALUE])["value"]
            for tool in tool_spans
        ) == ([41, 42] if parallel else [41])
        if parallel:
            merged = next(s for s in spans if s.name == "execute_tool (merged)")
            assert merged.context.trace_id == agent.context.trace_id
            assert all(
                tool.context.trace_id == merged.context.trace_id for tool in tool_spans
            )
        assert len(spans) == (7 if parallel else 5)
        if fail_model:
            assert invocation.status.status_code == StatusCode.ERROR
            assert agent.status.status_code == StatusCode.ERROR
        else:
            assert invocation.status.status_code == StatusCode.OK
            llm_spans = [
                s
                for s in spans
                if s.attributes.get(SpanAttributes.GEN_AI_SPAN_KIND)
                == FiSpanKindValues.LLM.value
            ]
            assert len(llm_spans) == 2
            final = llm_spans[-1]
            assert (
                final.attributes[SpanAttributes.GEN_AI_REQUEST_MODEL] == "offline-model"
            )
            assert final.attributes[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] == 5
            assert final.attributes[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] == 3
            assert final.attributes[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] == 2
    finally:
        instrumentor.uninstrument()
        assert Runner.run_async is original_run_async
        assert llm_module.tracer is original_llm_tracer
        assert llm_module.trace_call_llm is original_trace_call
        assert merged_module.tracer is original_merged_tracer
        if hasattr(runner, "close"):
            await runner.close()
        provider.shutdown()


@pytest.mark.parametrize("selective", [False, True])
def test_passthrough_start_span_does_not_end_the_parent(selective):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)
    proxy = (
        _SelectiveExecuteToolTracer(tracer, tracer)
        if selective
        else _PassthroughTracer(tracer)
    )
    try:
        with tracer.start_as_current_span("parent") as parent:
            suppressed = proxy.start_span("invoke_agent agent")
            assert suppressed.get_span_context() == parent.get_span_context()
            assert not suppressed.is_recording()
            suppressed.end()
            assert parent.is_recording()
            if selective:
                tool = proxy.start_span("execute_tool add_one")
                assert tool.is_recording()
                tool.end()
                assert len(exporter.get_finished_spans()) == 1
                assert parent.is_recording()
        assert len(exporter.get_finished_spans()) == (2 if selective else 1)
    finally:
        provider.shutdown()
