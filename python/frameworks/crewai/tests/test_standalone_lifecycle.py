"""Version-gated public Agent/Flow execution, not fabricated wrapper calls."""

import asyncio
import inspect
import json
from contextlib import nullcontext
from importlib.metadata import version
from pathlib import Path

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.trace import StatusCode

import test_provider_composition as composition_tests

# Register the shared fixture without importing another module's test functions.
provider_composition = composition_tests.provider_composition

MODERN = tuple(int(n) for n in version("crewai").split(".")[:2]) >= (1, 15)
modern = pytest.mark.skipif(
    not MODERN, reason="Public standalone APIs require modern CrewAI"
)


def execute(obj, api, *args, **kwargs):
    result = getattr(obj, api)(*args, **kwargs)
    return asyncio.run(result) if inspect.isawaitable(result) else result


@modern
@pytest.mark.parametrize("api", ["kickoff", "kickoff_async", "akickoff"])
@pytest.mark.parametrize("hidden", [False, True])
def test_standalone_agent_lifecycle(provider_composition, api, hidden):
    build, inst, _, provider, memory, calls, _ = provider_composition
    baseline = execute(build().agents[0], api, "positive-input")
    assert "positive-output" in baseline.raw and not memory.get_finished_spans()
    inst.instrument(
        tracer_provider=provider,
        config=TraceConfig(hide_inputs=hidden, hide_outputs=hidden),
    )
    with provider.get_tracer("caller").start_as_current_span("caller") as parent:
        result = execute(build().agents[0], api, "positive-input")
        assert result.raw == baseline.raw and len(calls) == 2
        spans = memory.get_finished_spans()
        # AgentExecutor's private Flow must not produce duplicate workflow spans.
        assert len(spans) == 1
        span = spans[0]
        assert span.attributes["gen_ai.span.kind"] == "AGENT"
        assert span.name.startswith("Agent.")
        assert span.parent.span_id == parent.get_span_context().span_id
        assert span.status.status_code is StatusCode.OK
        payload = json.dumps(dict(span.attributes))
        assert ("positive-input" in payload) is not hidden
        assert ("positive-output" in payload) is not hidden
        assert "synthetic-provider-key" not in payload
        assert not any(k.startswith("gen_ai.usage.") for k in span.attributes)


@modern
@pytest.mark.parametrize("api", ["kickoff", "kickoff_async", "akickoff"])
def test_agent_error_suppression_and_restoration(provider_composition, api):
    from crewai import Agent

    build, inst, _, provider, memory, _, state = provider_composition
    originals = {n: getattr(Agent, n) for n in ("kickoff", "kickoff_async", "akickoff")}
    inst.instrument(tracer_provider=provider)
    with suppress_instrumentation():
        assert (
            "positive-output" in execute(build().agents[0], api, "positive-input").raw
        )
    assert not memory.get_finished_spans()
    state["fail"] = True
    with pytest.raises(Exception):
        execute(build().agents[0], api, "positive-input")
    spans = memory.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code is StatusCode.ERROR
    assert spans[0].end_time and not spans[0].events and not spans[0].status.description
    inst.uninstrument()
    assert all(getattr(Agent, name) is original for name, original in originals.items())
    memory.clear()
    state["fail"] = False
    assert "positive-output" in execute(build().agents[0], api, "positive-input").raw
    assert not memory.get_finished_spans()


