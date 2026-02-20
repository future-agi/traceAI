import asyncio
import json
from typing import Any, Dict, List
from unittest.mock import MagicMock, Mock, patch

import pytest
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, ConsoleSpanExporter
from pydantic import BaseModel

from fi_instrumentation import TraceConfig, FITracer
from traceai_instructor import InstructorInstrumentor
from traceai_instructor._wrappers import (
    _PatchWrapper,
    _HandleResponseWrapper,
    SafeJSONEncoder,
    _flatten,
    _get_input_value,
    _to_dict,
    _raw_input,
    _raw_output,
)


class PersonModel(BaseModel):
    """Sample Pydantic model for testing."""
    name: str
    age: int
    email: str


@pytest.fixture
def tracer_provider():
    """Create a test TracerProvider with console export for testing."""
    provider = TracerProvider()
    exporter = ConsoleSpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider


@pytest.fixture
def config():
    """Create a test TraceConfig."""
    return TraceConfig()


@pytest.fixture
def instrumentation_scope():
    """Set up clean instrumentation environment."""
    # Clear any existing instrumentation suppression
    context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, False)
    yield
    # Reset to clean state
    context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, False)


@pytest.fixture
def mock_instructor():
    """Mock the instructor module."""
    with patch("traceai_instructor.import_module") as mock_import:
        mock_instructor_module = MagicMock()
        mock_patch_module = MagicMock()

        def import_side_effect(module_name, package=None):
            if module_name == "instructor":
                return mock_instructor_module
            elif module_name == "instructor.patch":
                return mock_patch_module
            return MagicMock()

        mock_import.side_effect = import_side_effect
        yield mock_instructor_module, mock_patch_module


@pytest.fixture
def mock_protect():
    """Mock the fi.evals.Protect functionality."""
    with patch("traceai_instructor.Protect") as mock_protect:
        mock_protect.protect = MagicMock()
        yield mock_protect


class TestInstructorInstrumentor:
    """🏫 Test InstructorInstrumentor lifecycle and basic functionality."""

    def test_instrumentation_dependencies(self):
        """Test instrumentor dependencies are properly declared."""
        instrumentor = InstructorInstrumentor()
        dependencies = instrumentor.instrumentation_dependencies()

        assert isinstance(dependencies, tuple)
        assert "instructor >= 0.0.1" in dependencies
        assert len(dependencies) == 1

    def test_instrument_basic(self, tracer_provider, config, mock_protect):
        """Test basic instrumentation setup."""
        instrumentor = InstructorInstrumentor()

        mock_instructor_module = MagicMock()
        mock_patch_module = MagicMock()

        def import_side_effect(module_name):
            if module_name == "instructor":
                return mock_instructor_module
            elif module_name == "instructor.patch":
                return mock_patch_module
            return MagicMock()

        with patch("traceai_instructor.wrap_function_wrapper") as mock_wrap, \
             patch("traceai_instructor.import_module", side_effect=import_side_effect):
            instrumentor._instrument(tracer_provider=tracer_provider, config=config)

            # Verify the tracer was created
            assert hasattr(instrumentor, '_tracer')
            assert isinstance(instrumentor._tracer, FITracer)

            # Verify original methods were stored
            assert hasattr(instrumentor, '_original_patch')
            assert hasattr(instrumentor, '_original_handle_response_model')

    def test_instrument_without_tracer_provider(self, config, mock_protect):
        """Test instrumentation without explicit tracer provider."""
        instrumentor = InstructorInstrumentor()
        mock_instructor_module = MagicMock()
        mock_patch_module = MagicMock()

        def import_side_effect(module_name):
            if module_name == "instructor":
                return mock_instructor_module
            elif module_name == "instructor.patch":
                return mock_patch_module
            return MagicMock()

        with patch("traceai_instructor.wrap_function_wrapper"), \
             patch("traceai_instructor.import_module", side_effect=import_side_effect), \
             patch("opentelemetry.trace.get_tracer_provider") as mock_get_provider:
            mock_get_provider.return_value = MagicMock()
            instrumentor._instrument(config=config)
            mock_get_provider.assert_called_once()

    def test_instrument_without_config(self, tracer_provider, mock_protect):
        """Test instrumentation without explicit config."""
        instrumentor = InstructorInstrumentor()
        mock_instructor_module = MagicMock()
        mock_patch_module = MagicMock()

        def import_side_effect(module_name):
            if module_name == "instructor":
                return mock_instructor_module
            elif module_name == "instructor.patch":
                return mock_patch_module
            return MagicMock()

        with patch("traceai_instructor.wrap_function_wrapper"), \
             patch("traceai_instructor.import_module", side_effect=import_side_effect):
            instrumentor._instrument(tracer_provider=tracer_provider)
            # Should create default TraceConfig
            assert isinstance(instrumentor._tracer, FITracer)

    def test_uninstrument(self, tracer_provider, config, mock_protect):
        """Test proper uninstrumentation."""
        instrumentor = InstructorInstrumentor()
        mock_instructor_module = MagicMock()
        mock_patch_module = MagicMock()

        def import_side_effect(module_name):
            if module_name == "instructor":
                return mock_instructor_module
            elif module_name == "instructor.patch":
                return mock_patch_module
            return MagicMock()

        # Set up original methods
        original_patch = MagicMock()
        original_handle = MagicMock()
        mock_instructor_module.patch = MagicMock()
        mock_patch_module.handle_response_model = MagicMock()

        with patch("traceai_instructor.wrap_function_wrapper"), \
             patch("traceai_instructor.import_module", side_effect=import_side_effect):
            instrumentor._instrument(tracer_provider=tracer_provider, config=config)

            # Store originals manually for testing
            instrumentor._original_patch = original_patch
            instrumentor._original_handle_response_model = original_handle

            # Test uninstrumentation
            instrumentor._uninstrument()

            # Verify restoration
            assert mock_instructor_module.patch == original_patch
            assert mock_patch_module.handle_response_model == original_handle
            assert instrumentor._original_patch is None
            assert instrumentor._original_handle_response_model is None


