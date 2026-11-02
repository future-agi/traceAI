"""Round-two probes: installed frameworks, synthetic transports, no network."""

import asyncio
import json

import httpx
import pytest
from agno.agent import Agent
from agno.models.message import Message
from agno.models.minimax import MiniMax
from agno.models.openai.responses import OpenAIResponses
from agno.workflow.step import Step
from agno.workflow.types import StepInput, StepOutput
from openai import AsyncOpenAI, OpenAI
from openinference.instrumentation import TraceConfig
from opentelemetry import trace
from traceai_agno import configure_agno_tracing

from .test_recall import harness as harness
from .test_recall import span_summary


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.asyncio
async def test_failed_step_status(harness, asynchronous, streaming):
    provider, exporter, tracker, model, settings = harness

    def fail(step_input):
        raise ValueError("synthetic step failure")

    async def afail(step_input):
        raise ValueError("synthetic step failure")

    def make_step():
        return Step(
            name="Failure",
            executor=afail if asynchronous else fail,
            skip_on_failure=True,
            max_retries=0,
        )

    async def run(step):
        if asynchronous:
            return (
                [item async for item in step.aexecute_stream(StepInput(input="test"))]
                if streaming
                else await step.aexecute(StepInput(input="test"))
            )
        return (
            list(step.execute_stream(StepInput(input="test")))
            if streaming
            else step.execute(StepInput(input="test"))
        )

    baseline = await run(make_step())
    baseline_output = (
        next(x for x in reversed(baseline) if isinstance(x, StepOutput))
        if streaming
        else baseline
    )
    assert baseline_output.success is False
    configure_agno_tracing(tracer_provider=provider)
    result = await run(make_step())
    output = (
        next(x for x in reversed(result) if isinstance(x, StepOutput))
        if streaming
        else result
    )
    assert output.success is False and output.error == baseline_output.error
    spans = exporter.get_finished_spans()
    assert len(spans) == len(tracker.started) == 1
    assert spans[0].status.status_code == trace.StatusCode.ERROR, span_summary(exporter)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_minimax_single_llm_span(harness, asynchronous):
    provider, exporter, tracker, model, settings = harness
    template = model()

    def make_agent():
        return Agent(
            model=MiniMax(
                id="MiniMax-M2",
                api_key="synthetic-minimax-key",
                client=template.client,
                async_client=template.async_client,
            ),
            telemetry=False,
        )

    async def run():
        agent = make_agent()
        if asynchronous:
            return "".join(
                [x.content or "" async for x in agent.arun("test", stream=True)]
            )
        return "".join(x.content or "" for x in agent.run("test", stream=True))

    baseline = await run()
    assert baseline == "Paris" and settings["requests"] == 1
    configure_agno_tracing(tracer_provider=provider)
    assert await run() == baseline
    assert (
        settings["requests"] == 2
    )  # Exactly one model request in each run, not a retry.
    spans = [
        s
        for s in exporter.get_finished_spans()
        if s.attributes.get("openinference.span.kind") == "LLM"
    ]
    assert len(spans) == 1, span_summary(exporter)
    assert "test" in spans[0].attributes["input.value"]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("hide", [False, True])