@modern
@pytest.mark.parametrize("api", ["kickoff", "kickoff_async"])
@pytest.mark.parametrize("hidden", [False, True])
def test_flow_lifecycle_and_explicit_projection(provider_composition, api, hidden):
    from crewai.flow.flow import Flow, start

    _, inst, _, provider, memory, _, _ = provider_composition

    class ApplicationFlow(Flow):
        @start()
        def run(self):
            return {"answer": "positive-output", "api_key": "synthetic-output-key"}

    # Restoring inherited methods must not add overrides to the public class.
    originals = {cls: dict(vars(cls)) for cls in Flow.__mro__ if cls is not object}
    baseline = execute(ApplicationFlow(), api, inputs={"question": "positive-input"})
    assert not memory.get_finished_spans()
    inst.instrument(
        tracer_provider=provider,
        config=TraceConfig(hide_inputs=hidden, hide_outputs=hidden),
    )
    with provider.get_tracer("caller").start_as_current_span("caller") as parent:
        flow = ApplicationFlow()
        result = execute(
            flow,
            api,
            inputs={"question": "positive-input", "api_key": "synthetic-input-key"},
        )
        assert result == baseline
        spans = memory.get_finished_spans()
        assert len(spans) == 2
        span = next(s for s in spans if s.name == "ApplicationFlow.kickoff_async")
        node = next(s for s in spans if s.attributes.get("flow.node.name") == "run")
        assert node.parent.span_id == span.context.span_id
        assert node.attributes["flow.node.type"] == "start"
        assert span.name.startswith("ApplicationFlow.")
        assert span.attributes["gen_ai.span.kind"] == "CHAIN"
        assert span.parent.span_id == parent.get_span_context().span_id
        assert span.status.status_code is StatusCode.OK
        payload = json.dumps(dict(span.attributes))
        assert ("positive-input" in payload) is not hidden
        assert ("positive-output" in payload) is not hidden
        assert "synthetic-input-key" not in payload
        assert "synthetic-output-key" not in payload
    inst.uninstrument()
    for cls, attrs in originals.items():
        for name in ("kickoff", "kickoff_async", "akickoff", "_execute_method"):
            assert vars(cls).get(name) is attrs.get(name)


@modern
@pytest.mark.parametrize("api", ["kickoff", "kickoff_async"])
def test_flow_failure_suppression_and_restoration(provider_composition, api):
    from crewai.flow.flow import Flow, start

    _, inst, _, provider, memory, _, _ = provider_composition
    marker = ValueError("synthetic-private-error")

    class FailingFlow(Flow):
        @start()
        async def run(self):
            raise marker

    inst.instrument(tracer_provider=provider)
    for suppressed in (True, False):
        with suppress_instrumentation() if suppressed else nullcontext():
            with pytest.raises(ValueError) as caught:
                execute(FailingFlow(), api)
            assert caught.value is marker
        spans = memory.get_finished_spans()
        assert len(spans) == (0 if suppressed else 2)
    for span in spans:
        assert span.status.status_code is StatusCode.ERROR
        assert span.end_time and not span.events and not span.status.description
    inst.uninstrument()
    memory.clear()
    with pytest.raises(ValueError):
        execute(FailingFlow(), api)
    assert not memory.get_finished_spans()


@modern
def test_flow_agent_auto_async_parentage_and_internal_dedup(provider_composition):
    from crewai.flow.flow import Flow, start

    build, inst, llm_inst, provider, memory, calls, _ = provider_composition
    if llm_inst is None:
        pytest.skip("Install traceAI-openai to test provider composition")

    class UserFlow(Flow):
        @start()
        def run(self):
            # The sync Agent API returns an awaitable when invoked by a Flow.
            return build().agents[0].kickoff("positive-input")

    inst.instrument(tracer_provider=provider)
    llm_inst.instrument(tracer_provider=provider)
    assert "positive-output" in UserFlow().kickoff().raw
    spans = memory.get_finished_spans()
    flows = [s for s in spans if s.name == "UserFlow.kickoff_async"]
    nodes = [s for s in spans if "flow.node.name" in s.attributes]
    agents = [s for s in spans if s.attributes.get("gen_ai.span.kind") == "AGENT"]
    llms = [s for s in spans if s.attributes.get("gen_ai.span.kind") == "LLM"]
    assert len(flows) == len(nodes) == len(agents) == len(llms) == len(calls) == 1
    assert len(spans) == 4  # No private AgentExecutor root or node spans.
    assert nodes[0].parent.span_id == flows[0].context.span_id
    assert agents[0].parent.span_id == nodes[0].context.span_id
    assert llms[0].parent.span_id == agents[0].context.span_id
    assert flows[0].start_time <= nodes[0].start_time <= agents[0].start_time <= llms[0].start_time
    assert llms[0].end_time <= agents[0].end_time <= nodes[0].end_time <= flows[0].end_time


