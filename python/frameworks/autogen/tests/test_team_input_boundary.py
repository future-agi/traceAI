"""Real AgentChat task/result boundaries respect independent input privacy."""

import asyncio

import pytest
from autogen_agentchat.agents import UserProxyAgent
from autogen_agentchat.base import TaskResult
from autogen_agentchat.conditions import MaxMessageTermination
from autogen_agentchat.messages import TextMessage
from autogen_agentchat.teams import RoundRobinGroupChat
from fi_instrumentation import FITracer, TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_autogen._v04_wrapper import wrap_team_run


@pytest.mark.parametrize("policy", ["hide_inputs", "hide_input_messages", "hide_input_text"])
@pytest.mark.parametrize("streaming", [False, True], ids=["call", "stream"])
@pytest.mark.parametrize("task_form", ["message", "list", "string"])
@pytest.mark.parametrize("input_only", [True, False], ids=["no-answer", "user-answer"])
@pytest.mark.parametrize("output_task_messages", [True, False], ids=["echo-task", "omit-task"])
def test_real_team_task_boundary(policy, streaming, task_form, input_only, output_task_messages):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    config = TraceConfig(**{
        flag: flag == policy for flag in (
            "hide_inputs", "hide_input_messages", "hide_input_text",
            "hide_outputs", "hide_output_messages", "hide_output_text",
        )
    })
    tracer = FITracer(provider.get_tracer(__name__), config=config)
    inputs = [TextMessage(content="private-task-text", source="user" if task_form == "string" else "operator")]
    if task_form == "list":
        inputs.insert(0, TextMessage(content="private-earlier-text", source="operator"))
    task = inputs if task_form == "list" else inputs[0].content if task_form == "string" else inputs[0]
    calls = []

    async def reply(prompt, cancellation_token=None):
        calls.append(prompt)
        return "public-answer"

    async def exercise():
        # Termination on the supplied task creates a genuine input-only result.
        # A genuine answer may itself have source="user": source is not a role.
        team = RoundRobinGroupChat(
            [UserProxyAgent("user", input_func=reply)],
            termination_condition=MaxMessageTermination(len(inputs)) if input_only else None,
            max_turns=1,
        )
        method = "run_stream" if streaming else "run"
        wrapped = wrap_team_run(getattr(type(team), method), tracer, method, config)
        arguments = {"task": task, "output_task_messages": output_task_messages}
        if streaming:
            items = [item async for item in wrapped(team, **arguments)]
            result = items[-1]
            assert isinstance(result, TaskResult)
            assert items[:-1] == result.messages
        else:
            result = await wrapped(team, **arguments)
        input_count = len(inputs) if output_task_messages else 0
        assert [message.content for message in result.messages[:input_count]] == [
            message.content for message in inputs[:input_count]
        ]
        if input_only:
            assert len(result.messages) == input_count
        else:
            # UserProxyAgent can also emit a UserInputRequestedEvent.
            assert len(result.messages) > input_count
            assert result.messages[-1].source == "user"
            assert result.messages[-1].content == "public-answer"

    try:
        asyncio.run(exercise())
        finished, = exporter.get_finished_spans()
        assert finished.attributes["input.value"] == "__REDACTED__"
        assert finished.attributes["autogen.task.content"] == "__REDACTED__"
        if input_only:
            assert calls == []
            assert "output.value" not in finished.attributes
            assert "autogen.task.result" not in finished.attributes
        else:
            assert len(calls) == 1
            assert finished.attributes["output.value"] == "public-answer"
            assert finished.attributes["autogen.task.result"] == "public-answer"
        assert "private-task-text" not in finished.to_json()
        assert "private-earlier-text" not in finished.to_json()
        assert [message.content for message in inputs] == (
            ["private-earlier-text", "private-task-text"] if task_form == "list" else ["private-task-text"]
        )
    finally:
        provider.shutdown()
