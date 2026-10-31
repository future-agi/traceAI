"""
Test suite for Guardrails framework instrumentation.

Tests the instrumentation of Guardrails components:
- Runner.step (GUARDRAIL spans)
- PromptCallableBase.__call__ (LLM spans)
- ValidatorServiceBase.after_run_validator (validation spans)
- Context variables management
- JSON encoding and utility functions
"""

import pytest
from unittest.mock import Mock, MagicMock, patch, call
import json
from enum import Enum

from opentelemetry import trace as trace_api
from opentelemetry.trace.status import StatusCode
from opentelemetry import context as context_api
from packaging.version import Version

from traceai_guardrails import GuardrailsInstrumentor, _Contextvars
from traceai_guardrails._wrap_guard_call import (
    _ParseCallableWrapper,
    _PromptCallableWrapper,
    _PostValidationWrapper,
    _WithTracer,
    SafeJSONEncoder,
    _flatten,
    _get_input_value,
    _get_raw_input,
    _get_raw_output,
    _to_dict,
    _get_llm_input_messages,
    _get_llm_output_messages,
)


class TestGuardrailsInstrumentor:
    """Exercise the real installed Guardrails hook targets without a provider call."""

    def setup_method(self):
        self.instrumentor = GuardrailsInstrumentor()

    def test_instrumentation_dependencies(self):
        assert self.instrumentor.instrumentation_dependencies() == ("guardrails-ai>=0.5.0",)

    @pytest.mark.parametrize("version,supported", [
        ("0.4.5", False), ("0.5.0", True), ("0.5.1", True), ("0.6.0", True),
    ])
    def test_instrument_version_support(self, version, supported):
        # The obsolete pre-0.5.1 guard was removed; retain the declared >=0.5.0
        # support contract rather than reintroducing a silent instrumentation skip.
        from packaging.requirements import Requirement

        requirement, = self.instrumentor.instrumentation_dependencies()
        assert (Version(version) in Requirement(requirement).specifier) is supported

    def test_instrument_basic(self):
        import guardrails as gd
        from guardrails.run import Runner
        from guardrails.llm_providers import PromptCallableBase
        from guardrails.validator_service import ValidatorServiceBase
        from opentelemetry.sdk.trace import TracerProvider

        originals = (Runner.step, PromptCallableBase.__call__,
                     ValidatorServiceBase.after_run_validator,
                     gd.guard.contextvars, gd.async_guard.contextvars)
        provider = TracerProvider()
        try:
            self.instrumentor._instrument(tracer_provider=provider)
            assert Runner.step.__wrapped__ is originals[0]
            assert PromptCallableBase.__call__.__wrapped__ is originals[1]
            assert ValidatorServiceBase.after_run_validator.__wrapped__ is originals[2]
            assert isinstance(gd.guard.contextvars, _Contextvars)
            assert isinstance(gd.async_guard.contextvars, _Contextvars)
        finally:
            self.instrumentor._uninstrument()
            provider.shutdown()
        assert Runner.step is originals[0]
        assert PromptCallableBase.__call__ is originals[1]
        assert ValidatorServiceBase.after_run_validator is originals[2]
        assert gd.guard.contextvars is originals[3]
        assert gd.async_guard.contextvars is originals[4]

    def test_uninstrument(self):
        # Calling cleanup before any hooks are installed must be harmless.
        import guardrails as gd
        from guardrails.run import Runner
        from guardrails.llm_providers import PromptCallableBase
        from guardrails.validator_service import ValidatorServiceBase

        originals = (Runner.step, PromptCallableBase.__call__,
                     ValidatorServiceBase.after_run_validator,
                     gd.guard.contextvars, gd.async_guard.contextvars)
        self.instrumentor._uninstrument()
        assert (Runner.step, PromptCallableBase.__call__,
                ValidatorServiceBase.after_run_validator,
                gd.guard.contextvars, gd.async_guard.contextvars) == originals

    def test_instrument_exception_handling(self):
        with patch("traceai_guardrails.import_module", side_effect=ImportError("Test error")):
            with patch.object(self.instrumentor, '_uninstrument', wraps=self.instrumentor._uninstrument) as cleanup:
                with pytest.raises(ImportError, match="Test error"):
                    self.instrumentor._instrument()
                cleanup.assert_called_once()


class TestContextvars:
    """Test the _Contextvars wrapper."""

    def test_contextvars_creation(self):
        """Test _Contextvars wrapper creation."""
        mock_cv = Mock()
        wrapped = _Contextvars(mock_cv)
        
        assert wrapped.__wrapped__ == mock_cv

    def test_contextvars_context_method(self):
        """Test _Contextvars.Context() method."""
        with patch("traceai_guardrails.contextvars.copy_context") as mock_copy:
            mock_context = Mock()
            mock_copy.return_value = mock_context
            
            result = _Contextvars.Context()
            
            assert result == mock_context
            mock_copy.assert_called_once()