class TestPatchWrapper:
    """📦 Test _PatchWrapper functionality."""

    @pytest.fixture
    def patch_wrapper(self, tracer_provider):
        """Create a _PatchWrapper instance for testing."""
        tracer = trace_api.get_tracer(__name__, tracer_provider=tracer_provider)
        return _PatchWrapper(tracer)

    def test_patch_wrapper_init(self, tracer_provider):
        """Test _PatchWrapper initialization."""
        tracer = trace_api.get_tracer(__name__, tracer_provider=tracer_provider)
        wrapper = _PatchWrapper(tracer)
        assert wrapper._tracer == tracer

    def test_patch_wrapper_call_sync(self, patch_wrapper, instrumentation_scope):
        """Test _PatchWrapper with synchronous function."""
        mock_wrapped = MagicMock()
        mock_wrapped.__module__ = "instructor"
        mock_wrapped.__name__ = "patch"
        mock_wrapped.return_value = {"patched": True}
        
        args = ("client",)
        kwargs = {"response_model": PersonModel, "mode": "json"}
        
        result = patch_wrapper(mock_wrapped, None, args, kwargs)
        
        mock_wrapped.assert_called_once_with(*args, **kwargs)
        assert result == {"patched": True}

    @pytest.mark.asyncio
    async def test_patch_wrapper_call_async(self, patch_wrapper, instrumentation_scope):
        """Test _PatchWrapper with asynchronous function."""
        async def mock_async_patch(*args, **kwargs):
            return {"async_patched": True}
        
        mock_wrapped = MagicMock()
        mock_wrapped.__module__ = "instructor"
        mock_wrapped.__name__ = "patch"
        mock_wrapped.side_effect = mock_async_patch
        
        # Mock iscoroutinefunction to return True
        with patch("inspect.iscoroutinefunction", return_value=True):
            args = ("async_client",)
            kwargs = {"response_model": PersonModel}
            
            result = await patch_wrapper(mock_wrapped, None, args, kwargs)
            assert result == {"async_patched": True}

    def test_patch_wrapper_suppressed(self, patch_wrapper):
        """Test _PatchWrapper respects instrumentation suppression."""
        mock_wrapped = MagicMock()
        mock_wrapped.__module__ = "instructor"
        mock_wrapped.__name__ = "patch"
        mock_wrapped.return_value = "suppressed_result"
        
        # Suppress instrumentation
        token = context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True)
        try:
            result = patch_wrapper(mock_wrapped, None, (), {})
            assert result == "suppressed_result"
        finally:
            # Don't use detach - just reset the context value
            context_api.set_value(_SUPPRESS_INSTRUMENTATION_KEY, False)

    def test_patch_wrapper_exception_handling(self, patch_wrapper, instrumentation_scope):
        """Test _PatchWrapper exception handling."""
        mock_wrapped = MagicMock()
        mock_wrapped.__module__ = "instructor"
        mock_wrapped.__name__ = "patch"
        mock_wrapped.side_effect = ValueError("Patch failed")
        
        with pytest.raises(ValueError, match="Patch failed"):
            patch_wrapper(mock_wrapped, None, (), {})


