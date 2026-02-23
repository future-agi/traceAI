"""Pytest fixtures for traceai-agno tests."""

import pytest
from unittest.mock import MagicMock, patch
from typing import Any, Dict, List, Optional


class MockModel:
    """Mock Agno model."""

    def __init__(
        self,
        model_id: str = "gpt-4",
        temperature: float = 0.7,
        max_tokens: int = 1000,
        top_p: float = 1.0,
    ):
        self.id = model_id
        self.model = model_id
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.top_p = top_p


class MockTool:
    """Mock Agno tool."""

    def __init__(
        self,
        name: str = "test_tool",
        description: str = "A test tool",
    ):
        self.name = name
        self.description = description

    def __call__(self, *args, **kwargs):
        return "Tool executed"


class MockAgent:
    """Mock Agno Agent."""

    def __init__(
        self,
        name: str = "TestAgent",
        agent_id: str = "agent-123",
        description: str = "A test agent",
        instructions: str = "Be helpful and accurate",
        model: Optional[MockModel] = None,
        tools: Optional[List[Any]] = None,
        debug_mode: bool = False,
        markdown: bool = True,
        memory: Optional[Any] = None,
        knowledge: Optional[Any] = None,
    ):
        self.name = name
        self.agent_id = agent_id
        self.description = description
        self.instructions = instructions
        self.model = model or MockModel()
        self.tools = tools or []
        self.debug_mode = debug_mode
        self.markdown = markdown
        self.memory = memory
        self.knowledge = knowledge

    def run(self, message: str) -> str:
        return f"Agent response to: {message}"

    async def arun(self, message: str) -> str:
        return f"Async agent response to: {message}"


class MockTeam:
    """Mock Agno Team."""

    def __init__(
        self,
        name: str = "TestTeam",
        agents: Optional[List[MockAgent]] = None,
    ):
        self.name = name
        self.agents = agents or []

    def run(self, message: str) -> str:
        return f"Team response to: {message}"


class MockWorkflow:
    """Mock Agno Workflow."""

    def __init__(self, name: str = "TestWorkflow"):
        self.name = name

    def run(self, *args, **kwargs):
        return "Workflow result"


class MockUsage:
    """Mock token usage."""

    def __init__(
        self,
        input_tokens: int = 100,
        output_tokens: int = 50,
    ):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.prompt_tokens = input_tokens
        self.completion_tokens = output_tokens


class MockResponse:
    """Mock LLM response."""

    def __init__(
        self,
        content: str = "Test response",
        usage: Optional[MockUsage] = None,
    ):
        self.content = content
        self.usage = usage or MockUsage()


class MockTracerProvider:
    """Mock OpenTelemetry TracerProvider."""

    def __init__(self):
        self.tracers = {}

    def get_tracer(self, name: str, version: str = None):
        if name not in self.tracers:
            self.tracers[name] = MockTracer(name)
        return self.tracers[name]

    def add_span_processor(self, processor):
        pass


class MockTracer:
    """Mock OpenTelemetry Tracer."""

    def __init__(self, name: str):
        self.name = name
        self.spans = []

    def start_span(self, name: str, **kwargs):
        span = MockSpan(name)
        self.spans.append(span)
        return span

    def start_as_current_span(self, name: str, **kwargs):
        return MockSpanContext(MockSpan(name))


class MockSpan:
    """Mock OpenTelemetry Span."""

    def __init__(self, name: str):
        self.name = name
        self.attributes = {}
        self.events = []
        self.status = None
        self._ended = False

    def set_attribute(self, key: str, value: Any):
        self.attributes[key] = value

    def set_attributes(self, attributes: Dict[str, Any]):
        self.attributes.update(attributes)

    def add_event(self, name: str, attributes: Dict[str, Any] = None):
        self.events.append({"name": name, "attributes": attributes or {}})

    def set_status(self, status):
        self.status = status

    def end(self):
        self._ended = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.end()


class MockSpanContext:
    """Mock span context manager."""

    def __init__(self, span: MockSpan):
        self.span = span

    def __enter__(self):
        return self.span

    def __exit__(self, *args):
        self.span.end()


class MockAgnoInstrumentor:
    """Mock AgnoInstrumentor from openinference."""

    _is_instrumented = False

    def __init__(self):
        self.tracer_provider = None

    def instrument(self, tracer_provider=None):
        self.tracer_provider = tracer_provider
        MockAgnoInstrumentor._is_instrumented = True

    def uninstrument(self):
        MockAgnoInstrumentor._is_instrumented = False


@pytest.fixture
def mock_model():
    """Create a mock Agno model."""
    return MockModel()


@pytest.fixture
def mock_tool():
    """Create a mock Agno tool."""
    return MockTool()


@pytest.fixture
def mock_agent():
    """Create a mock Agno agent."""
    return MockAgent()


@pytest.fixture
def mock_agent_with_tools():
    """Create a mock Agno agent with tools."""
    tools = [
        MockTool(name="search", description="Search the web"),
        MockTool(name="calculate", description="Perform calculations"),
    ]
    return MockAgent(
        name="ToolAgent",
        tools=tools,
        model=MockModel(model_id="claude-3-5-sonnet-20241022"),
    )


@pytest.fixture
def mock_team():
    """Create a mock Agno team."""
    agents = [
        MockAgent(name="Agent1"),
        MockAgent(name="Agent2"),
    ]
    return MockTeam(name="TestTeam", agents=agents)


@pytest.fixture
def mock_workflow():
    """Create a mock Agno workflow."""
    return MockWorkflow()


@pytest.fixture
def mock_response():
    """Create a mock LLM response."""
    return MockResponse()


@pytest.fixture
def mock_tracer_provider():
    """Create a mock tracer provider."""
    return MockTracerProvider()


@pytest.fixture
def mock_agno_instrumentor():
    """Create a mock AgnoInstrumentor."""
    return MockAgnoInstrumentor()


@pytest.fixture
def reset_instrumentor():
    """Reset the instrumentor singleton between tests."""
    from traceai_agno._instrumentor import AgnoInstrumentorWrapper

    # Reset singleton state
    AgnoInstrumentorWrapper._instance = None
    AgnoInstrumentorWrapper._is_instrumented = False
    yield
    # Cleanup after test
    AgnoInstrumentorWrapper._instance = None
    AgnoInstrumentorWrapper._is_instrumented = False
