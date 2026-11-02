"""Run the real framework through sync/async/stream/tool/team/workflow paths."""

import json

import httpx
import pytest
from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.team import Team
from agno.workflow import Workflow
from openai import AsyncOpenAI, OpenAI
from opentelemetry.trace import StatusCode
from traceai_agno import configure_agno_tracing


def model_response(request):
    assert request.url.host == "model.invalid"
    body = json.loads(request.content)
    if body["model"] == "error":
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "synthetic failure",
                    "type": "invalid_request_error",
                    "code": "invalid_request",
                }
            },
        )
    response = {"id": "chatcmpl-test", "created": 0, "model": body["model"]}
    if body.get("stream"):
        chunks = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Pa"},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {"index": 0, "delta": {"content": "ris"}, "finish_reason": None}
                ]
            },
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 2,
                    "total_tokens": 14,
                },
            },
        ]
        payload = "".join(
            "data: "
            + json.dumps({**response, "object": "chat.completion.chunk", **chunk})
            + "\n\n"
            for chunk in chunks
        )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=(payload + "data: [DONE]\n\n").encode(),
        )
    message = {"role": "assistant", "content": "Paris"}
    finish = "stop"
    if body.get("tools") and not any(m["role"] == "tool" for m in body["messages"]):
        name = body["tools"][0]["function"]["name"]
        arguments = {"city": "Paris"}
        if name == "delegate_task_to_member":
            arguments = {
                "member_id": "researcher",
                "task": "Find the capital of France",
            }
        elif name == "delegate_task_to_members":
            arguments = {"task": "Find the capital of France"}
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-test",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }
        finish = "tool_calls"
    return httpx.Response(
        200,
        json={
            **response,
            "object": "chat.completion",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14},
        },
    )


@pytest.fixture
async def model():

    with httpx.Client(transport=httpx.MockTransport(model_response)) as sync_http:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(model_response)
        ) as async_http:
            client = OpenAI(
                api_key="synthetic-test-key",
                base_url="https://model.invalid/v1",
                http_client=sync_http,
            )
            async_client = AsyncOpenAI(
                api_key="synthetic-test-key",
                base_url="https://model.invalid/v1",
                http_client=async_http,
            )
            yield lambda name="gpt-4o-mini": OpenAIChat(
                id=name, client=client, async_client=async_client
            )


def assert_tree(exporter, kinds):
    spans = exporter.get_finished_spans()
    assert spans
    assert len({s.context.span_id for s in spans}) == len(spans)
    assert len({s.context.trace_id for s in spans}) == 1
    roots = [s for s in spans if not s.parent]
    assert len(roots) == 1
    ids = {s.context.span_id for s in spans}
    assert all(s.parent.span_id in ids for s in spans if s.parent)
    actual = [s.attributes.get("openinference.span.kind") for s in spans]
    for kind, count in kinds.items():
        assert actual.count(kind) == count, [
            (s.name, s.attributes.get("openinference.span.kind")) for s in spans
        ]
    return spans


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
async def test_agent_lifecycle(tracing, model, asynchronous, streaming):
    provider, exporter = tracing
    configure_agno_tracing(tracer_provider=provider)
    agent = Agent(model=model(), telemetry=False)
    if asynchronous:
        if streaming:
            chunks = [
                chunk async for chunk in agent.arun("Capital of France?", stream=True)
            ]
            assert "".join(c.content or "" for c in chunks) == "Paris"
        else:
            assert (await agent.arun("Capital of France?")).content == "Paris"
    elif streaming:
        assert (
            "".join(
                c.content or "" for c in agent.run("Capital of France?", stream=True)
            )
            == "Paris"
        )
    else:
        assert agent.run("Capital of France?").content == "Paris"
    spans = assert_tree(exporter, {"AGENT": 1, "LLM": 1})
    llm = next(s for s in spans if s.attributes.get("openinference.span.kind") == "LLM")
    assert llm.attributes["llm.model_name"] == "gpt-4o-mini"
    assert llm.attributes["llm.token_count.prompt"] == 12
    assert llm.attributes["llm.token_count.completion"] == 2


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_real_tool_execution(tracing, model, asynchronous):
    provider, exporter = tracing
    configure_agno_tracing(tracer_provider=provider)
    calls = []

    def weather(city: str) -> str:
        """Return the weather for a city."""
        calls.append(city)
        return "Sunny"

    agent = Agent(model=model(), tools=[weather], telemetry=False)
    response = (
        await agent.arun("Weather in Paris?")
        if asynchronous
        else agent.run("Weather in Paris?")
    )
    assert response.content == "Paris"
    assert calls == ["Paris"]
    assert_tree(exporter, {"AGENT": 1, "LLM": 2, "TOOL": 1})


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_team_delegates_to_real_member(tracing, model, asynchronous):
    provider, exporter = tracing
    configure_agno_tracing(tracer_provider=provider)
    member = Agent(id="researcher", name="Researcher", model=model(), telemetry=False)
    team = Team(name="Research team", members=[member], model=model(), telemetry=False)
    result = (
        await team.arun("Capital of France?")
        if asynchronous
        else team.run("Capital of France?")
    )
    assert result.content == "Paris"
    assert_tree(exporter, {"AGENT": 2, "LLM": 3, "TOOL": 1})


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_workflow_runs_real_agent_step(tracing, model, asynchronous):
    provider, exporter = tracing
    configure_agno_tracing(tracer_provider=provider)
    workflow = Workflow(
        name="Research",
        steps=[Agent(name="Researcher", model=model(), telemetry=False)],
        telemetry=False,
    )
    result = (
        await workflow.arun("Capital of France?")
        if asynchronous
        else workflow.run("Capital of France?")
    )
    assert result.content == "Paris"
    assert_tree(exporter, {"AGENT": 1, "LLM": 1, "CHAIN": 2})


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_model_error_behavior_is_preserved(tracing, model, asynchronous):
    provider, exporter = tracing

    async def run():
        agent = Agent(model=model("error"), telemetry=False)
        # Agno versions differ: some raise, others return a failed RunOutput.
        from agno.exceptions import ModelProviderError

        try:
            result = (
                await agent.arun("Capital of France?")
                if asynchronous
                else agent.run("Capital of France?")
            )
        except ModelProviderError as exc:
            return type(exc), str(exc)
        return result.status, result.content

    baseline = await run()
    assert "synthetic failure" in str(baseline)
    configure_agno_tracing(tracer_provider=provider)
    assert await run() == baseline
    spans = assert_tree(exporter, {"AGENT": 1, "LLM": 1})
    llm = next(s for s in spans if s.attributes.get("openinference.span.kind") == "LLM")
    assert llm.status.status_code == StatusCode.ERROR