class TestHandleResponseWrapper:
    """🎯 Test _HandleResponseWrapper functionality."""

    @pytest.fixture
    def response_wrapper(self, tracer_provider):
        """Create a _HandleResponseWrapper instance for testing."""
        tracer = trace_api.get_tracer(__name__, tracer_provider=tracer_provider)
        return _HandleResponseWrapper(tracer)

    def test_response_wrapper_init(self, tracer_provider):
        """Test _HandleResponseWrapper initialization."""
        tracer = trace_api.get_tracer(__name__, tracer_provider=tracer_provider)
        wrapper = _HandleResponseWrapper(tracer)
        assert wrapper._tracer == tracer

    def test_response_wrapper_call_sync(self, response_wrapper, instrumentation_scope):
        """Test _HandleResponseWrapper with synchronous function."""
        mock_wrapped = MagicMock()
        mock_wrapped.__module__ = "instructor.patch"
        mock_wrapped.__name__ = "handle_response_model"
        
        # Mock response
        mock_response = [PersonModel(name="John", age=30, email="john@example.com")]
        mock_wrapped.return_value = mock_response
        
        args = (mock_response, PersonModel)
        kwargs = {"mode": "json"}
        
        result = response_wrapper(mock_wrapped, None, args, kwargs)
        
        mock_wrapped.assert_called_once_with(*args, **kwargs)
        assert result == mock_response

    @pytest.mark.asyncio
    async def test_response_wrapper_call_async(self, response_wrapper, instrumentation_scope):
        """Test _HandleResponseWrapper with asynchronous function."""
        async def mock_async_handle(*args, **kwargs):
            return {"model": "PersonModel", "data": {"name": "Alice", "age": 25}}
        
        mock_wrapped = MagicMock()
        mock_wrapped.__module__ = "instructor.patch"
        mock_wrapped.__name__ = "handle_response_model"
        mock_wrapped.side_effect = mock_async_handle
        
        # Mock iscoroutinefunction to return True
        with patch("inspect.iscoroutinefunction", return_value=True):
            args = ("response_data", PersonModel)
            kwargs = {"validation": True}
            
            result = await response_wrapper(mock_wrapped, None, args, kwargs)
            assert result["model"] == "PersonModel"
            assert result["data"]["name"] == "Alice"

    def test_response_wrapper_suppressed(self, response_wrapper):
        """Test _HandleResponseWrapper respects instrumentation suppression."""
        mock_wrapped = MagicMock()
        mock_wrapped.__module__ = "instructor.patch"
        mock_wrapped.__name__ = "handle_response_model"
        mock_wrapped.return_value = "suppressed_result"
        
        # Mock context.get_value to return True for suppression
        with patch("opentelemetry.context.get_value", return_value=True):
            result = response_wrapper(mock_wrapped, None, (), {})
            assert result == "suppressed_result"

    def test_response_wrapper_exception_handling(self, response_wrapper, instrumentation_scope):
        """Test _HandleResponseWrapper exception handling."""
        mock_wrapped = MagicMock()
        mock_wrapped.__module__ = "instructor.patch"
        mock_wrapped.__name__ = "handle_response_model"
        mock_wrapped.side_effect = Exception("Response handling failed")
        
        with pytest.raises(Exception, match="Response handling failed"):
            response_wrapper(mock_wrapped, None, (), {})

    def test_response_wrapper_empty_response(self, response_wrapper, instrumentation_scope):
        """Test _HandleResponseWrapper with empty response."""
        mock_wrapped = MagicMock()
        mock_wrapped.__module__ = "instructor.patch"
        mock_wrapped.__name__ = "handle_response_model"
        mock_wrapped.return_value = None
        
        result = response_wrapper(mock_wrapped, None, (), {})
        assert result is None


