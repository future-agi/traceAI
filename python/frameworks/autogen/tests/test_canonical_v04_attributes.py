"""T5-PY-005: fake AgentChat calls export collector-readable attributes."""

import asyncio
import sys
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fi_instrumentation import FITracer, TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_autogen._v04_wrapper import (
    _record_team_result,
    _team_attributes,
    wrap_agent_on_messages,
    wrap_agent_on_messages_stream,
    wrap_team_run,
    wrap_tool_execution,
)


@dataclass
class Message:
    content: str
    source: str


@dataclass
class Response:
    chat_message: Message


@dataclass
class TaskResult:
    messages: list
    stop_reason: str = "complete"


@pytest.fixture
def recording():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider.get_tracer(__name__), exporter
    provider.shutdown()


POLICIES = (
    "hide_inputs", "hide_outputs", "hide_input_messages",
    "hide_output_messages", "hide_input_text", "hide_output_text",
)


@pytest.mark.parametrize("team", [False, True], ids=["agent", "team"])
@pytest.mark.parametrize("streaming", [False, True], ids=["call", "stream"])
@pytest.mark.parametrize("policy", [None, *POLICIES])
def test_agentchat_exports_canonical_content_and_kind(recording, monkeypatch, team, streaming, policy):
    # Only the wrapped library is fake; real FITracer/OTel exercise export and
    # privacy. Streaming uses the same Response/TaskResult boundary as AgentChat.
    monkeypatch.setitem(sys.modules, "autogen_agentchat.base", SimpleNamespace(
        Response=Response, TaskResult=TaskResult,
    ))
    raw_tracer, exporter = recording
    config = TraceConfig(**{flag: flag == policy for flag in POLICIES})
    tracer = FITracer(raw_tracer, config=config)
    instance = SimpleNamespace(name="assistant")
    messages = [Message("question", "user")]
    answer = Message("answer", "assistant")
    terminal = TaskResult([*messages, answer]) if team else Response(answer)
    arguments = {"task": "question"} if team else {"messages": messages}
    calls = []
    cleanup = []

    async def original(self, **kwargs):
        calls.append((self, kwargs))
        return terminal

    async def original_stream(self, **kwargs):
        calls.append((self, kwargs))
        yield answer
        yield terminal
        cleanup.append(True)

    if team:
        wrapped = wrap_team_run(
            original_stream if streaming else original, tracer,
            "run_stream" if streaming else "run", config,
        )
    else:
        factory = wrap_agent_on_messages_stream if streaming else wrap_agent_on_messages
        wrapped = factory(original_stream if streaming else original, tracer, config)

    async def exercise():
        if not streaming:
            assert await wrapped(instance, **arguments) is terminal
            return
        stream = wrapped(instance, **arguments)
        assert await anext(stream) is answer
        assert await anext(stream) is terminal
        assert cleanup == []  # Recording must not prefetch post-result work.
        assert len(exporter.get_finished_spans()) == 1
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert cleanup == [True]
        await stream.aclose()

    asyncio.run(exercise())
    assert calls == [(instance, arguments)]
    if not team:
        assert calls[0][1]["messages"] is messages
    assert messages[0].content == "question"
    assert answer.content == "answer"
    span, = exporter.get_finished_spans()
    attributes = span.attributes
    # These exact keys feed resolveObservationType/contentValue in the collector;
    # autogen.* aliases alone do not populate its observation/content columns.
    assert attributes["gen_ai.span.kind"] == ("CHAIN" if team else "AGENT")
    assert attributes["autogen.span_kind"] == ("team_run" if team else "agent_run")
    input_alias = "autogen.task.content" if team else "autogen.input.last_message"
    output_alias = "autogen.task.result" if team else "autogen.output.content"
    expected_input = "__REDACTED__" if policy in POLICIES[::2] else "question"
    expected_output = "__REDACTED__" if policy in POLICIES[1::2] else "answer"
    assert attributes["input.value"] == attributes[input_alias] == expected_input
    assert attributes["output.value"] == attributes[output_alias] == expected_output
    if expected_input == "__REDACTED__":
        assert "question" not in span.to_json()
    if expected_output == "__REDACTED__":
        assert "answer" not in span.to_json()


