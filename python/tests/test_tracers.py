"""Tests for fi_instrumentation.instrumentation._tracers module."""

import asyncio
import pytest
from typing import Dict, Any, List
from unittest.mock import patch, MagicMock, Mock
from opentelemetry.trace import Tracer, Span, SpanKind
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

from fi_instrumentation.instrumentation._tracers import (
    FITracer,
    _IdGenerator,
    _infer_span_name,
    _infer_tool_description,
    _infer_tool_parameters,
    _get_jsonschema_type,
)
from fi_instrumentation.instrumentation.config import TraceConfig
from fi_instrumentation.fi_types import FiSpanKindValues

# Move this to module level to apply to all tests
@pytest.fixture(autouse=True)
def mock_trace_components():
    """Mock OpenTelemetry components for all tests in this module."""
    with patch('opentelemetry.trace.use_span') as mock_use_span, \
         patch('opentelemetry.context.get_value') as mock_get_value, \
         patch('opentelemetry.trace.Tracer.start_span') as mock_tracer_start_span:
        
        # Create a persistent mock span
        mock_span = MagicMock(spec=Span)
        mock_span.set_attribute = MagicMock()
        mock_span.set_attributes = MagicMock()
        mock_span.end = MagicMock()
        mock_span.is_recording.return_value = True
        mock_span.get_span_context.return_value = MagicMock()
        
        # Mock context values to not suppress instrumentation
        mock_get_value.return_value = False
        
        # Mock the Tracer class method that FITracer actually calls
        mock_tracer_start_span.return_value = mock_span
        
        # Mock use_span context manager
        context_manager = MagicMock()
        context_manager.__enter__ = MagicMock(return_value=mock_span)
        context_manager.__exit__ = MagicMock(return_value=None)
        mock_use_span.return_value = context_manager
        
        yield mock_span


class TestIdGenerator:
    """Test _IdGenerator functionality."""

    def test_id_generator_span_id_generation(self):
        """Test span ID generation."""
        generator = _IdGenerator()
        span_id = generator.generate_span_id()
        
        assert isinstance(span_id, int)
        assert span_id > 0
        assert span_id != 0  # Should not be INVALID_SPAN_ID
        
        # Should generate different IDs
        span_id2 = generator.generate_span_id()
        assert span_id != span_id2

    def test_id_generator_trace_id_generation(self):
        """Test trace ID generation."""
        generator = _IdGenerator()
        trace_id = generator.generate_trace_id()
        
        assert isinstance(trace_id, int)
        assert trace_id > 0
        assert trace_id != 0  # Should not be INVALID_TRACE_ID
        
        # Should generate different IDs
        trace_id2 = generator.generate_trace_id()
        assert trace_id != trace_id2

    def test_id_generator_avoids_invalid_ids(self):
        """Test that generator avoids invalid IDs."""
        generator = _IdGenerator()
        
        # Generate multiple IDs to ensure we don't get invalid ones
        for _ in range(100):
            span_id = generator.generate_span_id()
            trace_id = generator.generate_trace_id()
            
            assert span_id != 0  # INVALID_SPAN_ID
            assert trace_id != 0  # INVALID_TRACE_ID