class TestParseCallableWrapper:
    """Test the parse callable wrapper."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock(spec=trace_api.Tracer)
        self.wrapper = _ParseCallableWrapper(tracer=self.mock_tracer)

    def test_parse_callable_wrapper_basic(self):
        """Test basic parse callable wrapper operation."""
        mock_span = MagicMock()
        self.mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span
        
        def wrapped_func(*args, **kwargs):
            return {"result": "success"}
        
        result = self.wrapper(wrapped_func, None, (), {"param": "value"})
        
        assert result == {"result": "success"}
        self.mock_tracer.start_as_current_span.assert_called_once()
        mock_span.set_status.assert_called_with(StatusCode.OK)

    def test_parse_callable_wrapper_with_instance(self):
        """Test parse callable wrapper with instance."""
        mock_span = MagicMock()
        self.mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span
        
        mock_instance = Mock()
        mock_instance.__class__.__name__ = "TestRunner"
        
        def wrapped_func(*args, **kwargs):
            return {"result": "success"}
        wrapped_func.__name__ = "step"
        
        result = self.wrapper(wrapped_func, mock_instance, (), {})
        
        assert result == {"result": "success"}
        # Should use class name and method name for span
        call_args = self.mock_tracer.start_as_current_span.call_args[0]
        assert "TestRunner.step" == call_args[0]

    def test_parse_callable_wrapper_exception(self):
        """Test parse callable wrapper exception handling."""
        mock_span = MagicMock()
        self.mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span
        
        def wrapped_func(*args, **kwargs):
            raise ValueError("Test error")
        
        with pytest.raises(ValueError, match="Test error"):
            self.wrapper(wrapped_func, None, (), {})
        
        mock_span.record_exception.assert_called_once()
        # Check that error status was set (don't compare Status objects directly)
        status_call = mock_span.set_status.call_args[0][0]
        assert status_call.status_code == trace_api.StatusCode.ERROR
        assert "Test error" in str(status_call.description)

    def test_parse_callable_wrapper_suppressed(self):
        """Test parse callable wrapper when instrumentation is suppressed."""
        with patch("traceai_guardrails._wrap_guard_call.context_api.get_value") as mock_get_value:
            mock_get_value.return_value = True
            
            def wrapped_func(*args, **kwargs):
                return {"result": "bypassed"}
            
            result = self.wrapper(wrapped_func, None, (), {})
            
            assert result == {"result": "bypassed"}
            # Should not start span when suppressed
            self.mock_tracer.start_as_current_span.assert_not_called()


class TestPromptCallableWrapper:
    """Test the prompt callable wrapper."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock(spec=trace_api.Tracer)
        self.wrapper = _PromptCallableWrapper(tracer=self.mock_tracer)

    def test_prompt_callable_wrapper_basic(self):
        """Test basic prompt callable wrapper operation."""
        mock_span = MagicMock()
        self.mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span
        
        # Mock response
        mock_response = Mock()
        mock_response.output = "Generated response"
        mock_response.prompt_token_count = 10
        mock_response.response_token_count = 20
        
        def wrapped_func(*args, **kwargs):
            return mock_response
        
        result = self.wrapper(wrapped_func, None, (), {})
        
        assert result == mock_response
        self.mock_tracer.start_as_current_span.assert_called_once()
        mock_span.set_status.assert_called_with(StatusCode.OK)

    def test_prompt_callable_wrapper_with_messages(self):
        """Test prompt callable wrapper with message history."""
        mock_span = MagicMock()
        self.mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span
        
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"}
        ]
        
        mock_response = Mock()
        mock_response.output = "Generated response"
        
        def wrapped_func(*args, **kwargs):
            return mock_response
        
        result = self.wrapper(wrapped_func, None, (), {"msg_history": messages})
        
        assert result == mock_response
        # Should set LLM input message attributes (includes .message. in path)
        mock_span.set_attribute.assert_any_call("gen_ai.input.messages.0.message.role", "user")
        mock_span.set_attribute.assert_any_call("gen_ai.input.messages.0.message.content", "Hello")

    def test_prompt_callable_wrapper_with_tokens(self):
        """Test prompt callable wrapper with token counts."""
        mock_span = MagicMock()
        self.mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span
        
        mock_response = Mock()
        mock_response.output = "Generated response"
        mock_response.prompt_token_count = 15
        mock_response.response_token_count = 25
        
        def wrapped_func(*args, **kwargs):
            return mock_response
        
        result = self.wrapper(wrapped_func, None, (), {})
        
        assert result == mock_response
        # Should set token count attributes
        mock_span.set_attribute.assert_any_call("gen_ai.usage.input_tokens", 15)
        mock_span.set_attribute.assert_any_call("gen_ai.usage.output_tokens", 25)

    def test_prompt_callable_wrapper_exception(self):
        """Test prompt callable wrapper exception handling."""
        mock_span = MagicMock()
        self.mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span
        
        def wrapped_func(*args, **kwargs):
            raise RuntimeError("LLM error")
        
        with pytest.raises(RuntimeError, match="LLM error"):
            self.wrapper(wrapped_func, None, (), {})
        
        mock_span.record_exception.assert_called_once()
        # Check that error status was set (don't compare Status objects directly)
        status_call = mock_span.set_status.call_args[0][0]
        assert status_call.status_code == trace_api.StatusCode.ERROR
        assert "LLM error" in str(status_call.description)