@pytest.mark.parametrize("task,expected", [
    ("question", "question"),
    (Message("question", "user"), "question"),
    ([Message("first", "user"), Message("question", "user")], '["first", "question"]'),
])
def test_team_canonical_input_extracts_message_content(task, expected):
    attributes = _team_attributes(SimpleNamespace(), task, "run", TraceConfig())
    assert attributes["input.value"] == expected


@pytest.mark.parametrize("messages", [[], [Message("question", "user")]])
def test_team_without_answer_does_not_export_task_as_output(recording, messages):
    tracer, exporter = recording
    with tracer.start_as_current_span("empty-result") as span:
        _record_team_result(span, TaskResult(messages), TraceConfig(), input_message_count=len(messages))
    finished, = exporter.get_finished_spans()
    assert "output.value" not in finished.attributes
    assert "autogen.task.result" not in finished.attributes


@pytest.mark.parametrize("policy", ["hide_inputs", "hide_input_messages", "hide_input_text"])
@pytest.mark.parametrize("streaming", [False, True], ids=["call", "stream"])
@pytest.mark.parametrize("sequence", [False, True], ids=["message", "sequence"])
def test_team_input_only_non_user_task_stays_hidden(recording, monkeypatch, policy, streaming, sequence):
    monkeypatch.setitem(sys.modules, "autogen_agentchat.base", SimpleNamespace(
        Response=Response, TaskResult=TaskResult,
    ))
    raw_tracer, exporter = recording
    # Hide one input channel at a time; every output channel stays enabled.
    config = TraceConfig(**{flag: flag == policy for flag in POLICIES})
    tracer = FITracer(raw_tracer, config=config)
    messages = [Message("private-task-text", "operator")]
    if sequence:
        messages.insert(0, Message("private-earlier-text", "operator"))
    task = messages if sequence else messages[0]
    terminal = TaskResult(list(messages))
    instance = SimpleNamespace()
    calls = []
    cleanup = []

    async def original(self, *, task):
        calls.append((self, task))
        return terminal

    async def original_stream(self, *, task):
        calls.append((self, task))
        for message in messages:
            yield message
        yield terminal
        cleanup.append(True)

    wrapped = wrap_team_run(
        original_stream if streaming else original, tracer,
        "run_stream" if streaming else "run", config,
    )

    async def exercise():
        if not streaming:
            assert await wrapped(instance, task=task) is terminal
            return
        stream = wrapped(instance, task=task)
        for message in messages:
            assert await anext(stream) is message
        assert await anext(stream) is terminal
        assert cleanup == []
        assert len(exporter.get_finished_spans()) == 1
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert cleanup == [True]
        await stream.aclose()

    asyncio.run(exercise())
    assert len(calls) == 1
    assert calls[0][0] is instance
    assert calls[0][1] is task
    assert terminal.messages == messages
    assert [message.content for message in messages] == (
        ["private-earlier-text", "private-task-text"] if sequence else ["private-task-text"]
    )
    finished, = exporter.get_finished_spans()
    assert finished.attributes["input.value"] == "__REDACTED__"
    assert finished.attributes["autogen.task.content"] == "__REDACTED__"
    assert "output.value" not in finished.attributes
    assert "autogen.task.result" not in finished.attributes
    assert "private-task-text" not in finished.to_json()
    assert "private-earlier-text" not in finished.to_json()


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_tool_exports_canonical_kind(recording, asynchronous):
    tracer, exporter = recording
    returned = {"answer": 42}
    calls = []

    def original(value):
        calls.append(value)
        return returned

    async def original_async(value):
        return original(value)

    wrapped = wrap_tool_execution(original_async if asynchronous else original, tracer, "lookup")
    result = asyncio.run(wrapped("question")) if asynchronous else wrapped("question")
    assert result is returned
    assert calls == ["question"]
    span, = exporter.get_finished_spans()
    assert span.attributes["gen_ai.span.kind"] == "TOOL"
    assert span.attributes["autogen.span_kind"] == "tool_call"
