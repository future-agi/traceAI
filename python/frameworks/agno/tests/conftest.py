"""Pytest fixtures for traceai-agno tests."""

import pytest

import gzip
import sysconfig
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import httpx
from agno.agent import Agent
from agno.models.openai import OpenAIChat
from openai import OpenAI
from openinference.instrumentation.agno import AgnoInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_agno import AgnoInstrumentorWrapper
from unittest.mock import MagicMock, patch
from typing import Any, Dict, List, Optional


def pytest_addoption(parser):
    parser.addoption(
        "--require-installed-package",
        action="store_true",
        help="Fail if traceai_agno is imported from outside this environment's site-packages.",
    )


@pytest.fixture(scope="session", autouse=True)
def require_installed_package(request):
    if not request.config.getoption("--require-installed-package"):
        return

    import traceai_agno

    imported = Path(traceai_agno.__file__).resolve()
    expected = (
        Path(sysconfig.get_path("purelib")) / "traceai_agno" / "__init__.py"
    ).resolve()
    assert imported == expected, (
        f"Expected installed traceai_agno at {expected}, imported {imported}. "
        "Run pytest with --import-mode=importlib to prevent checkout shadowing."
    )


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


@pytest.fixture
def tracing():
    delegate = AgnoInstrumentor()
    if delegate.is_instrumented_by_opentelemetry:
        delegate.uninstrument()
    AgnoInstrumentorWrapper._instance = None
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider, exporter
    if delegate.is_instrumented_by_opentelemetry:
        delegate.uninstrument()
    AgnoInstrumentorWrapper._instance = None
    provider.shutdown()


@pytest.fixture
def agent():
    def respond(request):
        assert request.url.host == "model.invalid"
        return httpx.Response(200, json={
            "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
            "model": "gpt-4o-mini", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "Paris"},
                "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14},
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = OpenAI(api_key="synthetic-test-key", base_url="https://model.invalid/v1", http_client=http)
        yield Agent(model=OpenAIChat(id="gpt-4o-mini", client=client), telemetry=False)


@pytest.fixture
def receiver():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Encoding") == "gzip":
                payload = gzip.decompress(payload)
            requests.append((self.path, dict(self.headers), payload))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/tracer/v1/traces", requests
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
