"""Pytest configuration and fixtures for traceai-beeai tests."""

import sys
from enum import Enum
from unittest.mock import MagicMock


# Mock fi_instrumentation module
class MockProjectType(Enum):
    OBSERVE = "observe"
    EXPERIMENT = "experiment"


mock_fi_types = MagicMock()
mock_fi_types.ProjectType = MockProjectType

mock_fi = MagicMock()
mock_fi.fi_types = mock_fi_types
mock_fi.register = MagicMock(return_value=MagicMock())

sys.modules["fi_instrumentation"] = mock_fi
sys.modules["fi_instrumentation.fi_types"] = mock_fi_types


# Mock opentelemetry modules
class MockStatusCode(Enum):
    OK = 1
    ERROR = 2
    UNSET = 0


class MockStatus:
    def __init__(self, status_code, description=None):
        self.status_code = status_code
        self.description = description


class MockSpanKind(Enum):
    INTERNAL = 0
    SERVER = 1
    CLIENT = 2
    PRODUCER = 3
    CONSUMER = 4


class MockSpan:
    def __init__(self, name="test_span", **kwargs):
        self.name = name
        self.attributes = kwargs.get("attributes", {})
        self._status = None
        self._ended = False
        self._exceptions = []

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def set_attributes(self, attributes):
        self.attributes.update(attributes)

    def set_status(self, status):
        self._status = status

    def record_exception(self, exception):
        self._exceptions.append(exception)

    def end(self):
        self._ended = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.end()


class MockTracer:
    def __init__(self, name="test_tracer", **kwargs):
        self.name = name
        self.spans = []

    def start_span(self, name, **kwargs):
        span = MockSpan(name=name, **kwargs)
        self.spans.append(span)
        return span

    def start_as_current_span(self, name, **kwargs):
        span = MockSpan(name=name, **kwargs)
        self.spans.append(span)
        return span


class MockTracerProvider:
    def __init__(self):
        self.tracers = {}

    def get_tracer(self, name, **kwargs):
        if name not in self.tracers:
            self.tracers[name] = MockTracer(name=name)
        return self.tracers[name]


class MockResource:
    @staticmethod
    def create(attributes):
        return MagicMock()


class MockSDKTracerProvider:
    def __init__(self, resource=None):
        self.resource = resource
        self.processors = []
        self.tracers = {}

    def add_span_processor(self, processor):
        self.processors.append(processor)

    def get_tracer(self, name, **kwargs):
        if name not in self.tracers:
            self.tracers[name] = MockTracer(name=name)
        return self.tracers[name]


# Create mock modules
mock_trace = MagicMock()
mock_trace.TracerProvider = MockTracerProvider
mock_trace.Span = MockSpan
mock_trace.SpanKind = MockSpanKind
mock_trace.Status = MockStatus
mock_trace.StatusCode = MockStatusCode
mock_trace.get_tracer_provider = MagicMock(return_value=MockTracerProvider())
mock_trace.set_tracer_provider = MagicMock()

sys.modules["opentelemetry"] = MagicMock()
sys.modules["opentelemetry.trace"] = mock_trace
sys.modules["opentelemetry.trace.status"] = MagicMock()
sys.modules["opentelemetry.trace.status"].Status = MockStatus
sys.modules["opentelemetry.trace.status"].StatusCode = MockStatusCode

# Mock SDK modules
mock_sdk = MagicMock()
mock_sdk_trace = MagicMock()
mock_sdk_trace.TracerProvider = MockSDKTracerProvider
sys.modules["opentelemetry.sdk"] = mock_sdk
sys.modules["opentelemetry.sdk.trace"] = mock_sdk_trace
sys.modules["opentelemetry.sdk.trace.export"] = MagicMock()
sys.modules["opentelemetry.sdk.resources"] = MagicMock()
sys.modules["opentelemetry.sdk.resources"].Resource = MockResource

# Mock OTLP exporter
sys.modules["opentelemetry.exporter.otlp"] = MagicMock()
sys.modules["opentelemetry.exporter.otlp.proto.grpc.trace_exporter"] = MagicMock()
sys.modules["opentelemetry.exporter.otlp.proto.http.trace_exporter"] = MagicMock()


# Mock OpenInference BeeAI instrumentor
class MockBeeAIInstrumentor:
    _instrumented = False

    def instrument(self):
        MockBeeAIInstrumentor._instrumented = True

    def uninstrument(self):
        MockBeeAIInstrumentor._instrumented = False


mock_openinference = MagicMock()
mock_openinference_beeai = MagicMock()
mock_openinference_beeai.BeeAIInstrumentor = MockBeeAIInstrumentor

sys.modules["openinference"] = mock_openinference
sys.modules["openinference.instrumentation"] = MagicMock()
sys.modules["openinference.instrumentation.beeai"] = mock_openinference_beeai


# Mock BeeAI framework
sys.modules["beeai_framework"] = MagicMock()
sys.modules["beeai_framework.agents"] = MagicMock()
sys.modules["beeai_framework.tools"] = MagicMock()


import pytest


@pytest.fixture
def mock_tracer_provider():
    """Create a mock tracer provider for testing."""
    return MockTracerProvider()


@pytest.fixture
def mock_tracer(mock_tracer_provider):
    """Create a mock tracer for testing."""
    return mock_tracer_provider.get_tracer("test")


@pytest.fixture
def mock_agent():
    """Create a mock BeeAI Agent for testing."""
    agent = MagicMock()
    agent.name = "test_agent"
    agent.role = "Assistant"
    agent.instructions = "You are a helpful assistant."
    agent.llm = MagicMock()
    agent.llm.model = "granite-3.1-8b-instruct"
    agent.tools = []
    agent.requirements = []
    agent.memory = None
    return agent


@pytest.fixture
def mock_tool():
    """Create a mock BeeAI tool for testing."""
    tool = MagicMock()
    tool.name = "calculator"
    tool.description = "Perform mathematical calculations."
    return tool


@pytest.fixture
def mock_response():
    """Create a mock agent response for testing."""
    response = MagicMock()
    response.content = "Hello! How can I help you today?"
    response.usage = MagicMock()
    response.usage.input_tokens = 100
    response.usage.output_tokens = 50
    return response