class TestFITracer:
    """Test FITracer functionality."""

    @pytest.fixture
    def mock_tracer(self):
        """Create a mock OpenTelemetry tracer."""
        from opentelemetry.trace import Span
        
        tracer = MagicMock(spec=Tracer)
        tracer.id_generator = RandomIdGenerator()
        
        # Create a proper mock span that won't cause NoneType errors
        mock_span = MagicMock(spec=Span)
        mock_span.set_attribute = MagicMock()
        mock_span.set_attributes = MagicMock()
        mock_span.end = MagicMock()
        mock_span.is_recording.return_value = True
        mock_span.get_span_context.return_value = MagicMock()
        
        # Make sure start_span returns the mock span
        tracer.start_span.return_value = mock_span
        
        # Mock the context manager properly
        context_manager = MagicMock()
        context_manager.__enter__ = MagicMock(return_value=mock_span)
        context_manager.__exit__ = MagicMock(return_value=None)
        tracer.start_as_current_span.return_value = context_manager
        
        return tracer

    @pytest.fixture  
    def fi_tracer(self, mock_tracer):
        """Create a FITracer instance with mocked dependencies."""
        config = TraceConfig()
        return FITracer(mock_tracer, config)

    def test_fi_tracer_initialization(self, mock_tracer):
        """Test FITracer initialization."""
        config = TraceConfig()
        tracer = FITracer(mock_tracer, config)
        
        assert tracer._self_config == config
        assert isinstance(tracer._self_id_generator, _IdGenerator)

    def test_fi_tracer_id_generator_property(self, fi_tracer, mock_tracer):
        """Test id_generator property."""
        # When wrapped tracer has RandomIdGenerator, should return custom generator
        mock_tracer.id_generator = RandomIdGenerator()
        assert isinstance(fi_tracer.id_generator, _IdGenerator)
        
        # When wrapped tracer has other generator, should return that
        custom_generator = MagicMock()
        mock_tracer.id_generator = custom_generator
        assert fi_tracer.id_generator == custom_generator

    def test_fi_tracer_start_span_basic(self, fi_tracer, mock_tracer):
        """Test basic span creation."""
        # FITracer calls the class method, not instance method
        span = fi_tracer.start_span("test_span")
        
        assert span is not None
        # The span should be a FiSpan wrapper
        assert hasattr(span, 'set_attributes')
        assert hasattr(span, 'set_attribute')

    def test_fi_tracer_start_span_with_attributes(self, fi_tracer, mock_tracer):
        """Test span creation with attributes."""
        attributes = {"key1": "value1", "key2": "value2"}
        span = fi_tracer.start_span("test_span", attributes=attributes)
        
        assert span is not None
        # The span should be a FiSpan wrapper that handles attributes
        assert hasattr(span, 'set_attributes')
        # Since we have comprehensive mocking, this should work without errors

    def test_fi_tracer_start_as_current_span(self, fi_tracer, mock_tracer):
        """Test start_as_current_span context manager."""
        # Use the already configured mock tracer  
        with fi_tracer.start_as_current_span("test_span") as span:
            assert span is not None

    def test_fi_tracer_agent_decorator_sync(self, fi_tracer):
        """Test agent decorator on synchronous function."""
        @fi_tracer.agent
        def test_function(x: int, y: int) -> int:
            return x + y
        
        result = test_function(2, 3)
        assert result == 5

    def test_fi_tracer_agent_decorator_with_name(self, fi_tracer):
        """Test agent decorator with custom name."""
        @fi_tracer.agent(name="custom_agent")
        def test_function(x: int) -> int:
            return x * 2
        
        result = test_function(5)
        assert result == 10

    @pytest.mark.asyncio
    async def test_fi_tracer_agent_decorator_async(self, fi_tracer):
        """Test agent decorator on asynchronous function."""
        @fi_tracer.agent
        async def async_test_function(x: int) -> int:
            await asyncio.sleep(0.01)  # Small delay
            return x * 3
        
        result = await async_test_function(4)
        assert result == 12

    def test_fi_tracer_chain_decorator_sync(self, fi_tracer):
        """Test chain decorator on synchronous function."""
        @fi_tracer.chain
        def test_chain_function(text: str) -> str:
            return text.upper()
        
        result = test_chain_function("hello")
        assert result == "HELLO"

    def test_fi_tracer_chain_decorator_with_name(self, fi_tracer):
        """Test chain decorator with custom name."""
        @fi_tracer.chain(name="custom_chain")
        def test_function(text: str) -> str:
            return text.lower()
        
        result = test_function("WORLD")
        assert result == "world"

    @pytest.mark.asyncio
    async def test_fi_tracer_chain_decorator_async(self, fi_tracer):
        """Test chain decorator on asynchronous function."""
        @fi_tracer.chain
        async def async_chain_function(text: str) -> str:
            await asyncio.sleep(0.01)
            return f"processed_{text}"
        
        result = await async_chain_function("input")
        assert result == "processed_input"

    def test_fi_tracer_tool_decorator_sync(self, fi_tracer):
        """Test tool decorator on synchronous function."""
        @fi_tracer.tool
        def calculator(a: int, b: int) -> int:
            """A simple calculator tool."""
            return a + b
        
        result = calculator(10, 20)
        assert result == 30

    def test_fi_tracer_tool_decorator_with_params(self, fi_tracer):
        """Test tool decorator with custom parameters."""
        @fi_tracer.tool(
            name="custom_calculator",
            description="Custom calculator tool",
            parameters={"type": "object", "properties": {"a": {"type": "integer"}}}
        )
        def calculator_with_params(a: int, b: int = 5) -> int:
            return a * b
        
        result = calculator_with_params(7)
        assert result == 35

    @pytest.mark.asyncio
    async def test_fi_tracer_tool_decorator_async(self, fi_tracer):
        """Test tool decorator on asynchronous function."""
        @fi_tracer.tool(description="Async tool for processing")
        async def async_tool(value: str) -> str:
            await asyncio.sleep(0.01)
            return f"async_{value}"
        
        result = await async_tool("test")
        assert result == "async_test"

    def test_fi_tracer_decorator_error_handling(self, fi_tracer):
        """Test that decorators properly handle exceptions."""
        @fi_tracer.agent
        def failing_function():
            raise ValueError("Test error")
        
        with pytest.raises(ValueError, match="Test error"):
            failing_function()

    @pytest.mark.asyncio
    async def test_fi_tracer_async_decorator_error_handling(self, fi_tracer):
        """Test that async decorators properly handle exceptions."""
        @fi_tracer.chain
        async def async_failing_function():
            raise RuntimeError("Async test error")
        
        with pytest.raises(RuntimeError, match="Async test error"):
            await async_failing_function()


