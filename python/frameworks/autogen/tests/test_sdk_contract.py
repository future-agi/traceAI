"""Offline contracts at the real AgentChat and pyautogen call boundaries.

Only the public model-client extension point is local. No instrumented SDK
method or tracing dependency is replaced, and all spans use an OTel exporter.
"""

import asyncio
import inspect

import pytest
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import Response, TaskResult
from autogen_agentchat.messages import TextMessage
from autogen_agentchat.teams import RoundRobinGroupChat, SelectorGroupChat
from autogen_core import CancellationToken
from autogen_core.models import ChatCompletionClient, CreateResult, RequestUsage
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode


class LocalModel(ChatCompletionClient):
    """Deterministic public adapter; AssistantAgent itself runs unchanged."""

    model = "offline-model"

    def __init__(self, error=None, gate=None):
        self.error = error
        self.gate = gate
        self.entered = asyncio.Event()

    async def create(self, messages, **kwargs):
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        return CreateResult(
            content="offline reply", finish_reason="stop", cached=False,
            usage=RequestUsage(prompt_tokens=7, completion_tokens=3),
        )

    async def create_stream(self, messages, **kwargs):
        yield "offline "
        yield "reply"
        yield await self.create(messages, **kwargs)

    async def close(self):
        pass

    def actual_usage(self):
        return RequestUsage(prompt_tokens=7, completion_tokens=3)

    total_usage = actual_usage

    def count_tokens(self, messages, **kwargs):
        return 7

    def remaining_tokens(self, messages, **kwargs):
        return 100

    @property
    def model_info(self):
        return {"vision": False, "function_calling": False, "json_output": False,
                "family": "unknown", "structured_output": False}

    @property
    def capabilities(self):
        return self.model_info


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Unit tests reload this module; resolve the live singleton here.
    from traceai_autogen import AutogenInstrumentor
    instrumentor = AutogenInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    yield exporter, provider, instrumentor
    instrumentor.uninstrument()
    provider.shutdown()


def agent():
    return AssistantAgent("assistant", model_client=LocalModel())


