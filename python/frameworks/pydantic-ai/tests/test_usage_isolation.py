"""Run-local real-SDK usage; no model or collector connections are needed."""

import asyncio
import inspect

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai import Agent, models
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai import usage as usage_module
from traceai_pydantic_ai import PydanticAIInstrumentor
from traceai_pydantic_ai._agent_wrapper import extract_usage


@pytest.fixture
def traces(monkeypatch):
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = PydanticAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        yield exporter
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def run_usage():
    cls = getattr(usage_module, "RunUsage", None) or usage_module.Usage
    return cls()


def response(tokens=10):
    if "usage" in inspect.signature(ModelResponse).parameters:
        return ModelResponse(parts=[TextPart("ok")], usage=usage_module.RequestUsage(
            input_tokens=tokens, output_tokens=5))
    # Historical FunctionModel calculates usage from the real messages.
    return ModelResponse(parts=[TextPart("ok")])


def get_usage(result):
    return result.usage() if callable(result.usage) else result.usage


def counts(value):
    return tuple(getattr(value, key, None) for key in (
        "input_tokens" if hasattr(value, "input_tokens") else "request_tokens",
        "output_tokens" if hasattr(value, "output_tokens") else "response_tokens",
        "total_tokens"))


def span_counts(exporter):
    return [tuple(span.attributes.get(f"gen_ai.usage.{key}") for key in (
        "input_tokens", "output_tokens", "total_tokens"))
        for span in exporter.get_finished_spans()]


@pytest.mark.parametrize("method", ["run_sync", "run"])
def test_empty_shared_usage_returns_original_success(traces, method):
    shared = run_usage()  # Usage in 0.1.0 starts with nullable token counters.
    agent = Agent(TestModel())
    async def execute():
        return await agent.run("hello", usage=shared)
    result = asyncio.run(execute()) if method == "run" else agent.run_sync("hello", usage=shared)
    assert get_usage(result) is shared
    expected = counts(shared)
    assert span_counts(traces) == [expected]
    assert len(traces.get_finished_spans()) == 1
    assert traces.get_finished_spans()[0].status.status_code.name == "OK"


def test_unknown_usage_is_not_invented():
    from types import SimpleNamespace
    unknown = SimpleNamespace(request_tokens=None, response_tokens=None,
                              total_tokens=None, requests=0)
    values = extract_usage(SimpleNamespace(usage=unknown))
    assert not any(values.get(key) is not None for key in (
        "input_tokens", "output_tokens", "total_tokens"))


@pytest.mark.parametrize("has_messages", [True, False])
def test_missing_measurements_never_fall_back_to_a_shared_aggregate(has_messages):
    from types import SimpleNamespace
    from traceai_pydantic_ai._agent_wrapper import _OperationUsage
    shared = run_usage()
    operation = _OperationUsage(shared)
    # Another operation modifies the aggregate while this one does no request.
    if hasattr(shared, "input_tokens"):
        shared.input_tokens = 100
    else:
        shared.request_tokens = 100
    result = SimpleNamespace(usage=shared)
    if has_messages:
        result.new_messages = lambda: []
    assert operation.extract(result) == {}


def test_overlapping_shared_budget_exports_only_each_runs_responses(traces):
    async def execute():
        second_started, release_second = asyncio.Event(), asyncio.Event()
        async def first_model(messages, info):
            await second_started.wait()
            return response(10)
        async def second_model(messages, info):
            second_started.set()
            await release_second.wait()
            return response(20)
        shared = run_usage()
        first_agent, second_agent = Agent(FunctionModel(first_model)), Agent(FunctionModel(second_model))
        # Each standalone call provides an independent oracle on historical SDKs
        # that estimate usage rather than carrying it in ModelResponse.
        expected = []
        first = asyncio.create_task(first_agent.run("first", usage=shared))
        second = asyncio.create_task(second_agent.run("second", usage=shared))
        first_result = await first
        expected.append(counts(get_usage(first_result)))
        release_second.set()
        second_result = await second
        assert get_usage(second_result) is shared
        total = counts(shared)
        expected.append(tuple(b - a for a, b in zip(expected[0], total)))
        return expected, total
    expected, total = asyncio.run(execute())
    actual = span_counts(traces)
    assert actual == expected
    assert tuple(map(sum, zip(*actual))) == total
    if hasattr(usage_module, "RequestUsage"):
        assert actual == [(10, 5, 15), (20, 5, 25)]


@pytest.mark.parametrize("method", ["run_sync", "run"])
def test_continuation_exports_only_this_turn(traces, method):
    agent = Agent(FunctionModel(lambda messages, info: response()))
    async def execute():
        first = await agent.run("first")
        continuation = ({"conversation": first.conversation}
                        if hasattr(first, "conversation") else {"message_history": first.all_messages()})
        second = await agent.run("second", **continuation)
        return first, second
    if method == "run":
        first, second = asyncio.run(execute())
    else:
        first = agent.run_sync("first")
        continuation = ({"conversation": first.conversation}
                        if hasattr(first, "conversation") else {"message_history": first.all_messages()})
        second = agent.run_sync("second", **continuation)
    assert second.all_messages()[:len(first.all_messages())] == first.all_messages()
    assert len(traces.get_finished_spans()) == 2
    actual = span_counts(traces)
    if hasattr(first, "conversation"):
        assert second.conversation.conversation_id == first.conversation.conversation_id
        assert counts(first.conversation.usage) == (10, 5, 15)
        assert actual == [(10, 5, 15), (10, 5, 15)]
        assert counts(get_usage(second)) == (20, 10, 30)
    else:
        # Old message_history doesn't carry a cumulative budget.
        assert actual == [counts(get_usage(first)), counts(get_usage(second))]
