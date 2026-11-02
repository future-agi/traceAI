"""Boundary coverage for provider privacy and explicit coroutine ownership."""

import asyncio
import gc
import json
from dataclasses import dataclass
from inspect import getcoroutinestate

import httpx
import pytest
from agno.models.message import Message
from agno.models.minimax import MiniMax
from agno.models.openai import OpenAIChat
from agno.workflow import Workflow
from agno.workflow.parallel import Parallel
from agno.workflow.step import Step
from agno.workflow.types import StepInput, StepOutput
from google.genai import types
from openai import AsyncOpenAI, OpenAI
from opentelemetry import context, trace
from traceai_agno import configure_agno_tracing
from traceai_agno._wrappers._model_wrapper import _filter_sensitive_params
from traceai_agno._wrappers._stream import EagerSpanCoroutine, finish_span
from traceai_agno._wrappers._workflow_wrapper import _set_step_status

from .test_recall import harness as harness


def test_typed_redaction_is_structured_nonmutating_and_fail_closed():
    secret = "SYNTHETIC_TYPED_AUTH_BOUNDARY"

    class Opaque:
        def __str__(self):
            pytest.fail("opaque configuration must never be stringified")

        __repr__ = __str__

    @dataclass
    class Options:
        authorization: str
        max_tokens: int
        opaque: object

    config = types.GenerateContentConfig(
        temperature=0.25,
        http_options=types.HttpOptions(headers={"X-Custom-Auth": secret}),
    )
    options = Options(secret, 42, Opaque())
    cycle = {}
    cycle["child"] = cycle
    params = {"config": config, "extra": [options], "cycle": cycle}
    result = _filter_sensitive_params(params)
    assert secret not in json.dumps(result)
    assert result["config"]["temperature"] == 0.25
    assert result["config"]["http_options"]["headers"] == "[REDACTED]"
    assert result["extra"] == [
        {"authorization": "[REDACTED]", "max_tokens": 42, "opaque": "[REDACTED]"}
    ]
    assert result["cycle"]["child"] == "[REDACTED]"
    assert config.http_options.headers == {"X-Custom-Auth": secret}
    assert options.authorization == secret
    assert cycle["child"] is cycle


@pytest.mark.parametrize(
    "state,expected",
    [
        ("success", trace.StatusCode.OK),
        ("failed", trace.StatusCode.ERROR),
        ("paused", trace.StatusCode.UNSET),
        ("missing", trace.StatusCode.UNSET),
    ],
)
def test_step_status_respects_terminal_result(harness, state, expected):
    provider, exporter, tracker, model, settings = harness
    output = (
        None
        if state == "missing"
        else StepOutput(
            content="result",
            success=state != "failed",
            is_paused=state == "paused",
        )
    )
    with provider.get_tracer(__name__).start_as_current_span("step") as span:
        _set_step_status(span, output)
    assert exporter.get_finished_spans()[0].status.status_code == expected


@pytest.mark.parametrize("kind", ["workflow", "step", "parallel"])
@pytest.mark.parametrize(
    "exit_mode", ["close", "cancel", "success", "failure", "running_cancel"]
)
@pytest.mark.asyncio
async def test_async_dispatcher_ownership(harness, kind, exit_mode, recwarn):
    provider, exporter, tracker, model, settings = harness
    ran = []
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def execute(step_input):
        ran.append(True)
        entered.set()
        if exit_mode == "running_cancel":
            await blocked.wait()
        if exit_mode == "failure":
            raise ValueError("synthetic executor failure")
        return StepOutput(content="Paris")

    configure_agno_tracing(tracer_provider=provider)
    before = context.get_current()
    step = Step(name="Child", executor=execute, max_retries=0)
    if kind == "workflow":
        coroutine = Workflow(name="Owned", steps=[step], telemetry=False).arun("test")
    elif kind == "step":
        coroutine = step.aexecute(StepInput(input="test"))
    else:
        coroutine = Parallel(step, name="Owned").aexecute(StepInput(input="test"))
    assert context.get_current() == before
    if exit_mode == "close":
        coroutine.close()
        coroutine.close()
    else:
        task = asyncio.create_task(coroutine)
        if exit_mode == "cancel":
            task.cancel()
        elif exit_mode == "running_cancel":
            await entered.wait()
            task.cancel()
        if exit_mode in ("cancel", "running_cancel"):
            with pytest.raises(asyncio.CancelledError):
                await task
        elif exit_mode == "failure":
            # Some Agno containers return a failed result; neither is success.
            try:
                await task
            except (ValueError, RuntimeError):
                pass
        else:
            await task
    gc.collect()
    for _ in range(5):
        await asyncio.sleep(0)
    assert bool(ran) == (exit_mode not in ("close", "cancel"))
    assert context.get_current() == before
    assert tracker.started
    spans = exporter.get_finished_spans()
    assert len(tracker.started) == len(spans)
    assert len({s.context.span_id for s in spans}) == len(spans)
    if exit_mode in ("close", "cancel", "running_cancel"):
        assert all(s.status.status_code != trace.StatusCode.OK for s in spans)
    assert not [w for w in recwarn if "never awaited" in str(w.message)]


