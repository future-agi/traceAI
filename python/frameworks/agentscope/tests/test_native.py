"""Real AgentScope objects and middleware; synthetic next handlers, no vendor calls."""

import asyncio

import pytest
from agentscope.agent import Agent
from agentscope.credential import OpenAICredential
from agentscope.message import (
    AssistantMsg,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)
from agentscope.middleware import TracingMiddleware as NativeTracingMiddleware
from agentscope.model import ChatResponse, ChatUsage, OpenAIChatModel
from agentscope.state import AgentState
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_agentscope import TracingMiddleware, configure
from traceai_agentscope._projection import TOOL_ERROR


def make_agent(middleware=None):
    model = OpenAIChatModel(
        credential=OpenAICredential(
            api_key="synthetic-unused", base_url="http://127.0.0.1:9/v1"
        ),
        model="synthetic-model",
        max_retries=0,
    )
    return Agent(
        name="support",
        model=model,
        system_prompt="Synthetic test.",
        state=AgentState(session_id="session-42"),
        middlewares=[middleware] if middleware else [],
    )


def response():
    return ChatResponse(
        content=[TextBlock(text="PRIVATE_ANSWER")],
        is_last=True,
        usage=ChatUsage(input_tokens=7, output_tokens=3, time=0.01),
    )


async def run_native(agent, middleware, streaming=False):
    async def next_model(**kwargs):
        if streaming:

            async def chunks():
                yield ChatResponse(content=[TextBlock(text="partial")], is_last=False)
                yield response()

            return chunks()
        return response()

    async def next_reply(**kwargs):
        result = await middleware.on_model_call(
            agent,
            {
                "current_model": agent.model,
                "messages": [UserMsg(name="user", content="PRIVATE_PROMPT")],
            },
            next_model,
        )
        if streaming:
            async for _ in result:
                pass
        yield AssistantMsg(name="support", content="PRIVATE_ANSWER")

    async for _ in middleware.on_reply(
        agent,
        {
            "inputs": UserMsg(name="user", content="PRIVATE_PROMPT"),
        },
        next_reply,
    ):
        pass


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("capture", [False, True])
@pytest.mark.parametrize(
    "middleware_type", [NativeTracingMiddleware, TracingMiddleware]
)
def test_native_reply_model(shared, streaming, capture, middleware_type):
    provider, raw = shared
    dest = InMemorySpanExporter()
    handle = configure(
        project_name="test", provider=provider, exporter=dest, capture_content=capture
    )
    asyncio.run(run_native(make_agent(), middleware_type(), streaming))
    assert handle.force_flush()
    spans = dest.get_finished_spans()
    assert len(spans) == len(raw.get_finished_spans()) == 2
    model, reply = spans
    assert model.context == raw.get_finished_spans()[0].context
    assert model.parent.span_id == reply.context.span_id
    assert model.start_time == raw.get_finished_spans()[0].start_time
    assert reply.end_time == raw.get_finished_spans()[1].end_time
    assert [s.attributes["fi.span.kind"] for s in spans] == ["LLM", "AGENT"]
    assert all(s.attributes["session.id"] == "session-42" for s in spans)
    assert sum(s.attributes.get("gen_ai.usage.input_tokens", 0) for s in spans) == 7
    assert sum(s.attributes.get("gen_ai.usage.output_tokens", 0) for s in spans) == 3
    assert ("input.value" in model.attributes) is capture
    assert ("output.value" in reply.attributes) is capture
    if capture:
        assert "PRIVATE_PROMPT" in model.attributes["input.value"]
        assert "PRIVATE_ANSWER" in reply.attributes["output.value"]
    else:
        assert "PRIVATE_" not in str([dict(s.attributes) for s in spans])
    assert (
        "PRIVATE_PROMPT"
        in raw.get_finished_spans()[0].attributes["gen_ai.input.messages"]
    )
    assert handle.shutdown()


@pytest.mark.parametrize("streaming", [False, True])
def test_real_agent_reply(shared, monkeypatch, streaming):
    dest = InMemorySpanExporter()
    handle = configure(project_name="test", exporter=dest)
    agent = make_agent(TracingMiddleware())

    async def call_api(*args, **kwargs):
        if streaming:

            async def chunks():
                yield response()

            return chunks()
        return response()

    monkeypatch.setattr(agent.model, "_call_api", call_api)
    answer = asyncio.run(agent.reply(UserMsg(name="user", content="PRIVATE_PROMPT")))
    assert "PRIVATE_ANSWER" in str(answer)
    assert handle.force_flush()
    spans = dest.get_finished_spans()
    assert len(spans) == 2
    assert {s.attributes["fi.span.kind"] for s in spans} == {"AGENT", "LLM"}
    assert all(s.attributes["session.id"] == "session-42" for s in spans)
    assert handle.shutdown()