class TestUtilityFunctions:
    """🛠️ Test utility functions."""

    def test_safe_json_encoder_basic_types(self):
        """Test SafeJSONEncoder with basic types."""
        encoder = SafeJSONEncoder()
        
        # Test normal JSON serializable objects
        assert encoder.encode({"key": "value"}) == '{"key": "value"}'
        assert encoder.encode([1, 2, 3]) == "[1, 2, 3]"
        assert encoder.encode("string") == '"string"'

    def test_safe_json_encoder_pydantic_model(self):
        """Test SafeJSONEncoder with Pydantic models."""
        encoder = SafeJSONEncoder()
        person = PersonModel(name="John", age=30, email="john@example.com")
        
        # Should use model.dict() method
        result = json.loads(encoder.encode(person))
        assert result["name"] == "John"
        assert result["age"] == 30
        assert result["email"] == "john@example.com"

    def test_safe_json_encoder_non_serializable(self):
        """Test SafeJSONEncoder with non-serializable objects."""
        encoder = SafeJSONEncoder()
        
        class CustomObject:
            def __repr__(self):
                return "CustomObject()"
        
        obj = CustomObject()
        result = encoder.encode(obj)
        assert "CustomObject()" in result

    def test_flatten_basic_mapping(self):
        """Test _flatten with basic mapping."""
        mapping = {"key1": "value1", "key2": "value2"}
        result = dict(_flatten(mapping))
        
        assert result["key1"] == "value1"
        assert result["key2"] == "value2"

    def test_flatten_nested_mapping(self):
        """Test _flatten with nested mappings."""
        mapping = {
            "outer": {
                "inner1": "value1",
                "inner2": "value2"
            },
            "simple": "value3"
        }
        result = dict(_flatten(mapping))
        
        assert result["outer.inner1"] == "value1"
        assert result["outer.inner2"] == "value2"
        assert result["simple"] == "value3"

    def test_flatten_list_with_mappings(self):
        """Test _flatten with lists containing mappings."""
        mapping = {
            "items": [
                {"name": "item1", "value": 1},
                {"name": "item2", "value": 2}
            ]
        }
        result = dict(_flatten(mapping))
        
        assert result["items.0.name"] == "item1"
        assert result["items.0.value"] == 1
        assert result["items.1.name"] == "item2"
        assert result["items.1.value"] == 2

    def test_flatten_with_none_values(self):
        """Test _flatten filters out None values."""
        mapping = {"key1": "value1", "key2": None, "key3": "value3"}
        result = dict(_flatten(mapping))
        
        assert "key1" in result
        assert "key2" not in result
        assert "key3" in result

    def test_flatten_with_enum(self):
        """Test _flatten handles enum values."""
        from enum import Enum
        
        class Color(Enum):
            RED = "red"
            BLUE = "blue"
        
        mapping = {"color": Color.RED}
        result = dict(_flatten(mapping))
        
        assert result["color"] == "red"

    def test_get_input_value_basic(self):
        """Test _get_input_value with basic function."""
        def test_func(arg1, arg2, kwarg1=None):
            pass
        
        result = _get_input_value(test_func, "value1", "value2", kwarg1="kwarg_value")
        parsed = json.loads(result)
        
        assert parsed["arg1"] == "value1"
        assert parsed["arg2"] == "value2"
        assert parsed["kwarg1"] == "kwarg_value"

    def test_get_input_value_with_self(self):
        """Test _get_input_value filters out 'self' parameter."""
        def test_method(self, arg1, arg2):
            pass
        
        result = _get_input_value(test_method, "self_object", "value1", "value2")
        parsed = json.loads(result)
        
        # Should not include 'self'
        assert "self" not in parsed
        assert parsed["arg1"] == "value1"
        assert parsed["arg2"] == "value2"

    def test_get_input_value_with_kwargs(self):
        """Test _get_input_value with **kwargs."""
        def test_func(arg1, **kwargs):
            pass
        
        extra_kwargs = {"extra1": "value1", "extra2": "value2"}
        result = _get_input_value(test_func, "arg_value", **extra_kwargs)
        parsed = json.loads(result)
        
        assert parsed["arg1"] == "arg_value"
        assert parsed["extra1"] == "value1"
        assert parsed["extra2"] == "value2"

    def test_get_input_value_type_error(self):
        """Test _get_input_value handles TypeError gracefully."""
        def problematic_func():
            pass
        
        # Create a scenario that might cause TypeError in signature binding
        result = _get_input_value(problematic_func, "unexpected_arg", unexpected_kwarg="value")
        parsed = json.loads(result)
        
        # Should fallback to kwargs only
        assert parsed["unexpected_kwarg"] == "value"

    def test_to_dict_basic_types(self):
        """Test _to_dict with basic types."""
        assert _to_dict(None) is None
        assert _to_dict("string") == "string"
        assert _to_dict(42) == 42
        assert _to_dict(True) is True

    def test_to_dict_list(self):
        """Test _to_dict with lists."""
        input_list = [1, "string", {"key": "value"}]
        result = _to_dict(input_list)
        
        assert result == [1, "string", {"key": "value"}]

    def test_to_dict_dict(self):
        """Test _to_dict with dictionaries."""
        input_dict = {"key1": "value1", "nested": {"key2": "value2"}}
        result = _to_dict(input_dict)
        
        assert result == {"key1": "value1", "nested": {"key2": "value2"}}

    def test_to_dict_pydantic_model(self):
        """Test _to_dict with Pydantic models."""
        person = PersonModel(name="John", age=30, email="john@example.com")
        result = _to_dict(person)
        
        # Should return JSON string from model_dump_json()
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert parsed["name"] == "John"
        assert parsed["age"] == 30

    def test_to_dict_object_with_dict(self):
        """Test _to_dict with objects having __dict__."""
        class CustomObject:
            def __init__(self):
                self.attr1 = "value1"
                self.attr2 = "value2"
        
        obj = CustomObject()
        result = _to_dict(obj)
        
        assert result == {"attr1": "value1", "attr2": "value2"}

    def test_raw_input_basic(self):
        """Test _raw_input with basic arguments."""
        result = _raw_input("arg1", "arg2", kwarg1="value1", kwarg2="value2")
        
        assert result["arg_0"] == "arg1"
        assert result["arg_1"] == "arg2"
        assert result["kwarg1"] == "value1"
        assert result["kwarg2"] == "value2"

    def test_raw_input_with_pydantic(self):
        """Test _raw_input with Pydantic models."""
        person = PersonModel(name="John", age=30, email="john@example.com")
        result = _raw_input(person, model_type="PersonModel")
        
        # Should convert model to JSON string
        assert isinstance(result["arg_0"], str)
        assert result["model_type"] == "PersonModel"

    def test_raw_output_basic(self):
        """Test _raw_output with basic response."""
        response = {"result": "success", "data": [1, 2, 3]}
        result = _raw_output(response)
        
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert parsed["result"] == "success"
        assert parsed["data"] == [1, 2, 3]

    def test_raw_output_empty(self):
        """Test _raw_output with empty response."""
        assert _raw_output(None) == ""
        assert _raw_output("") == ""
        assert _raw_output([]) == ""

    def test_raw_output_pydantic_model(self):
        """Test _raw_output with Pydantic model."""
        person = PersonModel(name="Alice", age=25, email="alice@example.com")
        result = _raw_output(person)
        
        assert isinstance(result, str)
        # Should contain the JSON representation
        assert "Alice" in result
        assert "25" in result