@pytest.mark.parametrize("mode", ["throw_instance", "throw_type", "close"])
def test_coroutine_owner_closes_unstarted_sdk_source(harness, mode):
    provider, exporter, tracker, model, settings = harness
    ran = []

    async def source():
        ran.append(True)
        return "done"

    sdk = source()
    span = provider.get_tracer(__name__).start_span("owned")

    async def finishing():
        with finish_span(span):
            return await sdk

    coroutine = finishing()
    owner = EagerSpanCoroutine(coroutine, sdk, span)
    if mode == "close":
        owner.close()
    else:
        error = (
            ValueError("synthetic pre-start error")
            if mode == "throw_instance"
            else ValueError
        )
        with pytest.raises(ValueError):
            owner.throw(error)
    owner.close()
    assert not ran
    assert getcoroutinestate(sdk) == getcoroutinestate(coroutine) == "CORO_CLOSED"
    assert len(exporter.get_finished_spans()) == 1
    assert exporter.get_finished_spans()[0].status.status_code == trace.StatusCode.ERROR


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("positional", [False, True])
@pytest.mark.asyncio
async def test_interleaved_model_streams_remain_independent(
    harness, asynchronous, positional
):
    provider, exporter, tracker, model, settings = harness
    template = model()
    minimax = MiniMax(
        id="MiniMax-M2",
        api_key="synthetic-key",
        client=template.client,
        async_client=template.async_client,
    )
    configure_agno_tracing(tracer_provider=provider)
    before = context.get_current()

    def arguments():
        return dict(
            messages=[Message(role="user", content="test")],
            assistant_message=Message(role="assistant"),
        )

    def stream():
        method = minimax.ainvoke_stream if asynchronous else minimax.invoke_stream
        values = arguments()
        return (
            method(values["messages"], values["assistant_message"])
            if positional
            else method(**values)
        )

    if asynchronous:
        first = stream()
        second = stream()
        a = await anext(first)
        assert context.get_current() == before
        b = await anext(second)
        assert context.get_current() == before
        output_a = a.content + "".join([x.content or "" async for x in first])
        output_b = b.content + "".join([x.content or "" async for x in second])
    else:
        first = stream()
        second = stream()
        a = next(first)
        assert context.get_current() == before
        b = next(second)
        assert context.get_current() == before
        output_a = a.content + "".join(x.content or "" for x in first)
        output_b = b.content + "".join(x.content or "" for x in second)
    assert output_a == output_b == "Paris"
    assert settings["requests"] == 2
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert all("test" in s.attributes["input.value"] for s in spans)
    assert spans[0].context.trace_id != spans[1].context.trace_id
    assert all(
        s.parent is None and s.status.status_code == trace.StatusCode.OK for s in spans
    )
    assert context.get_current() == before


@pytest.mark.parametrize("mode", ["sync", "async", "child_task"])
@pytest.mark.asyncio
async def test_reentrant_requests_on_same_model_are_not_suppressed(harness, mode):
    provider, exporter, tracker, model, settings = harness
    requests = []

    def arguments():
        return dict(
            messages=[Message(role="user", content="test")],
            assistant_message=Message(role="assistant"),
        )

    def response():
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "created": 0,
                "model": "gpt-4o-mini",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Paris"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    def sync_handler(request):
        requests.append(request)
        if len(requests) == 1:
            assert m.invoke(**arguments()).content == "Paris"
        return response()

    async def async_handler(request):
        requests.append(request)
        if len(requests) == 1:
            inner = m.ainvoke(**arguments())
            result = await (
                asyncio.create_task(inner) if mode == "child_task" else inner
            )
            assert result.content == "Paris"
        return response()

    with httpx.Client(transport=httpx.MockTransport(sync_handler)) as http:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(async_handler)
        ) as ahttp:
            m = OpenAIChat(
                id="gpt-4o-mini",
                client=OpenAI(
                    api_key="synthetic",
                    base_url="https://model.invalid/v1",
                    http_client=http,
                ),
                async_client=AsyncOpenAI(
                    api_key="synthetic",
                    base_url="https://model.invalid/v1",
                    http_client=ahttp,
                ),
            )
            configure_agno_tracing(tracer_provider=provider)
            result = (
                m.invoke(**arguments())
                if mode == "sync"
                else await m.ainvoke(**arguments())
            )
            assert result.content == "Paris"
    assert len(requests) == 2
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert all(s.attributes["openinference.span.kind"] == "LLM" for s in spans)
    assert spans[0].parent.span_id == spans[1].context.span_id
