"""Offline pyagentspec harness: the real SDK's Trace/Span/Event classes, no network."""
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pyagentspec.agent import Agent
from pyagentspec.flows.edges import ControlFlowEdge
from pyagentspec.flows.flow import Flow
from pyagentspec.flows.nodes import EndNode, StartNode
from pyagentspec.llms import LlmGenerationConfig, OpenAiCompatibleConfig
from pyagentspec.property import StringProperty
from pyagentspec.tools import ServerTool

SECRET_KEY = "synthetic-llm-key-DO-NOT-EXPORT"
SECRET_ERROR = "synthetic-error-text-DO-NOT-EXPORT"

LLM = OpenAiCompatibleConfig(
    name="main-llm", url="http://llm.test", model_id="gpt-served", api_key=SECRET_KEY
)
GENERATION = LlmGenerationConfig(temperature=0.2, max_tokens=64, top_p=0.9)
TOOL = ServerTool(
    name="lookup",
    description="Look a capital up",
    inputs=[StringProperty(title="country")],
    outputs=[StringProperty(title="capital")],
)
AGENT = Agent(name="geo-helper", description="Answers geography", llm_config=LLM, system_prompt="Be brief.", tools=[TOOL])
START, END = StartNode(name="start"), EndNode(name="end")
FLOW = Flow(
    name="geo-flow",
    start_node=START,
    nodes=[START, END],
    control_flow_connections=[ControlFlowEdge(name="go", from_node=START, to_node=END)],
)


def attrs(span):
    return dict(span.attributes)


def by_name(spans, name):
    found = [s for s in spans.get_finished_spans() if s.name == name]
    assert len(found) == 1, [s.name for s in spans.get_finished_spans()]
    return found[0]


@pytest.fixture
def provider():
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer_provider.exporter = exporter
    yield tracer_provider
    tracer_provider.shutdown()


@pytest.fixture
def instrument(provider):
    from traceai_agentspec import AgentSpecInstrumentor

    instrumentors = []

    def start(**kwargs):
        instrumentor = AgentSpecInstrumentor()
        instrumentor.instrument(tracer_provider=provider, **kwargs)
        instrumentors.append(instrumentor)
        return provider.exporter

    yield start
    for instrumentor in reversed(instrumentors):
        instrumentor.uninstrument()


@pytest.fixture
def spans(instrument):
    return instrument()
