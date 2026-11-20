"""A streamed run's span records what a run of the same agent records.

Each case runs one agent with run() (or run_sync()) and with run_stream() (or
run_stream_sync()) on Pydantic AI's local TestModel or FunctionModel, so the
streamed result is the SDK's own. Spans go through the real OTel SDK to an
in-memory exporter.
"""

import asyncio

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_pydantic_ai import PydanticAIInstrumentor

pydantic_ai = pytest.importorskip("pydantic_ai")
BaseModel = pytest.importorskip("pydantic").BaseModel


PROMPT = "PRIVATE_PARITY_PROMPT"
REPLY = "PRIVATE_PARITY_REPLY in several words"
NAME = "parity-agent"
RUN_SPAN = "pydantic_ai.agent.run"
STREAM_SPAN = "pydantic_ai.agent.run_stream"

# What a run's span says about the run; a streamed run of the same agent says the same.
INPUT_SIDE = (
    "gen_ai.span.kind",
    "input.value",
    "input.mime_type",
    "pydantic_ai.run.prompt",
    "gen_ai.request.model",
    "gen_ai.system",
    "pydantic_ai.agent.name",
    "pydantic_ai.agent.result_type",
    "pydantic_ai.run.is_structured",
    "pydantic_ai.run.message_history_length",
)
OUTPUT_SIDE = ("output.value", "output.mime_type", "pydantic_ai.run.result")
STATUS_SIDE = ("pydantic_ai.is_error", "pydantic_ai.error.type")
CONTRACT = INPUT_SIDE + OUTPUT_SIDE + STATUS_SIDE
USAGE = ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.usage.total_tokens")


class Answer(BaseModel):
    city: str
    population: int


ANSWER = {"city": "PRIVATE_PARITY_CITY", "population": 7}


@pytest.fixture
def exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    exporter.provider = provider
    yield exporter
    provider.shutdown()


@pytest.fixture
def instrument(monkeypatch, exporter):
    from pydantic_ai import models

    for name in (
        "HIDE_INPUTS", "HIDE_OUTPUTS", "HIDE_INPUT_MESSAGES", "HIDE_OUTPUT_MESSAGES",
        "HIDE_INPUT_TEXT", "HIDE_OUTPUT_TEXT", "HIDE_INPUT_IMAGES", "HIDE_ERROR_DETAILS",
        "HIDE_LLM_INVOCATION_PARAMETERS", "HIDE_EMBEDDING_VECTORS", "PII_REDACTION",
        "BASE64_IMAGE_MAX_LENGTH",
    ):
        monkeypatch.delenv("FI_" + name, raising=False)
    # The models below are local; refuse real model requests as well.
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    monkeypatch.setattr(PydanticAIInstrumentor, "_instance", None)
    monkeypatch.setattr(PydanticAIInstrumentor, "_is_instrumented", False)
    monkeypatch.setattr(PydanticAIInstrumentor, "_is_instrumented_by_opentelemetry", False)
    monkeypatch.setattr(PydanticAIInstrumentor, "instrumentation_dependencies", lambda self: ())
    instrumentor = PydanticAIInstrumentor()

    def start(**flags):
        instrumentor.instrument(tracer_provider=exporter.provider, config=TraceConfig(**flags))

    yield start
    if instrumentor.is_instrumented:
        instrumentor.uninstrument()


def text_agent(with_tool=False):
    from pydantic_ai.models.test import TestModel

    model = TestModel(custom_output_text=REPLY, call_tools="all" if with_tool else [])
    agent = pydantic_ai.Agent(model, name=NAME)
    if with_tool:
        @agent.tool_plain
        def lookup(q: str) -> str:
            return "looked up " + q
    return agent


def structured_agent():
    from pydantic_ai.models.test import TestModel

    return pydantic_ai.Agent(TestModel(custom_output_args=ANSWER), name=NAME, output_type=Answer)


def failing_agent():
    from pydantic_ai.models.function import FunctionModel

    def answer(messages, info):
        raise LookupError("model failed")

    async def stream_answer(messages, info):
        raise LookupError("model failed")
        yield ""  # an async generator, like any streaming function

    return pydantic_ai.Agent(FunctionModel(answer, stream_function=stream_answer), name=NAME)


def has_sync_stream():
    return hasattr(pydantic_ai.Agent, "run_stream_sync")


def settles_streamed_runs():
    from pydantic_ai.result import StreamedRunResult

    return hasattr(StreamedRunResult, "stream_response") and hasattr(StreamedRunResult, "result")


def finished(exporter, name):
    (span,) = [span for span in exporter.get_finished_spans() if span.name == name]
    return span