@pytest.mark.parametrize("error", [False, True])
def test_tool_returned_state(shared, error):
    dest = InMemorySpanExporter()
    handle = configure(project_name="test", exporter=dest)
    middleware = TracingMiddleware()

    async def run():
        async def next_tool(**kwargs):
            yield ToolResultBlock(
                id="call-1",
                name="lookup",
                output="PRIVATE_RESULT",
                state=ToolResultState.ERROR if error else ToolResultState.SUCCESS,
            )

        async for _ in middleware.on_acting(
            make_agent(),
            {
                "tool_call": ToolCallBlock(
                    id="call-1", name="lookup", input='{"q":"PRIVATE_ARGUMENT"}'
                ),
            },
            next_tool,
        ):
            pass

    asyncio.run(run())
    assert handle.force_flush()
    (span,) = dest.get_finished_spans()
    assert span.status.status_code == (StatusCode.ERROR if error else StatusCode.OK)
    assert span.attributes["fi.span.kind"] == "TOOL"
    assert span.attributes.get(TOOL_ERROR, False) is error
    assert "PRIVATE_" not in str(dict(span.attributes))
    assert handle.shutdown()


def test_large_intermediate_error_result(shared):
    dest = InMemorySpanExporter()
    handle = configure(project_name="test", exporter=dest)

    async def run():
        async def next_tool(**kwargs):
            yield ToolResultBlock(
                id="call-1",
                name="lookup",
                output="x" * 100000,
                state=ToolResultState.ERROR,
            )
            yield ToolResultBlock(id="call-1", name="lookup", output="finished")

        async for _ in TracingMiddleware().on_acting(
            make_agent(),
            {
                "tool_call": ToolCallBlock(id="call-1", name="lookup", input="{}"),
            },
            next_tool,
        ):
            pass

    asyncio.run(run())
    assert handle.force_flush()
    (span,) = dest.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert handle.shutdown()


@pytest.mark.parametrize("operation", ["model", "tool", "reply"])
@pytest.mark.parametrize("exception", [ValueError, asyncio.CancelledError])
def test_native_exceptions_and_cancellation(shared, operation, exception):
    dest = InMemorySpanExporter()
    handle = configure(project_name="test", exporter=dest, capture_content=True)
    middleware, agent = TracingMiddleware(), make_agent()

    async def run():
        async def fail_model(**kwargs):
            raise exception("PRIVATE_ERROR")

        async def fail_generator(**kwargs):
            raise exception("PRIVATE_ERROR")
            yield  # pragma: no cover

        with pytest.raises(exception):
            if operation == "model":
                await middleware.on_model_call(
                    agent, {"current_model": agent.model}, fail_model
                )
            else:
                method = (
                    middleware.on_reply
                    if operation == "reply"
                    else middleware.on_acting
                )
                async for _ in method(
                    agent,
                    {"tool_call": ToolCallBlock(id="c", name="tool", input="{}")},
                    fail_generator,
                ):
                    pass

    asyncio.run(run())
    assert handle.force_flush()
    (span,) = dest.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description is None
    assert "PRIVATE_ERROR" not in str(dict(span.attributes))
    assert all(
        "exception.stacktrace" not in e.attributes
        and "exception.message" not in e.attributes
        for e in span.events
    )
    assert handle.shutdown()


def test_closed_tool_generator_ends_native_span(shared):
    dest = InMemorySpanExporter()
    handle = configure(project_name="test", exporter=dest)

    async def run():
        async def next_tool(**kwargs):
            yield ToolResultBlock(id="c", name="tool", output="first")
            yield ToolResultBlock(id="c", name="tool", output="second")

        gen = TracingMiddleware().on_acting(
            make_agent(),
            {
                "tool_call": ToolCallBlock(id="c", name="tool", input="{}"),
            },
            next_tool,
        )
        await anext(gen)
        await gen.aclose()
        assert not trace.get_current_span().is_recording()

    asyncio.run(run())
    assert handle.force_flush()
    (span,) = dest.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert handle.shutdown()


def test_external_execution_receipt_is_not_a_tool_execution(shared):
    from agentscope.event import ExternalExecutionResultEvent

    provider, raw = shared
    dest = InMemorySpanExporter()
    handle = configure(project_name="test", exporter=dest)

    async def run():
        async def next_reply(**kwargs):
            yield AssistantMsg(name="support", content="done")

        event = ExternalExecutionResultEvent(
            reply_id="reply-1",
            execution_results=[
                ToolResultBlock(
                    id="c",
                    name="external",
                    output="external-result",
                    state=ToolResultState.SUCCESS,
                ),
            ],
        )
        async for _ in TracingMiddleware().on_reply(
            make_agent(), {"inputs": event}, next_reply
        ):
            pass

    asyncio.run(run())
    assert handle.force_flush()
    assert len(raw.get_finished_spans()) == 2
    (exported,) = dest.get_finished_spans()
    assert exported.attributes["fi.span.kind"] == "AGENT"
    assert handle.shutdown()