class TestIntegrationScenarios:
    """🎭 Test integration scenarios and real-world usage patterns."""

    def test_instructor_patch_scenario(self, tracer_provider, config):
        """Test complete instructor.patch instrumentation scenario."""
        instrumentor = InstructorInstrumentor()
        mock_instructor = MagicMock()
        mock_patch_module = MagicMock()

        def import_side_effect(module_name):
            if module_name == "instructor":
                return mock_instructor
            elif module_name == "instructor.patch":
                return mock_patch_module
            return MagicMock()

        with patch("traceai_instructor.wrap_function_wrapper") as mock_wrap, \
             patch("traceai_instructor.import_module", side_effect=import_side_effect):
            # Instrument
            instrumentor._instrument(tracer_provider=tracer_provider, config=config)

            # Verify the instrumentor was set up correctly
            assert hasattr(instrumentor, '_tracer')
            assert isinstance(instrumentor._tracer, FITracer)

            # Test uninstrumentation
            instrumentor._uninstrument()

    def test_response_model_handling_scenario(self, tracer_provider):
        """Test response model handling instrumentation scenario."""
        tracer = trace_api.get_tracer(__name__, tracer_provider=tracer_provider)
        wrapper = _HandleResponseWrapper(tracer)
        
        # Mock a complex response handling scenario
        mock_wrapped = MagicMock()
        mock_wrapped.__module__ = "instructor.patch"
        mock_wrapped.__name__ = "handle_response_model"
        
        # Simulate response with multiple models
        mock_response = [
            PersonModel(name="John", age=30, email="john@example.com"),
            PersonModel(name="Jane", age=28, email="jane@example.com")
        ]
        mock_wrapped.return_value = mock_response
        
        result = wrapper(mock_wrapped, None, (mock_response, PersonModel), {"strict": True})
        
        assert result == mock_response
        assert len(result) == 2
        assert result[0].name == "John"
        assert result[1].name == "Jane"

    def test_error_scenarios(self, tracer_provider):
        """Test various error scenarios."""
        tracer = trace_api.get_tracer(__name__, tracer_provider=tracer_provider)
        
        # Test patch wrapper with error
        patch_wrapper = _PatchWrapper(tracer)
        mock_wrapped = MagicMock()
        mock_wrapped.__module__ = "instructor"
        mock_wrapped.__name__ = "patch"
        mock_wrapped.side_effect = RuntimeError("Patching failed")
        
        with pytest.raises(RuntimeError, match="Patching failed"):
            patch_wrapper(mock_wrapped, None, (), {})
        
        # Test response wrapper with error
        response_wrapper = _HandleResponseWrapper(tracer)
        mock_wrapped = MagicMock()
        mock_wrapped.__module__ = "instructor.patch"
        mock_wrapped.__name__ = "handle_response_model"
        mock_wrapped.side_effect = ValueError("Invalid response")
        
        with pytest.raises(ValueError, match="Invalid response"):
            response_wrapper(mock_wrapped, None, (), {})

    def test_complex_data_structures(self):
        """Test handling of complex nested data structures."""
        # Test complex flattening scenario
        complex_mapping = {
            "models": [
                {
                    "type": "person",
                    "data": {"name": "John", "details": {"age": 30, "city": "NYC"}}
                },
                {
                    "type": "company", 
                    "data": {"name": "TechCorp", "employees": [{"name": "Alice"}, {"name": "Bob"}]}
                }
            ],
            "metadata": {
                "version": "1.0",
                "created": "2024-01-01",
                "tags": ["test", "demo"]
            }
        }
        
        result = dict(_flatten(complex_mapping))
        
        # Verify complex nested access
        assert result["models.0.type"] == "person"
        assert result["models.0.data.name"] == "John"
        assert result["models.0.data.details.age"] == 30
        assert result["models.1.type"] == "company"
        assert result["metadata.version"] == "1.0"

    def test_performance_with_large_data(self):
        """Test performance with large data structures."""
        # Create large data structure
        large_data = {
            "items": [{"id": i, "value": f"item_{i}"} for i in range(100)],
            "metadata": {"count": 100, "processed": True}
        }
        
        # Test that flattening works efficiently
        result = dict(_flatten(large_data))
        
        # Verify structure is preserved
        assert len([k for k in result.keys() if k.startswith("items.")]) == 200  # 100 * 2 fields
        assert result["metadata.count"] == 100
        assert result["metadata.processed"] is True
        
        # Test JSON encoding works
        json_result = _raw_output(large_data)
        assert isinstance(json_result, str)
        assert len(json_result) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"]) 