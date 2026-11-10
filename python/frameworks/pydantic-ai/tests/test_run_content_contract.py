"""T5-PY-003: exported run attributes satisfy FutureAGI's content contract.

Only the wrapped library is faked; spans use the real OTel SDK/exporter. Literal
canonical keys pin the collector boundary without a network or a collector.
"""

import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_pydantic_ai import PydanticAIInstrumentor


@pytest.fixture
def traced_agent(monkeypatch):
    class FakeAgent:
        def __init__(self, result, child=None, error=None):
            self.result = result
            self.child = child
            self.error = error
            self.calls = []

        async def run(self, user_prompt, **kwargs):
            self.calls.append((user_prompt, kwargs))
            if self.error is not None:
                raise self.error
            if self.child is not None:
                self.child_result = await self.child.run(user_prompt="child question")
            return self.result

        def run_sync(self, *args, **kwargs):
            # Like the SDK, run_sync delegates to the instrumented async run.
            return asyncio.run(self.run(*args, **kwargs))

    library = ModuleType("pydantic_ai")
    library.Agent = FakeAgent
    library.usage = SimpleNamespace(RequestUsage=object)
    monkeypatch.setitem(sys.modules, "pydantic_ai", library)
    monkeypatch.setattr(PydanticAIInstrumentor, "_instance", None)
    monkeypatch.setattr(PydanticAIInstrumentor, "_is_instrumented", False)
    monkeypatch.setattr(
        PydanticAIInstrumentor, "instrumentation_dependencies", lambda self: ()
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = PydanticAIInstrumentor()
    try:
        instrumentor.instrument(tracer_provider=provider)
        assert instrumentor.is_instrumented
        yield FakeAgent, exporter
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def invoke(agent, method, *args, **kwargs):
    result = getattr(agent, method)(*args, **kwargs)
    return asyncio.run(result) if method == "run" else result


def assert_content_contract(span, prompt, output, method):
    attributes = span.attributes
    assert span.name == "pydantic_ai.agent.run"
    assert attributes["pydantic_ai.run.method"] == method
    assert attributes["pydantic_ai.span_kind"] == "agent_run"
    assert attributes["pydantic_ai.run.prompt"] == prompt
    assert attributes["pydantic_ai.run.result"] == output
    assert attributes["input.value"] == prompt
    assert attributes["output.value"] == output
    # safe_serialize produces bounded text (even structured values may truncate).
    assert attributes["input.mime_type"] == "text/plain"
    assert attributes["output.mime_type"] == "text/plain"
    assert attributes["gen_ai.span.kind"] == "AGENT"


@pytest.mark.parametrize("method", ["run_sync", "run"])
@pytest.mark.parametrize("result_field", ["output", "data"])
@pytest.mark.parametrize("keyword_prompt", [False, True])
def test_root_run_exports_collector_content(
    traced_agent, method, result_field, keyword_prompt
):
    Agent, exporter = traced_agent
    expected = SimpleNamespace(**{result_field: "answer"})
    agent = Agent(expected)
    deps = object()
    if keyword_prompt:
        result = invoke(agent, method, user_prompt="question", deps=deps)
    else:
        result = invoke(agent, method, "question", deps=deps)

    assert result is expected
    assert agent.calls == [("question", {"deps": deps})]
    (span,) = exporter.get_finished_spans()
    assert span.parent is None
    assert_content_contract(span, "question", "answer", method)


@pytest.mark.parametrize("method", ["run_sync", "run"])
def test_nested_runs_export_independent_collector_content(traced_agent, method):
    Agent, exporter = traced_agent
    child_result = SimpleNamespace(output="child answer")
    parent_result = SimpleNamespace(output="parent answer")
    child = Agent(child_result)
    parent = Agent(parent_result, child=child)

    assert invoke(parent, method, "parent question") is parent_result
    assert parent.child_result is child_result
    nested, outer = exporter.get_finished_spans()
    assert outer.parent is None
    assert nested.parent.span_id == outer.context.span_id
    assert nested.context.trace_id == outer.context.trace_id
    assert_content_contract(nested, "child question", "child answer", "run")
    assert_content_contract(outer, "parent question", "parent answer", method)


@pytest.mark.parametrize("method", ["run_sync", "run"])
@pytest.mark.parametrize(
    "output, serialized",
    [
        pytest.param("", "", id="empty-text"),
        pytest.param(False, "False", id="false"),
        pytest.param(0, "0", id="zero"),
        pytest.param({"answer": 42}, '{"answer": 42}', id="mapping"),
        pytest.param(["answer"], '["answer"]', id="list"),
        pytest.param("a" * 2500, "a" * 1997 + "...", id="bounded-text"),
    ],
)
def test_canonical_content_reuses_compatibility_serialization(
    traced_agent, method, output, serialized
):
    Agent, exporter = traced_agent
    expected = SimpleNamespace(output=output)
    agent = Agent(expected)
    prompt = "q" * 2500

    assert invoke(agent, method, user_prompt=prompt) is expected
    assert agent.calls == [(prompt, {})]
    (span,) = exporter.get_finished_spans()
    assert_content_contract(span, "q" * 1997 + "...", serialized, method)


@pytest.mark.parametrize("method", ["run_sync", "run"])
def test_failed_run_exports_input_and_kind_without_inventing_output(
    traced_agent, method
):
    Agent, exporter = traced_agent
    error = ValueError("fake agent failure")
    agent = Agent(None, error=error)

    with pytest.raises(ValueError) as caught:
        invoke(agent, method, "question")

    assert caught.value is error
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert span.attributes["pydantic_ai.run.prompt"] == "question"
    assert span.attributes["input.value"] == "question"
    assert span.attributes["input.mime_type"] == "text/plain"
    assert span.attributes["gen_ai.span.kind"] == "AGENT"
    assert "output.value" not in span.attributes
    assert "output.mime_type" not in span.attributes
    assert "pydantic_ai.run.result" not in span.attributes