@modern
def test_nested_user_flows_are_not_deduplicated(provider_composition):
    from crewai.flow.flow import Flow, start

    _, inst, _, provider, memory, _, _ = provider_composition

    class Child(Flow):
        @start()
        async def run(self):
            return "positive-output"

    class Parent(Flow):
        @start()
        async def run(self):
            return await Child().kickoff_async(inputs={"child": "positive-input"})

    inst.instrument(tracer_provider=provider)
    assert Parent().kickoff() == "positive-output"
    spans = memory.get_finished_spans()
    assert len(spans) == 4
    child = next(s for s in spans if s.name == "Child.kickoff_async")
    parent = next(s for s in spans if s.name == "Parent.kickoff_async")
    parent_node = next(s for s in spans if s.name == "Parent.run")
    child_node = next(s for s in spans if s.name == "Child.run")
    assert parent_node.parent.span_id == parent.context.span_id
    assert child.parent.span_id == parent_node.context.span_id
    assert child_node.parent.span_id == child.context.span_id


@modern
def test_cancelled_async_flow_ends_span(provider_composition):
    from crewai.flow.flow import Flow, start

    _, inst, _, provider, memory, _, _ = provider_composition

    async def scenario():
        entered = asyncio.Event()

        class WaitingFlow(Flow):
            @start()
            async def run(self):
                entered.set()
                await asyncio.Event().wait()

        inst.instrument(tracer_provider=provider)
        task = asyncio.create_task(WaitingFlow().kickoff_async())
        await asyncio.wait_for(entered.wait(), 5)
        assert not memory.get_finished_spans()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    spans = memory.get_finished_spans()
    assert len(spans) == 2
    node = next(s for s in spans if s.attributes.get("flow.node.name") == "run")
    root = next(s for s in spans if s.name == "WaitingFlow.kickoff_async")
    assert node.parent.span_id == root.context.span_id
    for span in spans:
        assert span.status.status_code is StatusCode.ERROR
        assert not span.events and span.end_time


@modern
@pytest.mark.parametrize("api", ["kickoff", "kickoff_async", "akickoff"])
@pytest.mark.parametrize("suppressed", [False, True])
def test_lazy_flow_uses_consuming_context(provider_composition, api, suppressed):
    from crewai.flow.flow import Flow, start

    _, inst, _, provider, memory, _, _ = provider_composition
    executed = []

    class StreamingFlow(Flow):
        @start()
        def run(self):
            executed.append(True)
            return "positive-output"

    async def scenario():
        inst.instrument(tracer_provider=provider)
        flow = StreamingFlow(stream=True)
        with provider.get_tracer("caller").start_as_current_span("factory"):
            stream = flow.kickoff() if api == "kickoff" else await getattr(flow, api)()
        assert not executed
        assert not [
            s
            for s in memory.get_finished_spans()
            if s.attributes.get("gen_ai.span.kind")
        ]
        with provider.get_tracer("caller").start_as_current_span("consumer") as parent:
            with suppress_instrumentation() if suppressed else nullcontext():
                if api == "kickoff":
                    list(stream)
                else:
                    _ = [chunk async for chunk in stream]
            spans = [
                s
                for s in memory.get_finished_spans()
                if s.attributes.get("gen_ai.span.kind")
            ]
            assert len(spans) == (0 if suppressed else 2)
            if spans:
                root = next(s for s in spans if s.name == "StreamingFlow.kickoff_async")
                node = next(s for s in spans if s.attributes.get("flow.node.name") == "run")
                assert root.parent.span_id == parent.get_span_context().span_id
                assert node.parent.span_id == root.context.span_id
                for span in spans:
                    assert span.status.status_code is StatusCode.OK
                    assert "positive-output" in span.attributes["output.value"]
        assert executed == [True] and flow.stream is True

    asyncio.run(scenario())


