"""Real offline Agent contracts: one usage delta per logical run."""

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai import Agent, models
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from traceai_pydantic_ai import PydanticAIInstrumentor


@pytest.fixture
def traced(monkeypatch):
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


def counters(usage):
    return (usage.input_tokens, usage.output_tokens, usage.total_tokens)


@pytest.mark.parametrize("method", ["run_sync", "run"])
def test_consecutive_runs_export_only_their_usage_delta(traced, method):
    agent = Agent(TestModel(custom_output_text="hello"))
    shared = RunUsage(input_tokens=100, output_tokens=50, requests=1)
    initial = counters(shared)
    expected = []

    async def run_twice():
        for prompt in ("hello", "hello again"):
            before = counters(shared)
            result = await agent.run(prompt, usage=shared)
            assert result.output == "hello"
            expected.append(
                tuple(after - prior for after, prior in zip(counters(shared), before))
            )

    if method == "run":
        asyncio.run(run_twice())
    else:
        for prompt in ("hello", "hello again"):
            before = counters(shared)
            result = agent.run_sync(prompt, usage=shared)
            assert result.output == "hello"
            expected.append(
                tuple(after - prior for after, prior in zip(counters(shared), before))
            )

    spans = traced.get_finished_spans()
    assert len(spans) == 2
    keys = ("input_tokens", "output_tokens", "total_tokens")
    for span, delta in zip(spans, expected):
        assert span.name == "pydantic_ai.agent.run"
        assert span.attributes["pydantic_ai.run.method"] == method
        assert tuple(span.attributes[f"gen_ai.usage.{key}"] for key in keys) == delta
        assert all(value > 0 for value in delta)
    for i, key in enumerate(keys):
        assert (
            sum(span.attributes[f"gen_ai.usage.{key}"] for span in spans)
            == counters(shared)[i] - initial[i]
        )


@pytest.mark.parametrize("same_agent", [False, True])
def test_sync_delegation_does_not_suppress_nested_logical_runs(traced, same_agent):
    # Only the internal run_sync -> run call is suppressed, not a real child
    # operation, including a recursive call on the same Agent instance.
    local_model = TestModel(custom_output_text="nested answer")
    child = Agent(local_model)

    async def invoke_child(messages, info):
        result = await (parent if same_agent else child).run(
            "child prompt", model=local_model
        )
        assert result.output == "nested answer"
        from pydantic_ai.messages import ModelResponse, TextPart

        return ModelResponse(parts=[TextPart("parent answer")])

    parent = Agent(FunctionModel(invoke_child))
    assert parent.run_sync("parent prompt").output == "parent answer"
    spans = traced.get_finished_spans()
    assert len(spans) == 2
    nested, outer = spans
    assert nested.attributes["pydantic_ai.run.method"] == "run"
    assert outer.attributes["pydantic_ai.run.method"] == "run_sync"
    assert nested.parent.span_id == outer.context.span_id
    assert nested.attributes["gen_ai.usage.input_tokens"] > 0


def test_sync_delegation_marker_is_reset_after_error(traced):
    error = ValueError("local model failed")

    def fail(messages, info):
        raise error

    agent = Agent(FunctionModel(fail))
    with pytest.raises(ValueError) as caught:
        agent.run_sync("fail")
    assert caught.value is error
    assert (
        asyncio.run(
            agent.run("recover", model=TestModel(custom_output_text="ok"))
        ).output
        == "ok"
    )
    failed, recovered = traced.get_finished_spans()
    assert failed.status.status_code.name == "ERROR"
    assert recovered.status.status_code.name == "OK"
    assert recovered.attributes["pydantic_ai.run.method"] == "run"
    assert recovered.parent is None