def run_span(exporter, agent, **options):
    """The span of agent.run(); the exporter starts empty for the streamed run."""
    asyncio.run(agent.run(PROMPT, **options))
    span = finished(exporter, RUN_SPAN)
    exporter.clear()
    return span


def usage_of(result):
    usage = result.usage
    return usage() if callable(usage) else usage


def stream(agent, consume, **options):
    async def execute():
        async with agent.run_stream(PROMPT, **options) as result:
            received = await consume(result)
            return received, usage_of(result)

    return asyncio.run(execute())


async def text_deltas(result):
    return "".join([part async for part in result.stream_text(delta=True)])


async def full_text(result):
    return [part async for part in result.stream_text()][-1]


async def outputs(result):
    # Pydantic AI 0.1 calls stream_output() stream().
    stream_output = getattr(result, "stream_output", None) or result.stream
    return [output async for output in stream_output()][-1]


async def get_output(result):
    return await result.get_output()


async def responses(result):
    # Reading the response snapshots finishes the run without handing its
    # output to the caller.
    async for _ in result.stream_response():
        pass


CONSUMERS = {
    "text-deltas": text_deltas,
    "full-text": full_text,
    "outputs": outputs,
    "get-output": get_output,
    "responses": responses,
}


def contract(span, keys):
    return {key: span.attributes.get(key) for key in keys}


def usage_counts(usage):
    """The run's token counts, under the names either SDK generation uses."""
    counts = {}
    for key, names in (
        ("gen_ai.usage.input_tokens", ("input_tokens", "request_tokens")),
        ("gen_ai.usage.output_tokens", ("output_tokens", "response_tokens")),
        ("gen_ai.usage.total_tokens", ("total_tokens",)),
    ):
        for name in names:
            value = getattr(usage, name, None)
            if isinstance(value, int):
                counts[key] = value
                break
    return counts


def assert_records_like_the_run(span, run, usage=None, keys=CONTRACT):
    # Never compare two spans that both lack the contract.
    assert run.attributes["gen_ai.span.kind"] == "AGENT"
    assert run.attributes["pydantic_ai.agent.name"] == NAME
    assert contract(span, keys) == contract(run, keys)
    # The framework's own fields keep their streamed values.
    assert span.attributes["pydantic_ai.span_kind"] == "stream"
    assert span.attributes["pydantic_ai.run.method"] == "run_stream"
    if usage is not None:
        counts = usage_counts(usage)
        assert counts.get("gen_ai.usage.input_tokens", 0) > 0
        assert contract(span, counts) == counts


@pytest.mark.parametrize("consumer", sorted(CONSUMERS))
def test_streamed_run_records_what_a_run_records(instrument, exporter, consumer):
    if consumer == "responses" and not settles_streamed_runs():
        pytest.skip("this Pydantic AI has no stream_response() or settled streamed result")
    instrument()
    agent = text_agent()
    expected = run_span(exporter, agent)
    received, usage = stream(agent, CONSUMERS[consumer])

    if consumer != "responses":
        assert received == REPLY
    span = finished(exporter, STREAM_SPAN)
    assert span.parent is None
    assert span.status.status_code is StatusCode.OK
    assert expected.attributes["output.value"] == REPLY
    assert_records_like_the_run(span, expected, usage)


def test_streamed_run_with_a_tool_records_what_a_run_records(instrument, exporter):
    instrument()
    agent = text_agent(with_tool=True)
    expected = run_span(exporter, agent)
    received, _ = stream(agent, text_deltas)

    assert received == REPLY
    span = finished(exporter, STREAM_SPAN)
    assert_records_like_the_run(span, expected)
    for key in ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens"):
        assert span.attributes[key] > 0


@pytest.mark.parametrize("consumer", ["outputs", "get-output"])
def test_structured_streamed_run_records_what_a_run_records(instrument, exporter, consumer):
    instrument()
    agent = structured_agent()
    expected = run_span(exporter, agent)
    received, usage = stream(agent, CONSUMERS[consumer])

    assert received == Answer(**ANSWER)
    span = finished(exporter, STREAM_SPAN)
    assert expected.attributes["pydantic_ai.run.is_structured"] is True
    assert "PRIVATE_PARITY_CITY" in expected.attributes["output.value"]
    assert_records_like_the_run(span, expected, usage)


def test_streamed_run_with_history_records_what_a_run_records(instrument, exporter):
    instrument()
    agent = text_agent()
    history = asyncio.run(agent.run("an earlier question")).new_messages()
    exporter.clear()
    expected = run_span(exporter, agent, message_history=history)
    received, _ = stream(agent, get_output, message_history=history)

    assert received == REPLY
    span = finished(exporter, STREAM_SPAN)
    assert span.attributes["pydantic_ai.run.message_history_length"] == len(history)
    assert_records_like_the_run(span, expected)
    for key in ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens"):
        assert span.attributes[key] > 0