class TestPostValidationWrapper:
    """Test the post validation wrapper."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock(spec=trace_api.Tracer)
        self.wrapper = _PostValidationWrapper(tracer=self.mock_tracer)

    def test_post_validation_wrapper_basic(self):
        """Test basic post validation wrapper operation."""
        mock_span = MagicMock()
        self.mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span
        
        # Mock validator and validation result
        mock_validator = Mock()
        mock_validator.rail_alias = "test_validator"
        mock_validator.on_fail_descriptor.name = "exception"
        
        mock_validation_result = Mock()
        mock_validation_result.outcome = "pass"
        mock_validation_result.metadata = {"score": 0.95}
        
        def wrapped_func(*args, **kwargs):
            return {"validation": "completed"}
        
        result = self.wrapper(wrapped_func, None, (mock_validator, None, mock_validation_result), {})
        
        assert result == {"validation": "completed"}
        # Should set validator attributes
        mock_span.set_attribute.assert_any_call("validator_name", "test_validator")
        mock_span.set_attribute.assert_any_call("validator_on_fail", "exception")
        mock_span.set_attribute.assert_any_call("output.value", "pass")

    def test_post_validation_wrapper_dataset_embeddings(self):
        """Test post validation wrapper with dataset embeddings validator."""
        mock_span = MagicMock()
        self.mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span
        
        # Mock validator and validation result for dataset embeddings
        mock_validator = Mock()
        mock_validator.rail_alias = "fi/dataset_embeddings"
        mock_validator.on_fail_descriptor.name = "filter"
        
        mock_validation_result = Mock()
        mock_validation_result.outcome = "pass"
        mock_validation_result.metadata = {
            "user_message": "Test user query",
            "similarity_score": 0.85
        }
        
        def wrapped_func(*args, **kwargs):
            return {"validation": "completed"}
        
        result = self.wrapper(wrapped_func, None, (mock_validator, None, mock_validation_result), {})
        
        assert result == {"validation": "completed"}
        # Should set input value from user_message
        mock_span.set_attribute.assert_any_call("input.value", "Test user query")
        # Only governed input/output and safe validator identity are recorded
        assert mock_span.set_attribute.call_count > 3

    def test_post_validation_wrapper_no_metadata(self):
        """Test post validation wrapper with no metadata."""
        mock_span = MagicMock()
        self.mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span
        
        mock_validator = Mock()
        mock_validator.rail_alias = "test_validator"
        mock_validator.on_fail_descriptor.name = "noop"
        
        mock_validation_result = Mock()
        mock_validation_result.outcome = "fail"
        mock_validation_result.metadata = None
        
        def wrapped_func(*args, **kwargs):
            return {"validation": "completed"}
        
        result = self.wrapper(wrapped_func, None, (mock_validator, None, mock_validation_result), {})
        
        assert result == {"validation": "completed"}
        mock_span.set_attribute.assert_any_call("output.value", "fail")


class TestUtilityFunctions:
    """Test utility functions."""

    def test_safe_json_encoder_dict_method(self):
        """Test SafeJSONEncoder with object having dict method."""
        encoder = SafeJSONEncoder()
        
        class TestObj:
            def dict(self):
                return {"test": "value"}
        
        obj = TestObj()
        result = encoder.default(obj)
        
        assert result == {"test": "value"}

    def test_safe_json_encoder_fallback(self):
        """Test SafeJSONEncoder fallback to repr."""
        encoder = SafeJSONEncoder()
        
        class TestObj:
            def __repr__(self):
                return "TestObj()"
        
        obj = TestObj()
        result = encoder.default(obj)
        
        assert result == "TestObj()"

    def test_flatten_nested_dict(self):
        """Test _flatten function with nested dict."""
        test_dict = {
            "level1": {
                "level2": "value",
                "level2b": None  # Should be skipped
            },
            "simple": "simple_value"
        }
        
        flattened = list(_flatten(test_dict))
        
        attr_dict = dict(flattened)
        assert "level1.level2" in attr_dict
        assert attr_dict["level1.level2"] == "value"
        assert "simple" in attr_dict
        assert attr_dict["simple"] == "simple_value"
        assert "level1.level2b" not in attr_dict  # None values excluded

    def test_flatten_list_of_dicts(self):
        """Test _flatten function with list of dicts."""
        test_dict = {
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"}
            ]
        }
        
        flattened = list(_flatten(test_dict))
        
        attr_dict = dict(flattened)
        assert "messages.0.role" in attr_dict
        assert attr_dict["messages.0.role"] == "user"
        assert "messages.1.content" in attr_dict
        assert attr_dict["messages.1.content"] == "Hi there"

    def test_flatten_enum_values(self):
        """Test _flatten function with enum values."""
        class TestEnum(Enum):
            VALUE = "test_value"
        
        test_dict = {
            "enum_field": TestEnum.VALUE
        }
        
        flattened = list(_flatten(test_dict))
        
        attr_dict = dict(flattened)
        assert "enum_field" in attr_dict
        assert attr_dict["enum_field"] == "test_value"

    def test_flatten_none_mapping(self):
        """Test _flatten function with None mapping."""
        result = list(_flatten(None))
        assert result == []

    def test_get_input_value_basic(self):
        """Test _get_input_value function."""
        def test_func(arg1, arg2="default"):
            pass
        
        result = _get_input_value(test_func, "value1", arg2="value2")
        
        # Should return JSON string of arguments
        parsed = json.loads(result)
        assert parsed["arg1"] == "value1"
        assert parsed["arg2"] == "value2"

    def test_get_input_value_with_self(self):
        """Test _get_input_value function with self parameter."""
        def test_method(self, arg1, arg2="default", **kwargs):
            pass
        
        # Test with extra keyword arguments
        result = _get_input_value(test_method, "value1", arg3="value3")
        
        parsed = json.loads(result)
        # Should exclude self parameter
        assert "self" not in parsed
        assert parsed["arg1"] == "value1"
        assert parsed["arg3"] == "value3"

    def test_get_raw_input(self):
        """Test _get_raw_input function."""
        result = _get_raw_input(("arg1", "arg2"), param1="value1", param2="value2")
        
        parsed = json.loads(result)
        assert parsed["args"] == ["arg1", "arg2"]
        assert parsed["param1"] == "value1"
        assert parsed["param2"] == "value2"

    def test_get_raw_output(self):
        """Test _get_raw_output function."""
        test_response = {"result": "success", "data": [1, 2, 3]}
        
        result = _get_raw_output(test_response)
        
        parsed = json.loads(result)
        assert parsed["response"]["result"] == "success"
        assert parsed["response"]["data"] == [1, 2, 3]

    def test_to_dict_with_to_dict_method(self):
        """Test _to_dict with object having to_dict method."""
        class TestObj:
            def to_dict(self):
                return {"test": "value"}
        
        obj = TestObj()
        result = _to_dict(obj)
        
        assert result == {"test": "value"}

    def test_to_dict_with_dict_attr(self):
        """Test _to_dict with object having __dict__."""
        class TestObj:
            def __init__(self):
                self.test = "value"
                self.number = 42
        
        obj = TestObj()
        result = _to_dict(obj)
        
        assert result == {"test": "value", "number": 42}

    def test_to_dict_with_list(self):
        """Test _to_dict with list."""
        test_list = [{"a": 1}, {"b": 2}]
        result = _to_dict(test_list)
        
        assert result == [{"a": 1}, {"b": 2}]

    def test_to_dict_with_tuple(self):
        """Test _to_dict with tuple."""
        test_tuple = ({"a": 1}, {"b": 2})
        result = _to_dict(test_tuple)
        
        assert result == ({"a": 1}, {"b": 2})

    def test_to_dict_with_dict(self):
        """Test _to_dict with dict."""
        test_dict = {"nested": {"value": 123}}
        result = _to_dict(test_dict)
        
        assert result == {"nested": {"value": 123}}

    def test_to_dict_fallback(self):
        """Test _to_dict fallback for other types."""
        result = _to_dict("simple string")
        assert result == "simple string"
        
        result = _to_dict(42)
        assert result == 42
        
        result = _to_dict(None)
        assert result is None

    def test_get_llm_input_messages(self):
        """Test _get_llm_input_messages function."""
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"}
        ]
        
        attributes = list(_get_llm_input_messages(messages))
        
        # Should extract role and content for each message (with .message. in path)
        attr_dict = dict(attributes)
        assert "gen_ai.input.messages.0.message.role" in attr_dict
        assert attr_dict["gen_ai.input.messages.0.message.role"] == "system"
        assert "gen_ai.input.messages.0.message.content" in attr_dict
        assert attr_dict["gen_ai.input.messages.0.message.content"] == "You are helpful"
        assert "gen_ai.input.messages.2.message.content" in attr_dict
        assert attr_dict["gen_ai.input.messages.2.message.content"] == "Hi there!"

    def test_get_llm_input_messages_invalid(self):
        """Test _get_llm_input_messages with invalid messages."""
        messages = [
            {"role": "user"},  # No content
            "invalid message",  # Not a dict
            {"content": "orphaned content", "role": "user"},  # Has role and content - valid!  
            {"role": "assistant", "content": "Valid message"}  # This one should work too
        ]
        
        attributes = list(_get_llm_input_messages(messages))
        
        # Should extract 2 valid message pairs (indices 2 and 3)
        assert len(attributes) == 4  # 2 messages × 2 attributes each (role + content)

    def test_get_llm_output_messages_string(self):
        """Test _get_llm_output_messages with string output."""
        output = "This is the assistant response"
        
        attributes = list(_get_llm_output_messages(output))
        
        attr_dict = dict(attributes)
        assert "gen_ai.output.messages.0.message.role" in attr_dict
        assert attr_dict["gen_ai.output.messages.0.message.role"] == "assistant"
        assert "gen_ai.output.messages.0.message.content" in attr_dict
        assert attr_dict["gen_ai.output.messages.0.message.content"] == "This is the assistant response"

    def test_get_llm_output_messages_empty(self):
        """Test _get_llm_output_messages with empty output."""
        attributes = list(_get_llm_output_messages(None))
        assert len(attributes) == 0
        
        attributes = list(_get_llm_output_messages(""))
        assert len(attributes) == 0


class TestWithTracer:
    """Test _WithTracer base class."""

    def test_with_tracer_creation(self):
        """Test _WithTracer creation and tracer assignment."""
        mock_tracer = Mock()
        
        class TestWrapper(_WithTracer):
            pass
        
        wrapper = TestWrapper(tracer=mock_tracer)
        
        assert wrapper._tracer == mock_tracer


@pytest.mark.parametrize("hide_content", [False, True])
def test_prompt_wrapper_exported_attributes_and_privacy(hide_content):
    """Canonical attributes survive export, while hidden payloads never do."""
    from fi_instrumentation import FITracer, TraceConfig
    from guardrails.classes.llm.llm_response import LLMResponse
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer(__name__), config=TraceConfig(
        hide_inputs=hide_content, hide_outputs=hide_content,
    ))
    response = LLMResponse(output="private-answer", prompt_token_count=3, response_token_count=5)

    def respond(msg_history):
        assert msg_history == [{"role": "user", "content": "private-question"}]
        return response

    try:
        result = _PromptCallableWrapper(tracer=tracer)(
            respond, None, (), {"msg_history": [{"role": "user", "content": "private-question"}]},
        )
        assert result is response
        span, = exporter.get_finished_spans()
        attrs = dict(span.attributes)
        assert span.status.status_code == StatusCode.OK
        assert attrs["gen_ai.span.kind"] == "LLM"
        assert attrs["gen_ai.usage.input_tokens"] == 3
        assert attrs["gen_ai.usage.output_tokens"] == 5
        assert "raw.input" not in attrs
        assert "raw.output" not in attrs
        if hide_content:
            assert "private-question" not in json.dumps(attrs)
            assert "private-answer" not in json.dumps(attrs)
            assert not any(key.startswith("gen_ai.input.messages") for key in attrs)
            assert not any(key.startswith("gen_ai.output.messages") for key in attrs)
        else:
            assert attrs["gen_ai.input.messages.0.message.content"] == "private-question"
            assert attrs["gen_ai.output.messages.0.message.content"] == "private-answer"
            assert "private-question" in attrs["input.value"]
            assert "private-answer" in attrs["output.value"]
    finally:
        provider.shutdown()


@pytest.fixture
def installed_guardrails():
    from guardrails.llm_providers import PromptCallableBase, AsyncPromptCallableBase
    originals = (PromptCallableBase.__call__, AsyncPromptCallableBase.__call__)
    from fi_instrumentation import TraceConfig
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = GuardrailsInstrumentor()

    def install(**privacy):
        instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**privacy))
        return exporter

    yield install
    instrumentor.uninstrument()
    assert (PromptCallableBase.__call__, AsyncPromptCallableBase.__call__) == originals
    provider.shutdown()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_installed_prompt_stream_is_lazy_and_unchanged(installed_guardrails, asynchronous):
    from guardrails.llm_providers import PromptCallableBase, AsyncPromptCallableBase
    from guardrails.classes.llm.llm_response import LLMResponse

    consumed = []

    def chunks():
        for text in ("first", "second"):
            consumed.append(text)
            yield text

    async def async_chunks():
        for text in ("first", "second"):
            consumed.append(text)
            yield text

    response = LLMResponse(output="", **(
        {"async_stream_output": async_chunks()} if asynchronous
        else {"stream_output": chunks()}
    ))

    class LocalPrompt(PromptCallableBase):
        def _invoke_llm(self, *args, **kwargs):
            return response

    class LocalAsyncPrompt(AsyncPromptCallableBase):
        async def invoke_llm(self, *args, **kwargs):
            return response

    exporter = installed_guardrails()
    result = await LocalAsyncPrompt()() if asynchronous else LocalPrompt()()
    assert result is response
    assert consumed == []  # The hook must not eagerly serialize either iterator.
    if asynchronous:
        iterator = result.async_stream_output
        assert await iterator.__anext__() == "first"
        assert consumed == ["first"]
        assert [part async for part in iterator] == ["second"]
    else:
        iterator = result.stream_output
        assert next(iterator) == "first"
        assert consumed == ["first"]
        assert list(iterator) == ["second"]
    assert consumed == ["first", "second"]
    span, = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.OK
    assert span.attributes["gen_ai.span.kind"] == "LLM"
    assert "first" not in json.dumps(dict(span.attributes))


@pytest.mark.parametrize("hide_inputs,hide_outputs", [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize("custom_handler", [False, True])
def test_installed_validation_privacy_and_custom_handler(
    installed_guardrails, monkeypatch, hide_inputs, hide_outputs, custom_handler,
):
    from guardrails.validator_base import Validator, validators_registry
    from guardrails.validator_service import ValidatorServiceBase
    from guardrails.classes.credentials import Credentials
    from guardrails.classes.validation.validator_logs import ValidatorLogs
    from guardrails.classes.validation.validation_result import PassResult
    from guardrails.types import OnFailAction

    # Exercise the real SDK constructor and method, replacing only local config
    # loading to avoid the user's credentials and the SDK's own hub telemetry.
    monkeypatch.setattr(Credentials, "from_rc_file", lambda: Credentials(
        no_metrics=True, enable_metrics=False, use_remote_inferencing=False,
    ))
    monkeypatch.setitem(validators_registry, "fi/dataset_embeddings", Validator)
    monkeypatch.setattr(Validator, "rail_alias", "fi/dataset_embeddings")
    callback = lambda value, failures: value
    validator = Validator(on_fail=callback if custom_handler else OnFailAction.NOOP, use_local=True)
    assert validator.on_fail_descriptor == ("custom" if custom_handler else OnFailAction.NOOP)
    if custom_handler:
        assert validator.on_fail_method is callback
    service = object.__new__(ValidatorServiceBase)
    service._disable_tracer = True
    logs = ValidatorLogs(validator_name="LocalValidator", registered_name=validator.rail_alias,
                         value_before_validation="private-question", property_path="$")
    result = PassResult(metadata={"user_message": "private-question", "nested": {
        "secret": "private-metadata",
    }, "score": 0.95})
    exporter = installed_guardrails(hide_inputs=hide_inputs, hide_outputs=hide_outputs)
    # Keyword invocation is supported as well as positional invocation.
    returned = service.after_run_validator(validator=validator, validator_logs=logs, result=result)
    assert returned is logs
    assert logs.validation_result is result
    assert logs.end_time is not None
    span, = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.OK
    attrs = dict(span.attributes)
    assert attrs["validator_on_fail"] == ("custom" if custom_handler else "NOOP")
    assert attrs["validator_name"] == "fi/dataset_embeddings"
    assert "user_message" not in attrs
    assert "score" not in attrs
    assert "private-metadata" not in json.dumps(attrs)
    assert attrs["input.value"] == ("__REDACTED__" if hide_inputs else "private-question")
    assert attrs["output.value"] == ("__REDACTED__" if hide_outputs else "pass")
    if hide_inputs:
        assert "private-question" not in json.dumps(attrs)


def test_installed_validation_survives_telemetry_extraction_failure(installed_guardrails):
    from guardrails.validator_base import Validator
    from guardrails.validator_service import ValidatorServiceBase
    from guardrails.classes.validation.validator_logs import ValidatorLogs
    from guardrails.classes.validation.validation_result import PassResult

    class BrokenTelemetryValidator(Validator):
        @property
        def on_fail_descriptor(self):
            raise ValueError("synthetic telemetry failure")

    validator = object.__new__(BrokenTelemetryValidator)
    validator.rail_alias = "test/local"
    logs = ValidatorLogs(validator_name="Local", registered_name="test/local",
                         value_before_validation="question", property_path="$")
    result = PassResult()
    service = object.__new__(ValidatorServiceBase)
    service._disable_tracer = True
    exporter = installed_guardrails()
    assert service.after_run_validator(validator, logs, result) is logs
    assert logs.validation_result is result
    span, = exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.OK


def test_iteration_output_projection_does_not_serialize_history_or_consume_stream(monkeypatch):
    from guardrails.classes.history.iteration import Iteration
    from guardrails.classes.history.inputs import Inputs
    from guardrails.classes.history.outputs import Outputs
    from guardrails.classes.llm.llm_response import LLMResponse

    consumed = []

    def chunks():
        consumed.append(True)
        yield "unconsumed-answer"

    iterator = chunks()
    output = {"metadata": {"application_field": "kept"}, "count": 0}
    response = LLMResponse(output="", stream_output=iterator)
    iteration = Iteration(call_id="local", index=0,
                          inputs=Inputs(metadata={"secret": "private-history"}),
                          outputs=Outputs(llm_response_info=response, guarded_output=output))
    original_output = iteration.outputs.guarded_output
    original_stream = response.stream_output

    def forbidden_serializer(*args, **kwargs):
        raise AssertionError("Mixed history serializers must never be invoked")

    for cls in (Iteration, Outputs, LLMResponse):
        monkeypatch.setattr(cls, "to_dict", forbidden_serializer)
    assert json.loads(_get_raw_output(iteration)) == {
        "response": {"rawOutput": "", "guardedOutput": output},
    }
    assert consumed == []
    assert iteration.inputs.metadata == {"secret": "private-history"}
    assert iteration.outputs.guarded_output is original_output
    assert response.stream_output is original_stream
    assert list(original_stream) == ["unconsumed-answer"]
    assert consumed == [True]


@pytest.mark.parametrize("hide_inputs,hide_outputs", [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize("validation", ["none", "pass", "fix"])
def test_installed_complete_guard_privacy(
    installed_guardrails, monkeypatch, hide_inputs, hide_outputs, validation,
):
    """Runner.step must not re-export inputs or validator metadata as output."""
    from guardrails import Guard
    from guardrails.classes.credentials import Credentials
    from guardrails.classes.validation.validation_result import PassResult, FailResult
    from guardrails.types import OnFailAction
    from guardrails.validator_base import Validator, validators_registry

    monkeypatch.setattr(Credentials, "from_rc_file", lambda: Credentials(
        no_metrics=True, enable_metrics=False, use_remote_inferencing=False,
    ))
    question = "private-prompt-variable"
    input_metadata = "private-input-metadata"
    validator_metadata = "private-validator-metadata"
    answer = "original-local-answer"
    corrected = "corrected-local-answer"
    validation_calls = []
    llm_calls = []

    class LocalValidator(Validator):
        rail_alias = "test/local-privacy"

        def validate(self, value, metadata):
            assert value == answer
            assert metadata["context"] == input_metadata
            validation_calls.append(value)
            private = {"nested": {"secret": validator_metadata}}
            if validation == "fix":
                return FailResult(error_message="local correction", fix_value=corrected,
                                  metadata=private)
            return PassResult(metadata=private)

    monkeypatch.setitem(validators_registry, LocalValidator.rail_alias, LocalValidator)

    def respond(prompt, **kwargs):
        assert prompt == question
        llm_calls.append(prompt)
        return answer

    def invoke():
        guard = Guard()
        guard.configure(allow_metrics_collection=False)
        if validation != "none":
            guard.use(LocalValidator(on_fail=OnFailAction.FIX, use_local=True))
        result = guard(llm_api=respond, prompt="${question}",
                       prompt_params={"question": question},
                       metadata={"context": input_metadata}, num_reasks=0)
        iteration, = guard.history.last.iterations
        if validation != "none":
            # The real SDK retains the private result metadata in history; the
            # instrumentation must not export it by serializing that history.
            log, = iteration.outputs.validator_logs
            assert log.validation_result.metadata["nested"]["secret"] == validator_metadata
        return (result.raw_llm_output, result.validated_output, result.validation_passed)

    baseline = invoke()  # Compare with the same complete uninstrumented Guard.
    exporter = installed_guardrails(hide_inputs=hide_inputs, hide_outputs=hide_outputs)
    assert invoke() == baseline == (answer, corrected if validation == "fix" else answer, True)
    assert llm_calls == [question, question]
    assert validation_calls == ([] if validation == "none" else [answer, answer])

    spans = exporter.get_finished_spans()
    assert len(spans) == (2 if validation == "none" else 3)
    runner, = [span for span in spans if span.name == "Runner.step"]
    assert runner.parent is None
    assert all(span.parent.span_id == runner.context.span_id
               for span in spans if span is not runner)
    for span in spans:
        assert span.status.status_code == StatusCode.OK
        attrs = dict(span.attributes)
        serialized = json.dumps(attrs)
        assert validator_metadata not in serialized
        assert "raw.input" not in attrs and "raw.output" not in attrs
        # Prompt parameters and input metadata belong exclusively to input
        # attributes, regardless of whether output capture is enabled.
        for key, value in attrs.items():
            if question in str(value) or input_metadata in str(value):
                assert not hide_inputs
                assert key == "input.value" or key.startswith("gen_ai.input.messages.")
        if hide_inputs:
            assert attrs["input.value"] == "__REDACTED__"
        if hide_outputs:
            assert attrs["output.value"] == "__REDACTED__"
            assert not any(key.startswith("gen_ai.output.messages.") for key in attrs)

    if not hide_inputs:
        assert question in runner.attributes["input.value"]
        assert input_metadata in runner.attributes["input.value"]
    if not hide_outputs:
        projected = json.loads(runner.attributes["output.value"])["response"]
        assert projected == {"rawOutput": answer,
                             "guardedOutput": corrected if validation == "fix" else answer}


@pytest.mark.parametrize("hide_inputs,hide_outputs", [(False, False), (True, False), (False, True), (True, True)])
def test_installed_complete_guard_reask_privacy(
    installed_guardrails, monkeypatch, hide_inputs, hide_outputs,
):
    """Reasks capture current inputs, not earlier validation/history objects."""
    from guardrails import Guard
    from guardrails.classes.credentials import Credentials
    from guardrails.classes.validation.validation_result import PassResult, FailResult
    from guardrails.types import OnFailAction
    from guardrails.validator_base import Validator, validators_registry

    monkeypatch.setattr(Credentials, "from_rc_file", lambda: Credentials(
        no_metrics=True, enable_metrics=False, use_remote_inferencing=False,
    ))
    question = "private-reask-question"
    application_metadata = {"context": "private-reask-input-metadata",
                            "metadata": {"application_field": "kept"},
                            "count": 0, "enabled": False}
    first_answer, final_answer = "first-local-answer", "final-local-answer"
    private_results = {first_answer: "private-failed-validator-metadata",
                       final_answer: "private-passed-validator-metadata"}
    llm_calls, validation_calls = [], []

    class LocalValidator(Validator):
        rail_alias = "test/local-reask-privacy"

        def validate(self, value, metadata):
            assert metadata["context"] == application_metadata["context"]
            validation_calls.append(value)
            private = {"nested": {"api_key": private_results[value]}}
            if value == first_answer:
                return FailResult(error_message="Please correct the answer", metadata=private)
            return PassResult(metadata=private)

    monkeypatch.setitem(validators_registry, LocalValidator.rail_alias, LocalValidator)

    def invoke():
        answers = iter((first_answer, final_answer))

        def respond(prompt, **kwargs):
            llm_calls.append(prompt)
            return next(answers)

        guard = Guard()
        guard.configure(allow_metrics_collection=False)
        guard.use(LocalValidator(on_fail=OnFailAction.REASK, use_local=True))
        result = guard(llm_api=respond, prompt="${question}",
                       prompt_params={"question": question},
                       metadata=application_metadata.copy(), num_reasks=1)
        iterations = guard.history.last.iterations
        assert len(iterations) == 2
        assert guard.history.last.inputs.metadata == application_metadata
        for iteration, answer in zip(iterations, (first_answer, final_answer)):
            log, = iteration.outputs.validator_logs
            # Private diagnostics remain available to the application. The
            # repair must not mutate history to remove them from telemetry.
            assert log.validation_result.metadata == {
                "nested": {"api_key": private_results[answer]},
            }
            assert iteration.raw_output == answer
        assert iterations[0].outputs.reasks
        assert not iterations[1].outputs.reasks
        return result.raw_llm_output, result.validated_output, result.validation_passed

    baseline = invoke()
    exporter = installed_guardrails(hide_inputs=hide_inputs, hide_outputs=hide_outputs)
    assert invoke() == baseline == (final_answer, final_answer, True)
    assert validation_calls == [first_answer, final_answer] * 2
    assert len(llm_calls) == 4
    assert llm_calls[:2] == llm_calls[2:]
    assert llm_calls[0] == question
    assert first_answer in llm_calls[1]  # This is a genuine current reask prompt.

    spans = exporter.get_finished_spans()
    assert len(spans) == 6
    runners = [span for span in spans if span.name == "Runner.step"]
    assert len(runners) == 2
    for runner in runners:
        assert runner.parent is None
        children = [span for span in spans if span.parent is not None
                    and span.parent.span_id == runner.context.span_id]
        assert len(children) == 2
        assert sorted(span.attributes["gen_ai.span.kind"] for span in children) == ["GUARDRAIL", "LLM"]
    for span in spans:
        assert span.status.status_code == StatusCode.OK
        attrs = dict(span.attributes)
        serialized = json.dumps(attrs)
        assert all(secret not in serialized for secret in private_results.values())
        assert "raw.input" not in attrs and "raw.output" not in attrs
        for key, value in attrs.items():
            if question in str(value) or application_metadata["context"] in str(value):
                assert not hide_inputs
                assert key == "input.value" or key.startswith("gen_ai.input.messages.")
        if hide_inputs:
            assert attrs["input.value"] == "__REDACTED__"
            assert not any(key.startswith("gen_ai.input.messages.") for key in attrs)
        if hide_outputs:
            assert attrs["output.value"] == "__REDACTED__"
            assert not any(key.startswith("gen_ai.output.messages.") for key in attrs)
    for index, runner in enumerate(runners):
        if not hide_inputs:
            inputs = json.loads(runner.attributes["input.value"])
            assert inputs["index"] == index
            assert inputs["prompt_params"] == {"question": question}
            assert inputs["metadata"] == application_metadata
            assert "call_log" not in inputs and "api" not in inputs
            assert inputs["prompt"] == ("${question}" if index == 0 else llm_calls[1])
            assert not any(key in runner.attributes["input.value"] for key in (
                "validatorLogs", "validationResponse", "failResults", "iterations",
            ))
        if not hide_outputs:
            assert json.loads(runner.attributes["output.value"])["response"] == {
                "rawOutput": first_answer if index == 0 else final_answer,
                "guardedOutput": None if index == 0 else final_answer,
            }


@pytest.mark.parametrize("positional", [False, True])
def test_step_input_projection_never_serializes_history(monkeypatch, positional):
    from guardrails.classes.history.call import Call
    from guardrails.classes.history.call_inputs import CallInputs
    from guardrails.classes.history.iteration import Iteration
    from guardrails.classes.history.outputs import Outputs
    from guardrails.classes.llm.llm_response import LLMResponse
    from guardrails.prompt import Prompt, Instructions

    consumed = []

    def chunks():
        consumed.append(True)
        yield "unconsumed-answer"

    response = LLMResponse(output="", stream_output=chunks())
    original_stream = response.stream_output
    iteration = Iteration(call_id="local", index=0,
                          outputs=Outputs(llm_response_info=response))
    metadata = {"context": "application-metadata", "count": 0, "enabled": False}
    history = Call(inputs=CallInputs(metadata=metadata, prompt_params={"old": "unused"}))
    history.iterations.push(iteration)

    def forbidden_serializer(*args, **kwargs):
        raise AssertionError("Mixed history serializers must never be invoked")

    for cls in (Call, CallInputs, Iteration, Outputs, LLMResponse):
        monkeypatch.setattr(cls, "to_dict", forbidden_serializer)

    def step(index, output_schema, call_log, *, api, instructions, prompt,
             msg_history=None, prompt_params=None, output=None):
        assert index == 1
        assert call_log is history
        return iteration

    args = (1, {"type": "string"}, history) if positional else ()
    kwargs = {"api": object(), "instructions": Instructions("current-instructions"),
              "prompt": Prompt("current-prompt"),
              "msg_history": [{"role": "user", "content": Prompt("current-message")}],
              "prompt_params": {"current": "parameter"}, "output": "provided-output"}
    if not positional:
        kwargs.update(index=1, output_schema={"type": "string"}, call_log=history)
    tracer = MagicMock(spec=trace_api.Tracer)
    wrapper = _ParseCallableWrapper(tracer=tracer)
    assert wrapper(step, None, args, kwargs) is iteration
    attrs = tracer.start_as_current_span.call_args.kwargs["attributes"]
    assert json.loads(attrs["input.value"]) == {
        "index": 1, "output_schema": {"type": "string"},
        "instructions": "current-instructions", "prompt": "current-prompt",
        "msg_history": [{"role": "user", "content": "current-message"}],
        "prompt_params": {"current": "parameter"}, "output": "provided-output",
        "metadata": metadata,
    }
    assert consumed == []
    assert response.stream_output is original_stream
    assert history.iterations.last is iteration
    assert history.inputs.prompt_params == {"old": "unused"}
    assert list(original_stream) == ["unconsumed-answer"]
    assert consumed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("hide_inputs,hide_outputs", [(False, False), (True, False), (False, True), (True, True)])
async def test_installed_complete_guard_current_messages_and_privacy(
    installed_guardrails, monkeypatch, asynchronous, hide_inputs, hide_outputs,
):
    """Exercise real messages-based SDKs and the retained prompt-based floor."""
    from inspect import signature
    from guardrails import Guard, AsyncGuard

    # Guardrails renamed its local configuration and current-prompt API after
    # 0.5.0. Exercise the installed API, never skip a supported release or replace
    # the SDK method being instrumented. Modern cells must take the messages path.
    messages_api = "messages" in signature(Guard.__call__).parameters
    if messages_api:
        from guardrails.classes.rc import RC
        monkeypatch.setattr(RC, "load", lambda *args, **kwargs: RC(
            enable_metrics=False, use_remote_inferencing=False,
        ))
    else:
        from guardrails.classes.credentials import Credentials
        monkeypatch.setattr(Credentials, "from_rc_file", lambda: Credentials(
            no_metrics=True, enable_metrics=False, use_remote_inferencing=False,
        ))

    question, answer = "private-current-message-question", "private-current-message-answer"
    messages = [{"role": "user", "content": question}]
    observed = []

    def respond(*args, messages=None, **kwargs):
        if messages_api:
            assert args == ()
            assert messages == [{"role": "user", "content": question}]
            observed.append(messages)
        else:
            assert args == (question,)
            observed.append(args[0])
        return answer

    async def async_respond(*args, messages=None, **kwargs):
        return respond(*args, messages=messages, **kwargs)

    async def invoke():
        guard = AsyncGuard() if asynchronous else Guard()
        guard.configure(allow_metrics_collection=False)
        inputs = {"messages": messages} if messages_api else {"prompt": question}
        result = guard(llm_api=async_respond if asynchronous else respond,
                       num_reasks=0, **inputs)
        if asynchronous:
            result = await result
        assert guard.history.last.iterations.last.raw_output == answer
        return result.raw_llm_output, result.validated_output, result.validation_passed

    baseline = await invoke()
    exporter = installed_guardrails(hide_inputs=hide_inputs, hide_outputs=hide_outputs)
    assert await invoke() == baseline == (answer, answer, True)
    assert observed == ([messages, messages] if messages_api else [question, question])
    spans = exporter.get_finished_spans()
    assert len(spans) == (1 if asynchronous else 2)
    prompt, = [span for span in spans if span.attributes["gen_ai.span.kind"] == "LLM"]
    if asynchronous:
        assert prompt.parent is None
    else:
        runner, = [span for span in spans if span.name == "Runner.step"]
        assert runner.parent is None
        assert prompt.parent.span_id == runner.context.span_id
        if not hide_inputs:
            inputs = json.loads(runner.attributes["input.value"])
            if messages_api:
                assert inputs["messages"] == messages
            else:
                assert inputs["prompt"] == question
            assert "call_log" not in inputs and "api" not in inputs

    for span in spans:
        assert span.status.status_code == StatusCode.OK
        attrs = dict(span.attributes)
        # Check every exported attribute, not only canonical fields. Native SDK
        # llm.* attributes must not provide a second ungoverned copy of payloads.
        for key, value in attrs.items():
            if question in str(value):
                assert not hide_inputs
                assert key == "input.value" or key.startswith("gen_ai.input.messages.")
            if answer in str(value):
                assert not hide_outputs
                assert key == "output.value" or key.startswith("gen_ai.output.messages.")
        assert not any(key.startswith("llm.") for key in attrs)
        if hide_inputs:
            assert question not in json.dumps(attrs)
        else:
            assert question in attrs["input.value"]
        if hide_outputs:
            assert answer not in json.dumps(attrs)
        else:
            assert answer in attrs["output.value"]
        if hide_inputs:
            assert attrs["input.value"] == "__REDACTED__"
        if hide_outputs:
            assert attrs["output.value"] == "__REDACTED__"
    if not hide_inputs and messages_api:
        assert prompt.attributes["gen_ai.input.messages.0.message.content"] == question
    if not hide_outputs:
        assert prompt.attributes["gen_ai.output.messages.0.message.content"] == answer


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("hide_inputs,hide_outputs", [(False, False), (True, False), (False, True), (True, True)])
async def test_installed_prompt_native_payload_filter_is_scoped(
    installed_guardrails, asynchronous, hide_inputs, hide_outputs,
):
    from guardrails.llm_providers import PromptCallableBase, AsyncPromptCallableBase
    from guardrails.classes.llm.llm_response import LLMResponse
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    question, answer, diagnostic = "native-private-question", "native-private-answer", "opaque-sdk-state"
    response = LLMResponse(output=answer, prompt_token_count=3, response_token_count=5)
    app_exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(app_exporter))
    tracer = provider.get_tracer(__name__)

    def backend():
        current = trace_api.get_current_span()
        # Cover both native setter APIs and all opaque legacy payload families.
        current.set_attribute("llm.input_messages.0.message.content", question)
        current.set_attributes({
            "llm.output_messages.0.message.content": answer,
            "llm.invocation_parameters": json.dumps({"messages": question, "state": diagnostic}),
            "llm.prompt_template.template": diagnostic,
            "llm.prompt_template.variables": diagnostic,
            "llm.function_call": diagnostic,
            "llm.model_name": "local-model",
            "llm.token_count.prompt": 3,
            "llm.token_count.completion": 5,
            "llm.token_count.total": 8,
        })
        with tracer.start_as_current_span("downstream") as child:
            child.set_attribute("application.attribute", "retained")
        assert trace_api.get_current_span() is current
        return response

    class LocalPrompt(PromptCallableBase):
        def _invoke_llm(self, *args, **kwargs):
            return backend()

    class LocalAsyncPrompt(AsyncPromptCallableBase):
        async def invoke_llm(self, *args, **kwargs):
            return backend()

    exporter = installed_guardrails(hide_inputs=hide_inputs, hide_outputs=hide_outputs)
    original = context_api.get_current()
    try:
        with tracer.start_as_current_span("application") as parent:
            parent.set_attribute("llm.invocation_parameters", "application-owned")
            kwargs = {"msg_history": [{"role": "user", "content": question}]}
            result = await LocalAsyncPrompt()(**kwargs) if asynchronous else LocalPrompt()(**kwargs)
            assert result is response
            assert trace_api.get_current_span() is parent
        assert context_api.get_current() is original
        span, = exporter.get_finished_spans()
        child, application = app_exporter.get_finished_spans()
        assert span.parent.span_id == application.context.span_id
        assert child.parent.span_id == span.context.span_id
        assert child.attributes["application.attribute"] == "retained"
        assert application.attributes["llm.invocation_parameters"] == "application-owned"
        attrs = dict(span.attributes)
        assert span.status.status_code == StatusCode.OK
        assert attrs["gen_ai.request.model"] == "local-model"
        assert attrs["gen_ai.usage.input_tokens"] == 3
        assert attrs["gen_ai.usage.output_tokens"] == 5
        assert attrs["gen_ai.usage.total_tokens"] == 8
        assert not any(key.startswith("llm.") for key in attrs)
        assert diagnostic not in json.dumps(attrs)
        if hide_inputs:
            assert question not in json.dumps(attrs)
        else:
            assert question in attrs["input.value"]
        if hide_outputs:
            assert answer not in json.dumps(attrs)
        else:
            assert answer in attrs["output.value"]
    finally:
        provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_installed_native_filter_restores_context_on_failure(
    installed_guardrails, asynchronous, cancelled,
):
    import asyncio
    from guardrails.llm_providers import PromptCallableBase, AsyncPromptCallableBase

    failure = asyncio.CancelledError("local cancellation") if cancelled else ValueError("local failure")

    def backend():
        trace_api.get_current_span().set_attribute("llm.invocation_parameters", "private-native-state")
        raise failure

    class LocalPrompt(PromptCallableBase):
        def _invoke_llm(self, *args, **kwargs):
            return backend()

    class LocalAsyncPrompt(AsyncPromptCallableBase):
        async def invoke_llm(self, *args, **kwargs):
            return backend()

    async def invoke():
        if asynchronous:
            await LocalAsyncPrompt()()
        else:
            LocalPrompt()()

    expected = asyncio.CancelledError if cancelled else Exception
    with pytest.raises(expected) as baseline:
        await invoke()
    exporter = installed_guardrails(hide_inputs=True, hide_outputs=True)
    original = context_api.get_current()
    with pytest.raises(type(baseline.value)) as observed:
        await invoke()
    assert str(observed.value) == str(baseline.value)
    if cancelled:
        assert observed.value is failure
    assert context_api.get_current() is original
    span, = exporter.get_finished_spans()
    assert "private-native-state" not in json.dumps(dict(span.attributes))
    if not cancelled:
        assert span.status.status_code == StatusCode.ERROR


@pytest.mark.parametrize("positional", [False, True])
def test_step_current_messages_projection_excludes_diagnostics(monkeypatch, positional):
    from guardrails.classes.history.call import Call
    from guardrails.classes.history.call_inputs import CallInputs
    from guardrails.prompt import Prompt
    from traceai_guardrails._wrap_guard_call import _get_step_input

    history = Call(inputs=CallInputs(metadata={"application": "kept"}))

    def forbidden_serializer(*args, **kwargs):
        raise AssertionError("Current messages must not serialize diagnostic history")

    monkeypatch.setattr(Call, "to_dict", forbidden_serializer)
    def step(index, output_schema, call_log, *, messages, api, prompt_params):
        pass

    messages = [{"role": "user", "content": [Prompt("current-message"),
                 {"text": Prompt("nested-message"), "enabled": False}]}]
    args = (0, {"type": "string"}, history) if positional else ()
    kwargs = {"messages": messages, "api": object(), "prompt_params": {"count": 0}}
    if not positional:
        kwargs.update(index=0, output_schema={"type": "string"}, call_log=history)
    assert json.loads(_get_step_input(step, *args, **kwargs)) == {
        "index": 0, "output_schema": {"type": "string"},
        "messages": [{"role": "user", "content": ["current-message",
                      {"text": "nested-message", "enabled": False}]}],
        "prompt_params": {"count": 0}, "metadata": {"application": "kept"},
    }
    assert isinstance(messages[0]["content"][0], Prompt)


if __name__ == "__main__":
    pytest.main([__file__])
