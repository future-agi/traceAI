"""Offline Runner contracts with the real SDK and only the HTTP transport replaced."""

import asyncio
import json
import logging

import agents
import httpx
import pytest
from agents import Agent, ModelSettings, Runner, set_trace_processors, set_tracing_disabled
from agents.models.openai_responses import OpenAIResponsesModel
from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import MessageAttributes, SpanAttributes, ToolCallAttributes
from openai import AsyncOpenAI
from openai.types.responses import ResponseCustomToolCall, ResponseFunctionToolCall, ResponseOutputMessage
from opentelemetry.sdk.trace import SpanProcessor
from traceai_openai_agents import OpenAIAgentsInstrumentor
from traceai_openai_agents._processor import _get_attributes_from_response_output


class _StartedSpans(SpanProcessor):
    def __init__(self):
        self.spans = []

    def on_start(self, span, parent_context=None):
        self.spans.append(span)

    def on_end(self, span):
        pass


@pytest.fixture
def runner_tracing(tracer_provider):
    # No vendor exporter, even in the uninstrumented controls.
    set_trace_processors([])
    set_tracing_disabled(False)
    started = _StartedSpans()
    tracer_provider.add_span_processor(started)
    yield started
    set_trace_processors([])
    instrumentor = OpenAIAgentsInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()