class TestInferenceUtilities:
    """Test utility functions for inferring span information."""

    def test_infer_span_name_function(self):
        """Test inferring span name from function."""
        def test_function():
            pass
        
        name = _infer_span_name(instance=None, callable=test_function)
        assert name == "test_function"

    def test_infer_span_name_method(self):
        """Test inferring span name from method."""
        class TestClass:
            def test_method(self):
                pass
        
        instance = TestClass()
        name = _infer_span_name(instance=instance, callable=instance.test_method)
        assert name == "TestClass.test_method"

    def test_infer_span_name_lambda(self):
        """Test inferring span name from lambda."""
        lambda_func = lambda x: x * 2
        name = _infer_span_name(instance=None, callable=lambda_func)
        assert name == "<lambda>"

    def test_infer_tool_description_with_docstring(self):
        """Test inferring tool description from docstring."""
        def function_with_docstring(x: int) -> int:
            """This is a test function that doubles the input."""
            return x * 2
        
        description = _infer_tool_description(function_with_docstring)
        assert description == "This is a test function that doubles the input."

    def test_infer_tool_description_without_docstring(self):
        """Test inferring tool description when no docstring."""
        def function_without_docstring(x: int) -> int:
            return x * 2
        
        description = _infer_tool_description(function_without_docstring)
        assert description is None

    def test_infer_tool_description_multiline_docstring(self):
        """Test inferring tool description from multiline docstring."""
        def function_with_multiline_docstring(x: int) -> int:
            """
            This is a test function.

            It doubles the input value and returns the result.
            """
            return x * 2

        description = _infer_tool_description(function_with_multiline_docstring)
        # The function may return the full docstring or just the first line
        assert "This is a test function" in description

    def test_infer_tool_parameters_simple(self):
        """Test inferring tool parameters from simple function."""
        def simple_function(x: int, y: str) -> str:
            """A simple function."""
            return f"{y}_{x}"
        
        params = _infer_tool_parameters(
            callable=simple_function,
            tool_name="simple_function",
            tool_description="A simple function."
        )
        
        assert "type" in params
        assert params["type"] == "object"
        assert "properties" in params
        assert "x" in params["properties"]
        assert "y" in params["properties"]

    def test_infer_tool_parameters_with_defaults(self):
        """Test inferring tool parameters with default values."""
        def function_with_defaults(x: int, y: str = "default", z: bool = True) -> str:
            return f"{y}_{x}_{z}"
        
        params = _infer_tool_parameters(
            callable=function_with_defaults,
            tool_name="function_with_defaults",
            tool_description=None
        )
        
        assert "required" in params
        assert "x" in params["required"]  # No default value
        assert "y" not in params["required"]  # Has default value
        assert "z" not in params["required"]  # Has default value

    def test_get_jsonschema_type_basic_types(self):
        """Test JSON schema type inference for basic types."""
        assert _get_jsonschema_type(int) == {"type": "integer"}
        assert _get_jsonschema_type(float) == {"type": "number"}
        assert _get_jsonschema_type(str) == {"type": "string"}
        assert _get_jsonschema_type(bool) == {"type": "boolean"}

    def test_get_jsonschema_type_complex_types(self):
        """Test JSON schema type inference for complex types."""
        from typing import List, Dict, Optional
        
        # List type
        list_schema = _get_jsonschema_type(List[str])
        assert list_schema["type"] == "array"
        
        # Dict type
        dict_schema = _get_jsonschema_type(Dict[str, int])
        assert dict_schema["type"] == "object"
        
        # Optional type should handle None
        optional_schema = _get_jsonschema_type(Optional[str])
        # Should handle optional types appropriately

    def test_get_jsonschema_type_unknown_type(self):
        """Test JSON schema type inference for unknown types."""
        class CustomClass:
            pass
        
        # Should handle unknown types gracefully
        schema = _get_jsonschema_type(CustomClass)
        assert isinstance(schema, dict)  # Should return some schema structure