def test_sync_streamed_run_records_what_a_sync_run_records(instrument, exporter):
    if not has_sync_stream():
        pytest.skip("this Pydantic AI has no run_stream_sync()")
    instrument()
    agent = text_agent()
    agent.run_sync(PROMPT)
    expected = finished(exporter, RUN_SPAN)
    exporter.clear()

    with agent.run_stream_sync(PROMPT) as result:
        received = "".join(result.stream_text(delta=True))
        usage = usage_of(result)

    assert received == REPLY
    span = finished(exporter, STREAM_SPAN)
    assert span.status.status_code is StatusCode.OK
    assert_records_like_the_run(span, expected, usage)


@pytest.mark.parametrize(
    "flags, hidden",
    [
        ({"hide_inputs": True}, (PROMPT,)),
        ({"hide_outputs": True}, (REPLY,)),
        ({"hide_inputs": True, "hide_outputs": True}, (PROMPT, REPLY)),
    ],
    ids=["inputs", "outputs", "both"],
)
def test_streamed_run_hides_content_like_a_run(instrument, exporter, flags, hidden):
    instrument(**flags)
    agent = text_agent()
    expected = run_span(exporter, agent)
    # Hiding content never changes what the application receives.
    received, usage = stream(agent, text_deltas)

    assert received == REPLY
    span = finished(exporter, STREAM_SPAN)
    assert_records_like_the_run(span, expected, usage)
    exported = span.to_json()
    for marker in (PROMPT, REPLY):
        assert (marker in exported) is (marker not in hidden)


@pytest.mark.parametrize("form", ["async", "sync"])
def test_failed_streamed_run_records_its_error_like_a_run(instrument, exporter, form):
    if form == "sync" and not has_sync_stream():
        pytest.skip("this Pydantic AI has no run_stream_sync()")
    instrument()
    agent = failing_agent()
    with pytest.raises(Exception) as run_error:
        asyncio.run(agent.run(PROMPT))
    expected = finished(exporter, RUN_SPAN)
    exporter.clear()

    with pytest.raises(Exception) as stream_error:
        if form == "async":
            stream(agent, get_output)
        else:
            with agent.run_stream_sync(PROMPT) as result:
                result.get_output()

    span = finished(exporter, STREAM_SPAN)
    for failed, error in ((expected, run_error.value), (span, stream_error.value)):
        assert failed.status.status_code is StatusCode.ERROR
        assert failed.attributes["pydantic_ai.is_error"] is True
        assert failed.attributes["pydantic_ai.error.type"] == type(error).__name__
    assert_records_like_the_run(span, expected, keys=INPUT_SIDE + OUTPUT_SIDE)


def test_streamed_run_ends_with_the_callers_error(instrument, exporter):
    instrument()
    agent = text_agent()
    expected = run_span(exporter, agent)

    async def execute():
        async with agent.run_stream(PROMPT) as result:
            await result.get_output()
            raise KeyError("caller failed")

    with pytest.raises(Exception) as caught:
        asyncio.run(execute())

    span = finished(exporter, STREAM_SPAN)
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["pydantic_ai.is_error"] is True
    assert span.attributes["pydantic_ai.error.type"] == type(caught.value).__name__
    assert_records_like_the_run(span, expected, keys=INPUT_SIDE)


@pytest.mark.parametrize("form", ["async", "sync"])
def test_abandoned_streamed_run_ends_its_span_without_output(instrument, exporter, form):
    if form == "sync" and not has_sync_stream():
        pytest.skip("this Pydantic AI has no run_stream_sync()")
    instrument()
    agent = text_agent()
    expected = run_span(exporter, agent)

    # Ungrouped deltas, so the rest of the reply is still unread when the caller
    # stops (the sync form reads ahead, but only one chunk).
    if form == "async":
        async def first_delta():
            async with agent.run_stream(PROMPT) as result:
                parts = result.stream_text(delta=True, debounce_by=None)
                first = await parts.__anext__()
                await parts.aclose()
                return first

        first = asyncio.run(first_delta())
    else:
        with agent.run_stream_sync(PROMPT) as result:
            parts = result.stream_text(delta=True, debounce_by=None)
            first = next(parts)
            parts.close()

    assert first and REPLY.startswith(first)
    span = finished(exporter, STREAM_SPAN)
    assert span.end_time is not None
    assert span.status.status_code is StatusCode.OK
    assert span.attributes["pydantic_ai.is_error"] is False
    assert_records_like_the_run(span, expected, keys=INPUT_SIDE)
    # A stream the caller never finished has no output or run usage to report.
    for key in OUTPUT_SIDE + USAGE:
        assert key not in span.attributes
