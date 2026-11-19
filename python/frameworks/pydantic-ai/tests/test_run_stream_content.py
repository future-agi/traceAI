"""A streamed run exports the same canonical content as a non-streamed run.

Spans use the real OTel SDK and an in-memory exporter. The model-backed cases
use Pydantic AI's local TestModel; the others fake only the streamed result.
"""

import asyncio
import collections.abc
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_pydantic_ai import PydanticAIInstrumentor


PROMPT = "PRIVATE_STREAM_PROMPT"
REPLY = "PRIVATE_STREAM_REPLY across several words"
STREAM_SPAN = "pydantic_ai.agent.run_stream"


@pytest.fixture
def exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    exporter.provider = provider
    yield exporter
    provider.shutdown()


@pytest.fixture
def instrumentor(monkeypatch, exporter):
    for name in (
        "HIDE_INPUTS", "HIDE_OUTPUTS", "HIDE_INPUT_MESSAGES", "HIDE_OUTPUT_MESSAGES",
        "HIDE_INPUT_TEXT", "HIDE_OUTPUT_TEXT", "HIDE_INPUT_IMAGES", "HIDE_ERROR_DETAILS",
        "HIDE_LLM_INVOCATION_PARAMETERS", "HIDE_EMBEDDING_VECTORS", "PII_REDACTION",
        "BASE64_IMAGE_MAX_LENGTH",
    ):
        monkeypatch.delenv("FI_" + name, raising=False)
    monkeypatch.setattr(PydanticAIInstrumentor, "_instance", None)
    monkeypatch.setattr(PydanticAIInstrumentor, "_is_instrumented", False)
    monkeypatch.setattr(PydanticAIInstrumentor, "_is_instrumented_by_opentelemetry", False)
    monkeypatch.setattr(PydanticAIInstrumentor, "instrumentation_dependencies", lambda self: ())
    instrumentor = PydanticAIInstrumentor()
    instrumentor._tracer = exporter.provider.get_tracer(__name__)
    yield instrumentor
    if instrumentor.is_instrumented:
        instrumentor.uninstrument()


@pytest.fixture
def make_agent(monkeypatch):
    pydantic_ai = pytest.importorskip("pydantic_ai")
    from pydantic_ai import models
    from pydantic_ai.models.test import TestModel

    # TestModel is local; refuse real model requests as an additional guard.
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)

    def make(with_tool=False):
        model = TestModel(custom_output_text=REPLY, call_tools="all" if with_tool else [])
        agent = pydantic_ai.Agent(model)
        if with_tool:
            @agent.tool_plain
            def lookup(q: str) -> str:
                return "looked up " + q
        return agent

    return make


async def text_deltas(result):
    return "".join([part async for part in result.stream_text(delta=True)])


async def full_text(result):
    return [part async for part in result.stream_text()][-1]


async def outputs(result):
    return [output async for output in result.stream_output()][-1]


async def get_output(result):
    return await result.get_output()


CONSUMERS = {
    "text-deltas": text_deltas,
    "full-text": full_text,
    "outputs": outputs,
    "get-output": get_output,
}


def stream(agent, consume, prompt=PROMPT):
    async def execute():
        async with agent.run_stream(prompt) as result:
            return await consume(result)

    return asyncio.run(execute())


def stream_span(exporter):
    (span,) = [span for span in exporter.get_finished_spans() if span.name == STREAM_SPAN]
    return span


def exported(exporter):
    return "\n".join(span.to_json() for span in exporter.get_finished_spans())


def assert_canonical_content(span, prompt, output):
    attributes = span.attributes
    assert attributes["gen_ai.span.kind"] == "AGENT"
    assert attributes["input.value"] == prompt
    assert attributes["input.mime_type"] == "text/plain"
    assert attributes["pydantic_ai.run.prompt"] == prompt
    assert attributes["output.value"] == output
    assert attributes["output.mime_type"] == "text/plain"
    assert attributes["pydantic_ai.run.result"] == output
    # The framework's own fields keep their existing values.
    assert attributes["pydantic_ai.span_kind"] == "stream"
    assert attributes["pydantic_ai.run.method"] == "run_stream"


@pytest.mark.parametrize("consumer", sorted(CONSUMERS))
def test_streamed_run_exports_prompt_reply_and_kind(instrumentor, exporter, make_agent, consumer):
    instrumentor.instrument(tracer_provider=exporter.provider)
    assert stream(make_agent(), CONSUMERS[consumer]) == REPLY

    span = stream_span(exporter)
    assert span.parent is None
    assert span.status.status_code is StatusCode.OK
    assert_canonical_content(span, PROMPT, REPLY)