@modern
@pytest.mark.parametrize("api", ["kickoff", "kickoff_async", "akickoff"])
def test_agent_cancellation_preserves_inflight_tool(provider_composition, api):
    import threading

    from crewai.tools import tool

    build, inst, _, provider, memory, calls, _ = provider_composition

    async def scenario(instrumented):
        entered, release, finished = (threading.Event() for _ in range(3))

        @tool
        def wait_for_release(query: str) -> str:
            """Finish the native worker when released by the application."""
            entered.set()
            try:
                assert release.wait(10), "test did not release the tool worker"
                return query
            finally:
                finished.set()

        agent = build().agents[0]
        agent.tools = [wait_for_release]
        if instrumented:
            inst.instrument(tracer_provider=provider)
        try:
            with provider.get_tracer("caller").start_as_current_span("consumer") as parent:
                task = asyncio.create_task(getattr(agent, api)("positive-input"))
                assert await asyncio.to_thread(entered.wait, 5)
                assert not memory.get_finished_spans()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
                assert not finished.is_set()  # Native cancellation cannot stop this thread.
                spans = memory.get_finished_spans()
                assert len(spans) == (1 if instrumented else 0)
                if instrumented:
                    agent_span = spans[0]
                    assert agent_span.attributes["gen_ai.span.kind"] == "AGENT"
                    assert agent_span.parent.span_id == parent.get_span_context().span_id
                    assert agent_span.status.status_code is StatusCode.ERROR
                    assert agent_span.end_time and not agent_span.events
                    assert not agent_span.status.description
                release.set()
                assert await asyncio.to_thread(finished.wait, 5)
        finally:
            # Joining asyncio's executor must never depend on a cancelled tool.
            release.set()

    asyncio.run(scenario(False))
    memory.clear()
    asyncio.run(scenario(True))
    assert len(calls) == 2
    spans = memory.get_finished_spans()
    tool_span = next(s for s in spans if s.attributes.get("gen_ai.span.kind") == "TOOL")
    agent_span = next(s for s in spans if s.attributes.get("gen_ai.span.kind") == "AGENT")
    assert tool_span.parent.span_id == agent_span.context.span_id
    assert tool_span.status.status_code is StatusCode.OK and tool_span.end_time
    assert not [s for s in spans if s.attributes.get("gen_ai.span.kind") == "CHAIN"]


@modern
@pytest.mark.parametrize("api", ["kickoff_async", "akickoff"])
def test_concurrent_agent_calls_keep_their_parents(provider_composition, api):
    build, inst, _, provider, memory, calls, _ = provider_composition
    inst.instrument(tracer_provider=provider)

    async def run(index):
        with provider.get_tracer("caller").start_as_current_span(
            f"caller-{index}"
        ) as parent:
            result = await getattr(build().agents[0], api)(f"positive-input-{index}")
            assert "positive-output" in result.raw
            return index, parent.get_span_context().span_id

    async def scenario():
        return await asyncio.gather(run(1), run(2))

    parents = dict(asyncio.run(scenario()))
    spans = [
        s
        for s in memory.get_finished_spans()
        if s.attributes.get("gen_ai.span.kind") == "AGENT"
    ]
    assert len(spans) == len(calls) == 2
    for span in spans:
        index = 1 if "positive-input-1" in span.attributes["input.value"] else 2
        assert span.parent.span_id == parents[index]


def test_readme_documents_complete_inventory_and_composition():
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    for name in (
        "Task._aexecute_core",
        "Crew.akickoff",
        "CrewStructuredTool.invoke",
        "CrewStructuredTool.ainvoke",
        "BaseTool.run",
        "BaseTool.arun",
        "Tool.run",
        "Tool.arun",
        "Agent.kickoff",
        "Agent.kickoff_async",
        "Flow.kickoff",
        "Flow.kickoff_async",
        "OpenAIInstrumentor",
        "consuming context",
    ):
        assert name in readme