@pytest.mark.parametrize("environment", [False, True])
@pytest.mark.asyncio
async def test_responses_instructions_hidden(
    harness, asynchronous, hide, environment, monkeypatch
):
    provider, exporter, tracker, model, settings = harness
    private = "SYNTHETIC_PRIVATE_SYSTEM_INSTRUCTIONS_ROUND2"
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
            sync = OpenAI(
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
                m = OpenAIResponses(
                    id="gpt-4o-mini",
                    client=sync,
                    async_client=async_client,
                    request_params={"instructions": private},
                )
                kwargs = dict(
                    messages=[Message(role="user", content="test")],
                    assistant_message=Message(role="assistant"),
                )
                return await m.ainvoke(**kwargs) if asynchronous else m.invoke(**kwargs)

            assert (await run()).content == "Paris"
            if environment:
                monkeypatch.setenv("OPENINFERENCE_HIDE_INPUTS", str(hide).lower())
                monkeypatch.setenv("OPENINFERENCE_HIDE_OUTPUTS", str(hide).lower())
                config = None
            else:
                config = TraceConfig(hide_inputs=hide, hide_outputs=hide)
            configure_agno_tracing(tracer_provider=provider, config=config)
            assert (await run()).content == "Paris"
    assert len(seen) == 2 and all(body["instructions"] == private for body in seen)
    leaked = [
        (s.name, k, v)
        for s in exporter.get_finished_spans()
        for k, v in s.attributes.items()
        if private in str(v)
    ]
    assert bool(leaked) is not hide, leaked
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["llm.model_name"] == "gpt-4o-mini"
    assert ("llm.invocation_parameters" in spans[0].attributes) is not hide


@pytest.mark.parametrize("kind", ["workflow", "step", "parallel"])
@pytest.mark.asyncio
async def test_cancel_before_first_await_finishes_span(harness, kind):
    from agno.workflow import Workflow
    from agno.workflow.parallel import Parallel
    from opentelemetry import context

    provider, exporter, tracker, model, settings = harness
    ran = []

    async def execute(step_input):
        ran.append(True)
        return StepOutput(content="Paris")

    def make_coroutine():
        step = Step(name="Child", executor=execute)
        if kind == "workflow":
            return Workflow(name="Unstarted", steps=[step], telemetry=False).arun(
                "test"
            )
        if kind == "step":
            return step.aexecute(StepInput(input="test"))
        return Parallel(step, name="Unstarted").aexecute(StepInput(input="test"))

    before = context.get_current()
    baseline = asyncio.create_task(make_coroutine())
    baseline.cancel()
    with pytest.raises(asyncio.CancelledError):
        await baseline
    assert not ran
    configure_agno_tracing(tracer_provider=provider)
    task = asyncio.create_task(make_coroutine())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(50):
        await asyncio.sleep(0)
    assert not ran and context.get_current() == before
    assert len(tracker.started) == len(exporter.get_finished_spans()), {
        "started": [s.name for s in tracker.started],
        "finished": span_summary(exporter),
    }


@pytest.mark.parametrize("hide", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_gemini_typed_auth_headers_are_redacted(harness, hide, asynchronous):
    from agno.models.google import Gemini
    from google import genai
    from google.genai import types

    provider, exporter, tracker, model, settings = harness
    secret = "SYNTHETIC_GEMINI_AUTH_ROUND2"
    seen = []

    def respond(request):
        assert request.url.host == "model.invalid"
        seen.append(dict(request.headers))
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "Paris"}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 2,
                    "candidatesTokenCount": 1,
                    "totalTokenCount": 3,
                },
                "modelVersion": "gemini-2.5-flash",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as ahttp:
            client = genai.Client(
                api_key="synthetic-google-key",
                vertexai=False,
                http_options=types.HttpOptions(
                    base_url="https://model.invalid",
                    httpx_client=http,
                    httpx_async_client=ahttp,
                ),
            )
            try:

                async def run():
                    m = Gemini(
                        id="gemini-2.5-flash",
                        client=client,
                        generation_config=types.GenerateContentConfig(
                            http_options=types.HttpOptions(
                                headers={"Authorization": secret}
                            )
                        ),
                    )
                    kwargs = dict(
                        messages=[Message(role="user", content="test")],
                        assistant_message=Message(role="assistant"),
                    )
                    return (
                        await m.ainvoke(**kwargs)
                        if asynchronous
                        else m.invoke(**kwargs)
                    )

                assert (await run()).content == "Paris"
                configure_agno_tracing(
                    tracer_provider=provider,
                    config=TraceConfig(hide_inputs=hide, hide_outputs=hide),
                )
                assert (await run()).content == "Paris"
            finally:
                client.close()
                await client.aio.aclose()
    assert len(seen) == 2 and all(
        headers["authorization"] == secret for headers in seen
    )
    leaked = [
        (s.name, k, v)
        for s in exporter.get_finished_spans()
        for k, v in s.attributes.items()
        if secret in str(v)
    ]
    assert not leaked, leaked
