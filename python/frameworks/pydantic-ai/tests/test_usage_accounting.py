"""Real offline Agent contracts: one usage delta per logical run."""

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai import Agent, RunContext, models
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai import usage as usage_module
from traceai_pydantic_ai import PydanticAIInstrumentor

RunUsage = getattr(usage_module, "RunUsage", None) or usage_module.Usage


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
    input_name = "input_tokens" if hasattr(usage, "input_tokens") else "request_tokens"
    output_name = "output_tokens" if hasattr(usage, "output_tokens") else "response_tokens"
    return (getattr(usage, input_name), getattr(usage, output_name), usage.total_tokens)


def seeded_usage():
    if hasattr(usage_module, "RunUsage"):
        return RunUsage(input_tokens=100, output_tokens=50, requests=1)
    return RunUsage(request_tokens=100, response_tokens=50, total_tokens=150, requests=1)


@pytest.mark.parametrize("method", ["run_sync", "run"])
def test_consecutive_runs_export_only_their_usage_delta(traced, method):
    agent = Agent(TestModel(custom_output_text="hello"))
    shared = seeded_usage()
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


@pytest.mark.parametrize("method", ["run_sync", "run"])
@pytest.mark.parametrize("explicit_budget", [False, True])
def test_nested_tool_runs_account_for_the_effective_sdk_budget(traced, method, explicit_budget):
    parent = Agent(TestModel(custom_output_text="parent answer"), name="parent")
    child = Agent(TestModel(custom_output_text="child answer"), name="child")
    budgets = []
    child_counts = []

    @parent.tool
    async def invoke(ctx: RunContext[None]) -> str:
        budgets.append(ctx.usage)
        before = counters(ctx.usage)
        result = await child.run("child prompt", usage=ctx.usage)
        actual_usage = result.usage() if callable(result.usage) else result.usage
        assert actual_usage is ctx.usage
        child_counts.append(tuple(after - (prior or 0) for after, prior in zip(counters(ctx.usage), before)))
        return result.output

    kwargs = {"usage": RunUsage()} if explicit_budget else {}
    if method == "run_sync":
        result = parent.run_sync("parent prompt", **kwargs)
    else:
        result = asyncio.run(parent.run("parent prompt", **kwargs))
    assert result.output == "parent answer"
    actual_usage = result.usage() if callable(result.usage) else result.usage
    assert actual_usage is budgets[0]
    if explicit_budget:
        assert actual_usage is kwargs["usage"]
    nested, outer = traced.get_finished_spans()  # Exactly one span per logical operation.
    assert nested.parent.span_id == outer.context.span_id
    keys = ("input_tokens", "output_tokens", "total_tokens")
    actual = [tuple(span.attributes[f"gen_ai.usage.{key}"] for key in keys)
              for span in (nested, outer)]
    assert actual[0] == child_counts[0]
    assert actual[1] == tuple(total - child for total, child in zip(counters(actual_usage), actual[0]))
    assert tuple(map(sum, zip(*actual))) == counters(actual_usage)
    # Historical TestModel's three requests consume these exact counts. This
    # guards the original [52, 158] double count, not just span presence.
    if not hasattr(usage_module, "RequestUsage"):
        assert actual == [(52, 2, 54), (106, 6, 112)]
        assert counters(actual_usage) == (158, 8, 166)


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
