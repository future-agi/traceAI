"""Pytest configuration for AutoGen tests."""

import sys
from unittest.mock import MagicMock
from enum import Enum
import pytest

# Mock opentelemetry first
mock_otel = MagicMock()
mock_otel_trace = MagicMock()
mock_otel_instrumentation = MagicMock()
mock_otel_instrumentation_instrumentor = MagicMock()

# Create mock classes
class MockStatusCode(Enum):
    OK = 1
    ERROR = 2
    UNSET = 0

class MockSpanKind(Enum):
    INTERNAL = 0
    CLIENT = 1
    SERVER = 2
    PRODUCER = 3
    CONSUMER = 4

mock_otel_trace.StatusCode = MockStatusCode
mock_otel_trace.SpanKind = MockSpanKind
mock_otel_trace.Status = MagicMock
mock_otel_trace.Link = MagicMock
mock_otel_trace.SpanContext = MagicMock
mock_otel_trace.get_tracer = MagicMock(return_value=MagicMock())
mock_otel_trace.get_tracer_provider = MagicMock(return_value=MagicMock())
mock_otel_trace.get_current_span = MagicMock(return_value=MagicMock())
mock_otel_trace.set_span_in_context = MagicMock()

# Create BaseInstrumentor mock
class MockBaseInstrumentor:
    def __init__(self):
        pass

    def instrument(self, **kwargs):
        self._instrument(**kwargs)

    def uninstrument(self, **kwargs):
        self._uninstrument(**kwargs)

    def _instrument(self, **kwargs):
        pass

    def _uninstrument(self, **kwargs):
        pass

mock_otel_instrumentation_instrumentor.BaseInstrumentor = MockBaseInstrumentor

# Register opentelemetry mocks
sys.modules["opentelemetry"] = mock_otel
sys.modules["opentelemetry.trace"] = mock_otel_trace
sys.modules["opentelemetry.instrumentation"] = mock_otel_instrumentation
sys.modules["opentelemetry.instrumentation.instrumentor"] = mock_otel_instrumentation_instrumentor

# Mock fi_instrumentation
mock_fi = MagicMock()

# Create a proper FITracer mock that doesn't cause spec issues
class MockFITracer:
    def __init__(self, tracer, config=None):
        self.tracer = tracer
        self.config = config

    def start_as_current_span(self, *args, **kwargs):
        return MagicMock()

mock_fi.FITracer = MockFITracer
mock_fi.TraceConfig = type("TraceConfig", (), {})
mock_fi.fi_types = MagicMock()

# Set up SpanAttributes with commonly used attributes
class MockSpanAttributes:
    GEN_AI_SPAN_KIND = "fi.span_kind"
    INPUT_VALUE = "fi.raw_input"
    INPUT_VALUE = "fi.input_value"
    INPUT_MIME_TYPE = "fi.input_mime_type"
    OUTPUT_VALUE = "fi.raw_output"
    OUTPUT_VALUE = "fi.output_value"
    OUTPUT_MIME_TYPE = "fi.output_mime_type"
    GEN_AI_TOOL_NAME = "fi.tool_name"
    TOOL_PARAMETERS = "fi.tool_parameters"
    TOOL_CALL_FUNCTION_ARGUMENTS = "fi.tool_call_function_arguments"
    TOOL_CALL_FUNCTION_NAME = "fi.tool_call_function_name"

mock_fi.fi_types.SpanAttributes = MockSpanAttributes

# Register fi_instrumentation mocks
sys.modules["fi_instrumentation"] = mock_fi
sys.modules["fi_instrumentation.fi_types"] = mock_fi.fi_types