class TestDecoratorIntegration:
    """Test decorator integration and edge cases."""

    def test_decorator_preserves_function_metadata(self):
        """Test that decorators preserve function metadata."""
        def original_function(x: int, y: str) -> str:
            """Original function docstring."""
            return f"{y}_{x}"
        
        # Create a mock tracer for testing
        mock_tracer = MagicMock(spec=Tracer)
        fi_tracer = FITracer(mock_tracer, TraceConfig())
        
        decorated = fi_tracer.agent(original_function)
        
        # Should preserve function name and docstring
        assert decorated.__name__ == "original_function"
        assert "Original function docstring" in str(decorated.__doc__ or "")

    def test_decorator_chaining(self):
        """Test chaining multiple decorators."""
        mock_tracer = MagicMock(spec=Tracer)
        fi_tracer = FITracer(mock_tracer, TraceConfig())
        
        @fi_tracer.tool(name="chained_tool")
        @fi_tracer.chain(name="chained_chain") 
        def chained_function(x: int) -> int:
            return x * 2
        
        # Should still work correctly
        result = chained_function(5)
        assert result == 10

    def test_decorator_with_class_methods(self):
        """Test decorators work with class methods."""
        mock_tracer = MagicMock(spec=Tracer)
        fi_tracer = FITracer(mock_tracer, TraceConfig())
        
        class TestClass:
            @fi_tracer.agent
            def instance_method(self, x: int) -> int:
                return x * 3
            
            @fi_tracer.chain
            @classmethod
            def class_method(cls, x: int) -> int:
                return x + 10
            
            @fi_tracer.tool
            @staticmethod
            def static_method(x: int) -> int:
                return x - 5
        
        instance = TestClass()
        
        # All methods should work correctly
        assert instance.instance_method(4) == 12
        assert TestClass.class_method(5) == 15
        assert TestClass.static_method(10) == 5 