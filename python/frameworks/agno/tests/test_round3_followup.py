"""Regressions for built-in tool privacy and terminal workflow stream closure."""

import asyncio
import json

import httpx
import pytest
from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.models.openai.responses import OpenAIResponses
from agno.workflow import Workflow
from agno.workflow.parallel import Parallel
from openai import AsyncOpenAI, OpenAI
from openinference.instrumentation import TraceConfig
from opentelemetry.trace import StatusCode
from traceai_agno import configure_agno_tracing


@pytest.mark.parametrize("hide", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_builtin_tool_configuration_is_not_exported(tracing, hide, asynchronous):
    provider, exporter = tracing
    secret = "SYNTHETIC_AGENT_MCP_AUTH_ROUND3"
    tool = {
        "type": "mcp",
        "server_label": "example",
        "server_url": "https://mcp.invalid",
        "authorization": secret,
    }
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp_synthetic",
                "object": "response",
                "created_at": 0,
                "model": "gpt-4o-mini",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "msg_synthetic",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": "Paris", "annotations": []}
                        ],
                    }
                ],
                "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as ahttp:
            client = OpenAI(
                api_key="synthetic-key",
                base_url="https://model.invalid/v1",
                http_client=http,
            )
            async_client = AsyncOpenAI(
                api_key="synthetic-key",
                base_url="https://model.invalid/v1",
                http_client=ahttp,
            )

            async def run():
                agent = Agent(
                    name="BuiltinTools",
                    model=OpenAIResponses(
                        id="gpt-4o-mini", client=client, async_client=async_client
                    ),
                    tools=[tool],
                    telemetry=False,
                )
                return await agent.arun("test") if asynchronous else agent.run("test")

            assert (await run()).content == "Paris"
            configure_agno_tracing(
                tracer_provider=provider,
                config=TraceConfig(hide_inputs=hide, hide_outputs=hide),
            )
            assert (await run()).content == "Paris"
    assert len(seen) == 2
    assert all(request["tools"][0]["authorization"] == secret for request in seen)
    assert tool["authorization"] == secret
    leaks = [
        (s.name, k)
        for s in exporter.get_finished_spans()
        for k, v in s.attributes.items()
        if secret in str(v)
    ]
    assert not leaks, leaks


@pytest.mark.parametrize("parallel", [False, True])
async def test_workflow_terminal_agent_stream_closes_successfully(tracing, parallel):
    provider, exporter = tracing

    def respond(request):
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Paris"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as ahttp:
            client = OpenAI(
                api_key="synthetic-key",
                base_url="https://model.invalid/v1",
                http_client=http,
            )
            async_client = AsyncOpenAI(
                api_key="synthetic-key",
                base_url="https://model.invalid/v1",
                http_client=ahttp,
            )

            def model():
                return OpenAIChat(
                    id="gpt-4o-mini", client=client, async_client=async_client
                )

            await _check_workflow(provider, exporter, model, parallel)


async def _check_workflow(provider, exporter, model, parallel):
    configure_agno_tracing(tracer_provider=provider)
    children = [Agent(name="Left", model=model(), telemetry=False)]
    if parallel:
        children.append(Agent(name="Right", model=model(), telemetry=False))
    steps = [Parallel(*children, name="Branches")] if parallel else children
    workflow = Workflow(name="Success", steps=steps, telemetry=False)
    chunks = [chunk async for chunk in workflow.arun("test", stream=True)]
    assert chunks[-1].event == "WorkflowCompleted"
    for _ in range(100):
        await asyncio.sleep(0)
    spans = exporter.get_finished_spans()
    errors = [
        (s.name, s.status.description)
        for s in spans
        if s.status.status_code == StatusCode.ERROR
    ]
    assert not errors, errors
    agent_spans = [s for s in spans if s.name in ("Left.arun", "Right.arun")]
    assert len(agent_spans) == (2 if parallel else 1)
    assert all(s.status.status_code == StatusCode.OK for s in agent_spans)
