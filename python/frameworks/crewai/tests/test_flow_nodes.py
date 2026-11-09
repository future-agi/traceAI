"""Real Flow node boundaries and hierarchy; no provider calls or Flow-state export."""

import asyncio
import json
from importlib.metadata import version

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from packaging.version import Version

from traceai_crewai import CrewAIInstrumentor

pytestmark = pytest.mark.skipif(
    Version(version("crewai")) < Version("1.15.0"),
    reason="Flow node contract requires modern CrewAI; absent on the 0.41.1 floor",
)


@pytest.fixture
def traced_flow():
    instrumentor = CrewAIInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    try:
        yield instrumentor, provider, memory
    finally:
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
        provider.shutdown()


def node_spans(memory):
    return {
        span.attributes["flow.node.name"]: span
        for span in memory.get_finished_spans()
        if "flow.node.name" in span.attributes
    }


def test_multistep_flow_exposes_each_node(traced_flow):
    from crewai.flow.flow import Flow, listen, start

    instrumentor, provider, memory = traced_flow

    class Pipeline(Flow):
        @start()
        def retrieve(self):
            return {"document": "retrieved input"}

        @listen(retrieve)
        async def summarize(self, document):
            await asyncio.sleep(0)
            return {"answer": document["document"] + " summarized"}

    baseline = Pipeline().kickoff()
    instrumentor.instrument(tracer_provider=provider)
    with provider.get_tracer("caller").start_as_current_span("caller") as caller:
        assert Pipeline().kickoff() == baseline
        nodes = node_spans(memory)
        assert set(nodes) == {"retrieve", "summarize"}
        roots = [s for s in memory.get_finished_spans() if "flow.node.name" not in s.attributes]
        assert len(roots) == 1
        root = roots[0]
        assert root.parent.span_id == caller.get_span_context().span_id
        for name, node in nodes.items():
            assert name in node.name
            assert node.attributes["flow.node.type"] == ("start" if name == "retrieve" else "listen")
            assert node.attributes["gen_ai.span.kind"] == "CHAIN"
            assert node.parent.span_id == root.context.span_id
            assert node.status.status_code is StatusCode.OK
            assert root.start_time <= node.start_time <= node.end_time <= root.end_time
        assert json.loads(nodes["retrieve"].attributes["output.value"]) == {"document": "retrieved input"}
        assert "retrieved input" in nodes["summarize"].attributes["input.value"]
        assert json.loads(nodes["summarize"].attributes["output.value"]) == baseline


@pytest.mark.parametrize("route", ["selected", "alternate"])
def test_router_only_traces_the_executed_branch(traced_flow, route):
    from crewai.flow.flow import Flow, listen, router, start

    instrumentor, provider, memory = traced_flow

    class Branching(Flow):
        @start()
        def retrieve(self):
            return "document"

        @router(retrieve)
        def choose(self, document):
            assert document == "document"
            return route

        @listen("selected")
        def selected_handler(self):
            return "selected-result"

        @listen("alternate")
        async def alternate_handler(self):
            return "alternate-result"

    baseline = Branching().kickoff()
    instrumentor.instrument(tracer_provider=provider)
    assert Branching().kickoff() == baseline == route + "-result"
    nodes = node_spans(memory)
    assert set(nodes) == {"retrieve", "choose", route + "_handler"}
    assert nodes["choose"].attributes["flow.node.type"] == "router"
    assert nodes[route + "_handler"].attributes["flow.node.type"] == "listen"
    assert json.loads(nodes["choose"].attributes["output.value"]) == route
    root = next(s for s in memory.get_finished_spans() if s.name == "Branching.kickoff_async")
    assert all(s.parent.span_id == root.context.span_id for s in nodes.values())


@pytest.mark.parametrize("async_node", [False, True])
def test_failing_listener_preserves_exception_and_node_status(traced_flow, async_node):
    from crewai.flow.flow import Flow, listen, start

    instrumentor, provider, memory = traced_flow
    marker = ValueError("synthetic-private-error")

    class Failing(Flow):
        @start()
        def retrieve(self):
            return "document"

        if async_node:
            @listen(retrieve)
            async def fail(self, document):
                await asyncio.sleep(0)
                raise marker
        else:
            @listen(retrieve)
            def fail(self, document):
                raise marker

    with pytest.raises(ValueError) as baseline:
        Failing().kickoff()
    instrumentor.instrument(tracer_provider=provider)
    with pytest.raises(ValueError) as caught:
        Failing().kickoff()
    assert caught.value is baseline.value is marker
    spans = memory.get_finished_spans()
    assert len(spans) == 3
    nodes = node_spans(memory)
    assert nodes["retrieve"].status.status_code is StatusCode.OK
    assert nodes["fail"].status.status_code is StatusCode.ERROR
    assert spans[-1].status.status_code is StatusCode.ERROR
    for span in spans:
        assert span.end_time and not span.events and not span.status.description
        assert "synthetic-private-error" not in json.dumps(dict(span.attributes))