def test_streamed_run_with_tool_keeps_execution_spans_under_the_run(instrumentor, exporter, make_agent):
    instrumentor.instrument(tracer_provider=exporter.provider)
    assert stream(make_agent(with_tool=True), text_deltas) == REPLY

    span = stream_span(exporter)
    children = [child for child in exporter.get_finished_spans() if child is not span]
    assert {child.name for child in children} >= {
        "pydantic_ai.model.request", "pydantic_ai.tool.call.lookup",
    }
    assert all(child.parent.span_id == span.context.span_id for child in children)
    assert_canonical_content(span, PROMPT, REPLY)


@pytest.mark.parametrize(
    "flags, hidden",
    [
        ({"hide_inputs": True}, {PROMPT}),
        ({"hide_outputs": True}, {REPLY}),
        ({"hide_inputs": True, "hide_outputs": True}, {PROMPT, REPLY}),
    ],
    ids=["inputs", "outputs", "both"],
)
def test_streamed_run_content_obeys_privacy(instrumentor, exporter, make_agent, flags, hidden):
    instrumentor.instrument(tracer_provider=exporter.provider, config=TraceConfig(**flags))
    # Hiding content never changes what the application receives.
    assert stream(make_agent(with_tool=True), text_deltas) == REPLY

    text = exported(exporter)
    for marker in (PROMPT, REPLY):
        assert (marker in text) is (marker not in hidden)
    attributes = stream_span(exporter).attributes
    assert attributes["gen_ai.span.kind"] == "AGENT"
    if PROMPT not in hidden:
        assert attributes["input.value"] == PROMPT
    if REPLY not in hidden:
        assert attributes["output.value"] == REPLY


def chunked_agent(instrumentor, chunks, events):
    """An agent whose stream yields text chunks, like StreamedRunResult."""

    @asynccontextmanager
    async def run_stream(self, user_prompt):
        async def stream_text(*, delta=False):
            text = ""
            try:
                for chunk in chunks:
                    text += chunk
                    yield chunk if delta else text
            finally:
                events.append("closed")

        async def stream_output():
            async for text in stream_text():
                yield text

        async def get_output():
            return await full_text(result)

        result = SimpleNamespace(
            stream_text=stream_text, stream_output=stream_output, get_output=get_output,
        )
        yield result

    class Agent:
        model = None

    Agent.run_stream = instrumentor._wrap_run_stream(run_stream)
    return Agent()


@pytest.mark.parametrize("consumer", sorted(CONSUMERS))
def test_completed_stream_records_the_final_output(instrumentor, exporter, consumer):
    events = []
    agent = chunked_agent(instrumentor, ["first ", "second ", "third"], events)

    assert stream(agent, CONSUMERS[consumer], prompt="question") == "first second third"
    assert events == ["closed"]
    assert_canonical_content(stream_span(exporter), "question", "first second third")


@pytest.mark.parametrize("delta", [False, True], ids=["full-text", "text-deltas"])
def test_incomplete_stream_records_input_without_inventing_output(instrumentor, exporter, delta):
    events = []
    agent = chunked_agent(instrumentor, ["first ", "second ", "third"], events)

    async def first_chunk(result):
        async for part in result.stream_text(delta=delta):
            return part

    assert stream(agent, first_chunk, prompt="question") == "first "
    attributes = stream_span(exporter).attributes
    assert attributes["gen_ai.span.kind"] == "AGENT"
    assert attributes["input.value"] == "question"
    assert attributes["input.mime_type"] == "text/plain"
    for key in ("output.value", "output.mime_type", "pydantic_ai.run.result"):
        assert key not in attributes


def test_closing_an_observed_stream_closes_the_native_stream(instrumentor, exporter):
    events = []
    agent = chunked_agent(instrumentor, ["first ", "second "], events)

    async def execute():
        async with agent.run_stream("question") as result:
            parts = result.stream_text(delta=True)
            # Callers (and Pydantic AI's sync bridge) close async generators explicitly.
            assert isinstance(parts, collections.abc.AsyncGenerator)
            assert await parts.__anext__() == "first "
            await parts.aclose()
            assert events == ["closed"]

    asyncio.run(execute())
    assert "output.value" not in stream_span(exporter).attributes


def test_awaitable_stream_factory_exports_canonical_content(instrumentor, exporter):
    class Result:
        async def get_output(self):
            return "answer"

    class Manager:
        async def __aenter__(self):
            return Result()

        async def __aexit__(self, *exc_info):
            return False

    class Agent:
        model = None

        async def run_stream(self, user_prompt):
            return Manager()

    Agent.run_stream = instrumentor._wrap_run_stream(Agent.run_stream)

    async def execute():
        async with await Agent().run_stream(user_prompt="question") as result:
            return await result.get_output()

    assert asyncio.run(execute()) == "answer"
    span = stream_span(exporter)
    assert span.status.status_code is StatusCode.OK
    assert_canonical_content(span, "question", "answer")
