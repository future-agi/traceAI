"""Independent installed-package probes; synthetic transport and credentials only."""

import asyncio
import json

import httpx
import pytest
from agno.agent import Agent
from agno.models.message import Message
from agno.models.openai.responses import OpenAIResponses
from agno.tools.function import Function, FunctionCall
from agno.workflow import Workflow
from openai import OpenAI
from openinference.instrumentation import TraceConfig
from opentelemetry import context, trace
from traceai_agno import configure_agno_tracing

from .test_recall import harness as harness


def summary(exporter):
    return [
        (
            s.name,
            s.status.status_code.name,
            s.parent.span_id if s.parent else None,
            dict(s.attributes),
        )
        for s in exporter.get_finished_spans()
    ]


@pytest.mark.parametrize("hide", [False, True])
def test_mcp_tool_credentials_redacted(harness, hide):
    provider, exporter, tracker, model, settings = harness
    secret = "SYNTHETIC_MCP_AUTH_533"
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
        client = OpenAI(
            api_key="synthetic-key",
            base_url="https://model.invalid/v1",
            http_client=http,
        )

        def run():
            m = OpenAIResponses(id="gpt-4o-mini", client=client)
            return m.invoke(
                messages=[Message(role="user", content="test")],
                assistant_message=Message(role="assistant"),
                tools=[
                    {
                        "type": "mcp",
                        "server_label": "example",
                        "server_url": "https://mcp.invalid",
                        "authorization": secret,
                    }
                ],
            )

        assert run().content == "Paris"
        configure_agno_tracing(
            tracer_provider=provider,
            config=TraceConfig(hide_inputs=hide, hide_outputs=hide),
        )
        assert run().content == "Paris"
    assert seen[-1]["tools"][0]["authorization"] == secret
    leaked = [
        (s.name, k, v)
        for s in exporter.get_finished_spans()
        for k, v in s.attributes.items()
        if secret in str(v)
    ]
    assert not leaked, leaked


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.asyncio
async def test_workflow_cancel_status(harness, asynchronous, streaming):
    provider, exporter, tracker, model, settings = harness
    settings["cancel"] = "synthetic-workflow-cancel-533"
    configure_agno_tracing(tracer_provider=provider)
    wf = Workflow(
        name="CancelledWorkflow",
        steps=[Agent(model=model(), telemetry=False)],
        telemetry=False,
    )
    kwargs = dict(run_id=settings["cancel"], stream=streaming)
    if asynchronous:
        result = (
            [r async for r in wf.arun("test", **kwargs)]
            if streaming
            else await wf.arun("test", **kwargs)
        )
    else:
        result = (
            list(wf.run("test", **kwargs)) if streaming else wf.run("test", **kwargs)
        )
    run_output = (
        (getattr(result[-1], "run_output", None) or result[-1]) if streaming else result
    )
    assert run_output.status.value == "CANCELLED", repr(run_output)
    span = next(
        s
        for s in exporter.get_finished_spans()
        if s.name.startswith("CancelledWorkflow.")
    )
    assert span.status.status_code == trace.StatusCode.ERROR, summary(exporter)


def test_sync_workflow_exception_restores_context(harness):
    provider, exporter, tracker, model, settings = harness

    def steps(workflow, execution_input, **kwargs):
        raise ValueError("synthetic workflow failure")

    with pytest.raises(Exception) as baseline:
        Workflow(name="BrokenWorkflow", steps=steps, telemetry=False).run("test")
    assert not trace.get_current_span().get_span_context().is_valid
    configure_agno_tracing(tracer_provider=provider)
    before = context.get_current()
    token = context.attach(before)
    try:
        wf = Workflow(name="BrokenWorkflow", steps=steps, telemetry=False)
        with pytest.raises(type(baseline.value), match="synthetic workflow failure"):
            wf.run("test")
        current = trace.get_current_span().get_span_context()
        Agent(name="Independent", model=model(), telemetry=False).run("test")
        independent = next(
            s for s in exporter.get_finished_spans() if s.name == "Independent.run"
        )
        assert not current.is_valid and independent.parent is None, summary(exporter)
    finally:
        context.detach(token)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_unstarted_tool_stream_close_finishes_span(harness, asynchronous):
    provider, exporter, tracker, model, settings = harness
    configure_agno_tracing(tracer_provider=provider)

    def pieces():
        yield "first"

    async def apieces():
        yield "first"

    fc = FunctionCall(
        function=Function.from_callable(apieces if asynchronous else pieces),
        arguments={},
    )
    response = await fc.aexecute() if asynchronous else fc.execute()
    if asynchronous:
        await response.result.aclose()
    else:
        response.result.close()
    assert len(tracker.started) == len(exporter.get_finished_spans()), summary(exporter)


@pytest.mark.asyncio
async def test_async_workflow_graph_parentage(harness):
    provider, exporter, tracker, model, settings = harness
    configure_agno_tracing(tracer_provider=provider)
    wf = Workflow(
        name="GraphWorkflow",
        steps=[Agent(name="Child", model=model(), telemetry=False)],
        telemetry=False,
    )
    assert (await wf.arun("test")).content == "Paris"
    spans = exporter.get_finished_spans()
    step = next(
        s for s in spans if s.name.endswith(".aexecute") and s.name.startswith("Child")
    )
    child = next(s for s in spans if s.name == "Child.arun")
    assert child.parent == step.context
    assert (
        child.attributes["graph.node.parent_id"] == step.attributes["graph.node.id"]
    ), summary(exporter)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_workflow_close_propagates(harness, asynchronous):
    provider, exporter, tracker, model, settings = harness
    configure_agno_tracing(tracer_provider=provider)
    wf = Workflow(
        name="EarlyWorkflow",
        steps=[Agent(name="Child", model=model(), telemetry=False)],
        telemetry=False,
    )
    if asynchronous:
        stream = wf.arun("test", stream=True)
        for _ in range(20):
            item = await anext(stream)
            if getattr(item, "content", None) == "Par":
                break
        await stream.aclose()
        for _ in range(50):
            await asyncio.sleep(0)
    else:
        stream = wf.run("test", stream=True)
        for item in stream:
            if getattr(item, "content", None) == "Par":
                break
        stream.close()
    assert len(tracker.started) == len(exporter.get_finished_spans()), {
        "started": [s.name for s in tracker.started],
        "finished": summary(exporter),
    }


@pytest.mark.parametrize("streaming", [False, True])
def test_async_workflow_setup_error_finishes_span(harness, streaming):
    from pydantic import BaseModel

    provider, exporter, tracker, model, settings = harness

    class Input(BaseModel):
        text: str

    def make_workflow():
        return Workflow(
            name="InvalidInput",
            input_schema=Input,
            steps=[Agent(model=model(), telemetry=False)],
            telemetry=False,
        )

    with pytest.raises(Exception) as baseline:
        make_workflow().arun(input={"wrong": "field"}, stream=streaming)
    configure_agno_tracing(tracer_provider=provider)
    before = context.get_current()
    with pytest.raises(type(baseline.value)):
        make_workflow().arun(input={"wrong": "field"}, stream=streaming)
    assert context.get_current() == before
    assert len(tracker.started) == len(exporter.get_finished_spans()) == 1, {
        "started": [s.name for s in tracker.started],
        "finished": summary(exporter),
    }
    assert exporter.get_finished_spans()[0].status.status_code == trace.StatusCode.ERROR