def team():
    return RoundRobinGroupChat([agent()], max_turns=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_team_preserves_real_sdk_result_and_usage(spans, streaming):
    exporter, provider, _ = spans
    outer = provider.get_tracer(__name__)
    with outer.start_as_current_span("caller") as parent:
        chat = team()
        if streaming:
            assert inspect.isasyncgenfunction(chat.run_stream)
            result = None
            async for item in chat.run_stream(task="hello", cancellation_token=CancellationToken()):
                # Pausing a generator must not leave its span current in caller code.
                assert trace.get_current_span() is parent
                if isinstance(item, TaskResult):
                    result = item
        else:
            result = await chat.run(task="hello", cancellation_token=CancellationToken())
        assert isinstance(result, TaskResult)
        assert [message.content for message in result.messages] == ["hello", "offline reply"]
        assert result.stop_reason
        assert trace.get_current_span() is parent
    finished = exporter.get_finished_spans()
    runs = [span for span in finished if span.name.startswith("autogen.team.")]
    assert len(runs) == (1 if streaming else 2)  # run delegates to run_stream
    by_name = {span.name: span for span in runs}
    root = by_name["autogen.team.run_stream.RoundRobinGroupChat" if streaming else "autogen.team.run.RoundRobinGroupChat"]
    assert root.parent.span_id == parent.get_span_context().span_id
    for span in runs:
        assert span.status.status_code == StatusCode.OK
        assert span.attributes["autogen.task.message_count"] == 2
        assert span.attributes["gen_ai.usage.input_tokens"] == 7
        assert span.attributes["gen_ai.usage.output_tokens"] == 3
        assert span.attributes["gen_ai.usage.total_tokens"] == 10
        assert span.end_time is not None
    stream_span = by_name["autogen.team.run_stream.RoundRobinGroupChat"]
    if not streaming:
        assert stream_span.parent.span_id == root.context.span_id
    agents = [span for span in finished if span.name.startswith("autogen.agent.")]
    assert len(agents) == 1  # team calls concrete AssistantAgent.on_messages_stream
    assert agents[0].attributes["autogen.output.content"] == "offline reply"
    assert agents[0].attributes["gen_ai.request.model"] == "offline-model"
    # AgentChat's runtime may insert its own spans between the team and agent.
    ancestors = {span.context.span_id: span for span in finished}
    current = agents[0]
    while current.context.span_id != stream_span.context.span_id:
        current = ancestors[current.parent.span_id]


@pytest.mark.asyncio
async def test_team_taskless_continuation_and_keyword_forwarding(spans):
    chat = team()
    first = await chat.run(task="hello")
    second = await chat.run()
    streamed = [item async for item in chat.run_stream()]
    assert first.messages[-1].content == second.messages[-1].content == "offline reply"
    assert isinstance(streamed[-1], TaskResult)
    assert streamed[-1].messages[-1].content == "offline reply"
    assert len([s for s in spans[0].get_finished_spans() if s.name.startswith("autogen.team.")]) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_concrete_assistant_hooks(spans, streaming):
    assistant = agent()
    token = CancellationToken()
    messages = [TextMessage(content="hello", source="user")]
    if streaming:
        assert inspect.isasyncgenfunction(assistant.on_messages_stream)
        items = [item async for item in assistant.on_messages_stream(messages=messages, cancellation_token=token)]
        response = items[-1]
    else:
        response = await assistant.on_messages(messages=messages, cancellation_token=token)
    assert isinstance(response, Response)
    assert response.chat_message.content == "offline reply"
    finished = spans[0].get_finished_spans()
    assert len(finished) == (1 if streaming else 2)
    for span in finished:
        assert span.attributes["autogen.agent.name"] == "assistant"
        assert span.attributes["autogen.output.content"] == "offline reply"
        assert span.status.status_code == StatusCode.OK
    if not streaming:
        child, parent = finished
        assert parent.name == "autogen.agent.on_messages.assistant"
        assert child.parent.span_id == parent.context.span_id


@pytest.mark.asyncio
async def test_team_stream_early_close_ends_span_without_context_leak(spans):
    exporter, provider, _ = spans
    with provider.get_tracer(__name__).start_as_current_span("caller") as caller:
        stream = team().run_stream(task="hello")
        first = await anext(stream)
        assert first.content == "hello"
        assert trace.get_current_span() is caller
        await stream.aclose()
        assert trace.get_current_span() is caller
    runs = [s for s in exporter.get_finished_spans() if s.name.startswith("autogen.team.")]
    assert len(runs) == 1
    assert runs[0].attributes["autogen.stream.closed_early"] is True
    assert runs[0].end_time is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_real_assistant_exception_identity_and_error_spans(spans, streaming):
    failure = ValueError("offline failure")
    assistant = AssistantAgent("assistant", model_client=LocalModel(error=failure))
    with pytest.raises(ValueError) as caught:
        if streaming:
            async for _ in assistant.on_messages_stream([], CancellationToken()):
                pass
        else:
            await assistant.on_messages([], CancellationToken())
    assert caught.value is failure
    finished = spans[0].get_finished_spans()
    assert len(finished) == (1 if streaming else 2)
    for span in finished:
        assert span.status.status_code == StatusCode.ERROR
        assert span.attributes["autogen.error.type"] == "ValueError"
        assert len([event for event in span.events if event.name == "exception"]) == 1
        assert span.end_time is not None


@pytest.mark.asyncio
async def test_real_assistant_cancel_preserves_cancellation_and_ends_spans(spans):
    model = LocalModel(gate=asyncio.Event())
    assistant = AssistantAgent("assistant", model_client=model)
    task = asyncio.create_task(assistant.on_messages([], CancellationToken()))
    await asyncio.wait_for(model.entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    finished = spans[0].get_finished_spans()
    assert len(finished) == 2
    assert all(s.status.status_code == StatusCode.ERROR and s.end_time for s in finished)
    assert all(s.attributes["autogen.error.type"] == "CancelledError" for s in finished)


@pytest.mark.asyncio
async def test_real_assistant_streams_model_chunks(spans):
    if "model_client_stream" not in inspect.signature(AssistantAgent).parameters:
        # This option was added after 0.4.0. Instrumentation must not make an
        # unsupported upstream constructor option appear to work.
        with pytest.raises(TypeError, match="model_client_stream"):
            AssistantAgent("assistant", model_client=LocalModel(), model_client_stream=True)
        assert spans[0].get_finished_spans() == ()
        return
    assistant = AssistantAgent("assistant", model_client=LocalModel(), model_client_stream=True)
    items = [item async for item in assistant.on_messages_stream([], CancellationToken())]
    assert [item.content for item in items[:-1]] == ["offline ", "reply"]
    assert isinstance(items[-1], Response)
    assert items[-1].chat_message.content == "offline reply"
    span, = spans[0].get_finished_spans()
    assert span.status.status_code == StatusCode.OK
    assert span.attributes["autogen.output.content"] == "offline reply"
    assert span.attributes["autogen.stream.message_count"] == 3


def test_uninstrument_restores_exact_defining_methods_and_reinstrument(spans):
    _, provider, instrumentor = spans
    originals = dict(instrumentor._patches)
    # These teams inherit one implementation; it must be patched exactly once.
    assert RoundRobinGroupChat.run is SelectorGroupChat.run
    assert "run" not in vars(RoundRobinGroupChat)
    assert AssistantAgent.on_messages is not originals[AssistantAgent, "on_messages"]
    from traceai_autogen import AutogenInstrumentor
    assert AutogenInstrumentor() is instrumentor
    instrumentor.uninstrument()
    for (cls, method), original in originals.items():
        assert vars(cls)[method] is original
    instrumentor.instrument(tracer_provider=provider)
    assert len(instrumentor._patches) == len(originals)
    instrumentor.uninstrument()
    for (cls, method), original in originals.items():
        assert vars(cls)[method] is original