@pytest.mark.parametrize("hidden", [
    None, "FI_HIDE_INPUTS", "FI_HIDE_INPUT_TEXT", "FI_HIDE_INPUT_MESSAGES",
    "FI_HIDE_OUTPUTS", "FI_HIDE_OUTPUT_TEXT", "FI_HIDE_OUTPUT_MESSAGES",
])
def test_node_projection_excludes_state_and_honors_hide_flags(traced_flow, monkeypatch, hidden):
    from crewai.flow.flow import Flow, listen, start

    instrumentor, provider, memory = traced_flow
    if hidden:
        monkeypatch.setenv(hidden, "true")
    returned = ({"answer": "application-output", "api_key": "synthetic-output-key"}, "tuple-tail")

    class Private(Flow):
        @start()
        def retrieve(self):
            self.state["runtime_configuration"] = "synthetic-state-secret"
            return {"document": "application-input", "api_key": "synthetic-input-key"}

        @listen(retrieve)
        def summarize(self, document):
            assert document["api_key"] == "synthetic-input-key"
            return returned

    instrumentor.instrument(tracer_provider=provider, config=TraceConfig())
    assert Private().kickoff() is returned
    nodes = node_spans(memory)
    assert set(nodes) == {"retrieve", "summarize"}
    node = nodes["summarize"]
    hide_inputs = bool(hidden and "INPUT" in hidden)
    hide_outputs = bool(hidden and "OUTPUT" in hidden)
    assert ("application-input" in node.attributes.get("input.value", "")) is not hide_inputs
    assert ("application-output" in node.attributes.get("output.value", "")) is not hide_outputs
    if not hide_outputs:
        assert json.loads(node.attributes["output.value"]) == [
            {"answer": "application-output", "api_key": "__REDACTED__"}, "tuple-tail"
        ]
    for span in memory.get_finished_spans():
        payload = json.dumps(dict(span.attributes))
        for value in ("synthetic-state-secret", "synthetic-input-key", "synthetic-output-key"):
            assert value not in payload
        assert not any(key.startswith("flow.state") for key in span.attributes)


def test_concurrent_flows_and_parallel_listeners_keep_root_parentage(traced_flow):
    from crewai.flow.flow import Flow, listen, start

    instrumentor, provider, memory = traced_flow
    instrumentor.instrument(tracer_provider=provider)

    async def scenario():
        all_entered = asyncio.Event()
        entered = []

        async def rendezvous(label):
            entered.append(label)
            if len(entered) == 4:
                all_entered.set()
            await asyncio.wait_for(all_entered.wait(), 5)
            return label

        class Concurrent(Flow):
            @start()
            async def retrieve(self):
                return self.state["label"]

            @listen(retrieve)
            async def first(self, label):
                return await rendezvous(label)

            @listen(retrieve)
            async def second(self, label):
                return await rendezvous(label)

        async def run(label):
            with provider.get_tracer("caller").start_as_current_span(label) as parent:
                assert await Concurrent().kickoff_async(inputs={"label": label}) == label
                return parent.get_span_context().span_id

        parents = await asyncio.gather(run("one"), run("two"))
        assert sorted(entered) == ["one", "one", "two", "two"]
        return parents

    parents = asyncio.run(scenario())
    spans = memory.get_finished_spans()
    roots = [s for s in spans if s.name == "Concurrent.kickoff_async"]
    assert len(roots) == 2
    assert {root.parent.span_id for root in roots} == set(parents)
    nodes = [s for s in spans if "flow.node.name" in s.attributes]
    assert len(nodes) == 6
    for root in roots:
        label = json.loads(root.attributes["input.value"])["label"]
        children = [s for s in nodes if s.parent.span_id == root.context.span_id]
        assert {s.attributes["flow.node.name"] for s in children} == {"retrieve", "first", "second"}
        for child in children:
            assert child.context.trace_id == root.context.trace_id
            assert json.loads(child.attributes["output.value"]) == label
            assert root.start_time <= child.start_time <= child.end_time <= root.end_time
        listeners = [s for s in children if s.attributes["flow.node.type"] == "listen"]
        assert max(s.start_time for s in listeners) < min(s.end_time for s in listeners)


def test_user_flow_named_agent_executor_is_not_suppressed(traced_flow):
    from crewai.flow.flow import Flow, start

    instrumentor, provider, memory = traced_flow

    class AgentExecutor(Flow):
        @start()
        def run(self):
            return "user result"

    instrumentor.instrument(tracer_provider=provider)
    assert AgentExecutor().kickoff() == "user result"
    assert len(memory.get_finished_spans()) == 2
    assert set(node_spans(memory)) == {"run"}


def test_node_suppression_and_exact_defining_owner_restoration(traced_flow):
    from crewai.flow.flow import Flow, listen, start

    instrumentor, provider, memory = traced_flow
    originals = {cls: dict(vars(cls)) for cls in Flow.__mro__}

    class Pipeline(Flow):
        @start()
        def retrieve(self):
            return "document"

        @listen(retrieve)
        def summarize(self, document):
            return document + " summarized"

    for _ in range(2):
        instrumentor.instrument(tracer_provider=provider)
        with suppress_instrumentation():
            assert Pipeline().kickoff() == "document summarized"
        assert not memory.get_finished_spans()
        assert Pipeline().kickoff() == "document summarized"
        assert len(memory.get_finished_spans()) == 3
        instrumentor.uninstrument()
        for cls, attrs in originals.items():
            for name in ("kickoff_async", "_execute_method"):
                assert vars(cls).get(name) is attrs.get(name)
                assert (name in vars(cls)) == (name in attrs)
        memory.clear()
        assert Pipeline().kickoff() == "document summarized"
        assert not memory.get_finished_spans()
