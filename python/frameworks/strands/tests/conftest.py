"""Pytest configuration and fixtures for traceai-strands tests."""

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


# Create mock modules
mock_trace = MagicMock()
mock_trace.TracerProvider = MockTracerProvider
mock_trace.Span = MockSpan
mock_trace.SpanKind = MockSpanKind
mock_trace.Status = MockStatus
mock_trace.StatusCode = MockStatusCode
mock_trace.get_tracer_provider = MagicMock(return_value=MockTracerProvider())

sys.modules["opentelemetry"] = MagicMock()
sys.modules["opentelemetry.trace"] = mock_trace
sys.modules["opentelemetry.trace.status"] = MagicMock()
sys.modules["opentelemetry.trace.status"].Status = MockStatus
sys.modules["opentelemetry.trace.status"].StatusCode = MockStatusCode

# Mock SDK modules
mock_sdk = MagicMock()
mock_sdk_trace = MagicMock()
mock_sdk_trace.TracerProvider = MockTracerProvider
sys.modules["opentelemetry.sdk"] = mock_sdk
sys.modules["opentelemetry.sdk.trace"] = mock_sdk_trace

# Mock OTLP exporter
sys.modules["opentelemetry.exporter.otlp"] = MagicMock()
sys.modules["opentelemetry.exporter.otlp.proto.grpc.trace_exporter"] = MagicMock()
sys.modules["opentelemetry.exporter.otlp.proto.http.trace_exporter"] = MagicMock()


# Mock Strands modules
class MockStrandsTelemetry:
    def __init__(self, tracer_provider=None):
        self.tracer_provider = tracer_provider

    def setup_otlp_exporter(self, **kwargs):
        return self

    def setup_console_exporter(self, **kwargs):
        return self

    def setup_meter(self, **kwargs):
        return self


mock_strands_telemetry = MagicMock()
mock_strands_telemetry.StrandsTelemetry = MockStrandsTelemetry

sys.modules["strands"] = MagicMock()
sys.modules["strands.telemetry"] = mock_strands_telemetry


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
    """Create a mock Strands Agent for testing."""
    agent = MagicMock()
    agent.system_prompt = "You are a helpful assistant."
    agent.model = MagicMock()
    agent.model.model_id = "us.anthropic.claude-sonnet-4-20250514-v1:0"
    agent.tools = []
    agent.trace_attributes = {}
    return agent


@pytest.fixture
def mock_tool():
    """Create a mock Strands tool for testing."""
    def calculator(x: int, y: int) -> int:
        """Calculate the sum of two numbers."""
        return x + y

    return calculator


@pytest.fixture
def mock_response():
    """Create a mock agent response for testing."""
    response = MagicMock()
    response.content = "Hello! How can I help you today?"
    response.usage = MagicMock()
    response.usage.input_tokens = 100
    response.usage.output_tokens = 50
    return response