async def _run(custom):
    requests, invocations = [], []

    async def invoke(ctx, value):
        invocations.append(value)
        return "synthetic tool result"

    native = hasattr(agents, "CustomTool")
    kwargs = {}
    if custom:
        if native:
            kwargs["tools"] = [agents.CustomTool(
                name="plain_tool", description="Accept plain text", on_invoke_tool=invoke,
            )]
        else:
            # Older SDKs forward this public setting but do not execute custom tools.
            kwargs["model_settings"] = ModelSettings(extra_body={
                "tools": [{"type": "custom", "name": "plain_tool", "description": "Accept plain text"}],
                "tool_choice": {"type": "custom", "name": "plain_tool"},
            })

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path == "/v1/responses"
        if custom and len(requests) == 1:
            assert body["tools"][0]["type"] == "custom"
            output = [{"type": "custom_tool_call", "id": "ctc_synthetic", "call_id": "call_synthetic",
                       "name": "plain_tool", "input": "synthetic argument"}]
        else:
            output = [{"type": "message", "id": "msg_synthetic", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": "synthetic answer", "annotations": []}]}]
        return httpx.Response(200, json={
            "id": "resp_synthetic_" + str(len(requests)), "object": "response", "created_at": 1,
            "model": "gpt-4.1", "status": "completed", "output": output, "tools": body.get("tools", []),
            "parallel_tool_calls": False, "tool_choice": "auto",
            "usage": {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        async with AsyncOpenAI(api_key="synthetic-not-a-credential", base_url="https://offline.invalid/v1",
                               http_client=http, max_retries=0) as client:
            agent = Agent(name="Assistant", instructions="Be concise",
                          model=OpenAIResponsesModel(model="gpt-4.1", openai_client=client), **kwargs)
            result = await Runner.run(agent, "synthetic prompt", max_turns=3)
    if custom and native:
        assert invocations == ["synthetic argument"]
        assert len(requests) == 2
        assert any(item.get("type") == "custom_tool_call_output" and item.get("output") == "synthetic tool result"
                   for item in requests[1]["input"])
    else:
        assert invocations == []
        assert len(requests) == 1
    assert result.final_output == ("" if custom and not native else "synthetic answer")
    return result.final_output


def _assert_all_finished(started, exporter):
    assert started.spans
    unfinished = [span.name for span in started.spans if span.is_recording()]
    assert unfinished == [], f"Unfinished spans: {unfinished}"
    finished = exporter.get_finished_spans()
    assert {s.context.span_id for s in finished} == {s.context.span_id for s in started.spans}
    assert len(finished) == len(started.spans)
    assert all(s.end_time >= s.start_time for s in finished)


@pytest.mark.parametrize("custom", [False, True], ids=["text", "custom"])
def test_uninstrumented_runner_control(custom, runner_tracing, in_memory_span_exporter):
    asyncio.run(_run(custom))
    assert runner_tracing.spans == []
    assert in_memory_span_exporter.get_finished_spans() == ()


def test_instrumented_text_control(runner_tracing, tracer_provider, in_memory_span_exporter, caplog):
    OpenAIAgentsInstrumentor().instrument(tracer_provider=tracer_provider)
    with caplog.at_level(logging.ERROR):
        asyncio.run(_run(False))
    _assert_all_finished(runner_tracing, in_memory_span_exporter)
    assert not [r for r in caplog.records if "trace processor" in r.getMessage()]


@pytest.mark.parametrize("hidden", [False, True], ids=["visible", "hidden"])
def test_runner_custom_tool_finalizes_all_spans(
    hidden, runner_tracing, tracer_provider, in_memory_span_exporter, caplog,
):
    OpenAIAgentsInstrumentor().instrument(
        tracer_provider=tracer_provider,
        config=TraceConfig(hide_inputs=hidden, hide_outputs=hidden, pii_redaction=False),
    )
    with caplog.at_level(logging.ERROR):
        asyncio.run(_run(True))
    _assert_all_finished(runner_tracing, in_memory_span_exporter)
    assert not [r for r in caplog.records if "trace processor" in r.getMessage()]
    spans = in_memory_span_exporter.get_finished_spans()
    if hidden:
        exported = json.dumps([dict(s.attributes) for s in spans])
        for marker in ("synthetic prompt", "synthetic argument", "synthetic answer", "synthetic tool result"):
            assert marker not in exported
    else:
        first_response = next(s for s in spans if s.name == "response")
        prefix = f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_TOOL_CALLS}.0."
        assert first_response.attributes[prefix + ToolCallAttributes.TOOL_CALL_ID] == "call_synthetic"
        assert first_response.attributes[prefix + ToolCallAttributes.TOOL_CALL_FUNCTION_NAME] == "plain_tool"
        assert json.loads(first_response.attributes[prefix + ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON]) == {
            "input": "synthetic argument",
        }
    assert "synthetic-not-a-credential" not in json.dumps([dict(s.attributes) for s in spans])


@pytest.mark.parametrize("preceding", ["none", "message", "function", "custom"])
def test_custom_tool_output_indices(preceding):
    custom = ResponseCustomToolCall(type="custom_tool_call", call_id="call_second", name="plain_tool", input="second")
    outputs = []
    msg_idx, tool_idx = 0, 0
    if preceding == "message":
        outputs.append(ResponseOutputMessage(id="msg_first", type="message", role="assistant", status="completed", content=[]))
        msg_idx = 1
    elif preceding == "function":
        outputs.append(ResponseFunctionToolCall(type="function_call", call_id="call_first", name="function_tool", arguments="{}"))
        tool_idx = 1
    elif preceding == "custom":
        outputs.append(ResponseCustomToolCall(type="custom_tool_call", call_id="call_first", name="plain_tool", input="first"))
        tool_idx = 1
    outputs.append(custom)
    attrs = dict(_get_attributes_from_response_output(outputs))
    prefix = f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{msg_idx}."
    assert attrs[prefix + MessageAttributes.MESSAGE_ROLE] == "assistant"
    call_prefix = prefix + f"{MessageAttributes.MESSAGE_TOOL_CALLS}.{tool_idx}."
    assert attrs[call_prefix + ToolCallAttributes.TOOL_CALL_ID] == "call_second"
    if tool_idx:
        assert attrs[prefix + f"{MessageAttributes.MESSAGE_TOOL_CALLS}.0." + ToolCallAttributes.TOOL_CALL_ID] == "call_first"


def test_conversion_failure_still_finalizes_response_and_root(
    runner_tracing, tracer_provider, in_memory_span_exporter, monkeypatch, caplog,
):
    # Supplemental fault injection at our own converter, not an upstream SDK mock.
    import traceai_openai_agents._processor as processor_module

    calls = []

    def broken_conversion(response, **kwargs):
        calls.append(kwargs.get("include_usage", True))
        raise ValueError("synthetic conversion detail must not be logged")

    monkeypatch.setattr(processor_module, "_get_attributes_from_response", broken_conversion)
    OpenAIAgentsInstrumentor().instrument(tracer_provider=tracer_provider)
    with caplog.at_level(logging.WARNING):
        asyncio.run(_run(False))
    assert True in calls and False in calls  # response and root conversion both ran
    _assert_all_finished(runner_tracing, in_memory_span_exporter)
    assert "synthetic conversion detail" not in caplog.text