@pytest.mark.parametrize("error", [False, True])
def test_real_agent_model_tool_model_trace(shared, monkeypatch, error):
    from agentscope.permission import PermissionBehavior, PermissionDecision
    from agentscope.tool import FunctionTool, ToolChunk

    dest = InMemorySpanExporter()
    handle = configure(project_name="test", exporter=dest)
    agent = make_agent(TracingMiddleware())
    calls = []

    async def lookup(q: str):
        """Look up a synthetic item."""
        calls.append(q)
        return ToolChunk(
            content=[TextBlock(text="PRIVATE_TOOL_OUTPUT")],
            state=ToolResultState.ERROR if error else ToolResultState.SUCCESS,
        )

    tool = FunctionTool(
        lookup,
        permission=PermissionDecision(PermissionBehavior.ALLOW, "Synthetic test"),
    )
    responses = iter(
        [
            ChatResponse(
                content=[
                    ToolCallBlock(id="c", name="lookup", input='{"q":"synthetic"}')
                ],
                is_last=True,
                usage=ChatUsage(input_tokens=7, output_tokens=3, time=0.01),
            ),
            response(),
        ]
    )

    async def call_api(*args, **kwargs):
        return next(responses)

    monkeypatch.setattr(agent.model, "_call_api", call_api)

    async def run():
        await agent.toolkit.add_tool(tool)
        return await agent.reply(UserMsg(name="user", content="PRIVATE_PROMPT"))

    assert "PRIVATE_ANSWER" in str(asyncio.run(run()))
    assert calls == ["synthetic"]
    assert handle.force_flush()
    spans = dest.get_finished_spans()
    assert len(spans) == 4  # model, actual tool, model, agent: no duplicate SDK spans
    root = next(s for s in spans if s.attributes["fi.span.kind"] == "AGENT")
    assert all(s.parent.span_id == root.context.span_id for s in spans if s is not root)
    tool_span = next(s for s in spans if s.attributes["fi.span.kind"] == "TOOL")
    assert tool_span.status.status_code == (
        StatusCode.ERROR if error else StatusCode.OK
    )
    assert sum(s.attributes.get("gen_ai.usage.input_tokens", 0) for s in spans) == 14
    assert sum(s.attributes.get("gen_ai.usage.output_tokens", 0) for s in spans) == 6
    assert handle.shutdown()


def test_concurrent_replies_keep_separate_parents(shared):
    dest = InMemorySpanExporter()
    handle = configure(project_name="test", exporter=dest)

    async def run():
        await asyncio.gather(
            *(run_native(make_agent(), TracingMiddleware(), True) for _ in range(10))
        )

    asyncio.run(run())
    assert handle.force_flush()
    spans = dest.get_finished_spans()
    roots = {
        s.context.span_id for s in spans if s.attributes["fi.span.kind"] == "AGENT"
    }
    children = [s for s in spans if s.attributes["fi.span.kind"] == "LLM"]
    assert len(roots) == len(children) == 10
    assert {s.parent.span_id for s in children} == roots
    assert handle.shutdown()


def test_readme_application_example(shared, monkeypatch):
    from pathlib import Path

    import traceai_agentscope
    from test_lifecycle import Exporter

    exporter = Exporter()
    actual_configure = traceai_agentscope.configure
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-unused")
    monkeypatch.setattr(
        traceai_agentscope,
        "configure",
        lambda **kwargs: actual_configure(exporter=exporter, **kwargs),
    )

    async def call_api(*args, **kwargs):
        return response()

    monkeypatch.setattr(OpenAIChatModel, "_call_api", call_api)
    readme = Path(__file__).parents[1].joinpath("README.md").read_text()
    source = readme.split("```python\n", 1)[1].split("\n```", 1)[0]
    exec(compile(source, "README.md example", "exec"), {})
    assert exporter.closed == 1
    assert len(exporter.spans) == 2
    assert {s.resource.attributes["project_name"] for s in exporter.spans} == {
        "support-agent"
    }


def test_tool_content_uses_only_structured_native_output(shared):
    from importlib.metadata import version

    dest = InMemorySpanExporter()
    handle = configure(project_name="test", exporter=dest, capture_content=True)

    async def run():
        async def next_tool(**kwargs):
            yield ToolResultBlock(
                id="c",
                name="tool",
                output="APPROVED_OUTPUT",
                state=ToolResultState.ERROR,
                metadata={
                    "api_key": "PRIVATE_CREDENTIAL",
                    "exception.stacktrace": "PRIVATE_STACK",
                },
            )

        async for _ in TracingMiddleware().on_acting(
            make_agent(),
            {
                "tool_call": ToolCallBlock(
                    id="c", name="tool", input='{"q":"approved"}'
                ),
            },
            next_tool,
        ):
            pass

    asyncio.run(run())
    assert handle.force_flush()
    (span,) = dest.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert "PRIVATE_" not in span.to_json()
    if version("agentscope") == "2.0.10":
        assert "output.value" not in span.attributes
    else:
        assert "APPROVED_OUTPUT" in span.attributes["output.value"]
    assert handle.shutdown()
