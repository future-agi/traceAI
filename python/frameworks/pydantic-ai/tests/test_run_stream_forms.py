"""Both forms of run_stream record a failure's type and the run's own usage.

An agent's run_stream either returns its stream manager or, in older and
custom agents, is awaited for it. Each case wraps one of the two forms around
Pydantic AI's own streamed run on its local TestModel or FunctionModel. Spans
go through the real OTel SDK to an in-memory exporter.
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_pydantic_ai import PydanticAIInstrumentor

pydantic_ai = pytest.importorskip("pydantic_ai")


REPLY = "PRIVATE_FORMS_REPLY in several words"
STREAM_SPAN = "pydantic_ai.agent.run_stream"
USAGE = ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "gen_ai.usage.total_tokens")
FORMS = ["manager", "awaitable"]


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
    from pydantic_ai import models

    # The models below are local; refuse real model requests as well.
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    monkeypatch.setattr(PydanticAIInstrumentor, "_instance", None)
    monkeypatch.setattr(PydanticAIInstrumentor, "_is_instrumented", False)
    instrumentor = PydanticAIInstrumentor()
    # Pydantic AI's Agent stays unpatched: its stream is the one each form
    # hands out, and the wrapped form is the only run_stream span.
    instrumentor._tracer = exporter.provider.get_tracer(__name__)
    return instrumentor


class ResponsesWithoutUsage:
    """Pydantic AI's streamed result, whose responses carry no usage of their
    own, as on Pydantic AI releases from before per-response usage."""

    def __init__(self, result):
        self._result = result

    def __getattr__(self, name):
        return getattr(self._result, name)

    def new_messages(self, *args, **kwargs):
        return [
            SimpleNamespace(kind="response", parts=message.parts)
            if getattr(message, "kind", None) == "response" else message
            for message in self._result.new_messages(*args, **kwargs)
        ]


def streaming_agent(instrumentor, agent, form, without_response_usage=False):
    """An agent whose run_stream returns `agent`'s stream manager ("manager")
    or is awaited for it ("awaitable")."""

    @asynccontextmanager
    async def manager(user_prompt, **kwargs):
        async with agent.run_stream(user_prompt, **kwargs) as result:
            yield ResponsesWithoutUsage(result) if without_response_usage else result

    if form == "manager":
        def run_stream(self, user_prompt, **kwargs):
            return manager(user_prompt, **kwargs)
    else:
        async def run_stream(self, user_prompt, **kwargs):
            return manager(user_prompt, **kwargs)

    class Agent:
        model = None

    Agent.run_stream = instrumentor._wrap_run_stream(run_stream)
    return Agent()


async def open_stream(agent, form, **options):
    manager = agent.run_stream("PRIVATE_FORMS_PROMPT", **options)
    return await manager if form == "awaitable" else manager


def stream_span(exporter):
    (span,) = [span for span in exporter.get_finished_spans() if span.name == STREAM_SPAN]
    return span


def failing_model():
    from pydantic_ai.models.function import FunctionModel

    def answer(messages, info):
        raise LookupError("model failed")

    async def stream_answer(messages, info):
        raise LookupError("model failed")
        yield ""  # an async generator, like any streaming function

    return FunctionModel(answer, stream_function=stream_answer)


def usage_of(result):
    usage = result.usage
    return usage() if callable(usage) else usage


def usage_counts(usage):
    """The token counts, under the names either SDK generation uses."""
    counts = {}
    for key, names in zip(USAGE, (
        ("input_tokens", "request_tokens"), ("output_tokens", "response_tokens"), ("total_tokens",),
    )):
        for name in names:
            value = getattr(usage, name, None)
            if isinstance(value, int):
                counts[key] = value
                break
    return counts


def shared_budget():
    """A usage budget another operation has already counted into."""
    from pydantic_ai import usage as usage_module

    if hasattr(usage_module, "RunUsage"):
        return usage_module.RunUsage(input_tokens=100, output_tokens=50, requests=1)
    return usage_module.Usage(request_tokens=100, response_tokens=50, total_tokens=150, requests=1)


@pytest.mark.parametrize("phase", ["model", "caller"])
@pytest.mark.parametrize("form", FORMS)
def test_failed_stream_records_the_error_type_in_either_form(instrumentor, exporter, form, phase):
    from pydantic_ai.models.test import TestModel

    model = failing_model() if phase == "model" else TestModel(custom_output_text=REPLY)
    agent = streaming_agent(instrumentor, pydantic_ai.Agent(model), form)

    async def execute():
        async with await open_stream(agent, form) as result:
            assert await result.get_output() == REPLY
            raise KeyError("caller failed")

    with pytest.raises(Exception) as caught:
        asyncio.run(execute())

    if phase == "caller":
        assert isinstance(caught.value, KeyError)
    span = stream_span(exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["pydantic_ai.is_error"] is True
    # The type of the exception the caller received, as a failed run records it.
    assert span.attributes.get("pydantic_ai.error.type") == type(caught.value).__name__


@pytest.mark.parametrize("budget", ["own", "shared"])
@pytest.mark.parametrize("form", FORMS)
def test_stream_without_response_usage_reports_only_its_own_budget(instrumentor, exporter, form, budget):
    from pydantic_ai.models.test import TestModel

    native = pydantic_ai.Agent(TestModel(custom_output_text=REPLY))
    agent = streaming_agent(instrumentor, native, form, without_response_usage=True)
    options = {"usage": shared_budget()} if budget == "shared" else {}

    async def execute():
        async with await open_stream(agent, form, **options) as result:
            assert await result.get_output() == REPLY
            # The finished run has a response, but no counts of its own on it.
            assert any(getattr(message, "kind", None) == "response" for message in result.new_messages())
            return usage_counts(usage_of(result))

    totals = asyncio.run(execute())
    span = stream_span(exporter)
    assert span.status.status_code is StatusCode.OK
    assert totals.get("gen_ai.usage.input_tokens", 0) > 0
    recorded = {key: span.attributes[key] for key in USAGE if key in span.attributes}
    if budget == "own":
        # Nothing else counts into a budget the run created for itself.
        assert recorded == totals
    else:
        # A budget the caller passes in may hold other operations' counts. With
        # no counts of the run's own, the span reports none rather than those.
        assert recorded == {}
