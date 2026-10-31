"""
Test suite for Groq framework instrumentation.

Tests the instrumentation of Groq components:
- Completions.create (LLM spans)
- AsyncCompletions.create (async LLM spans)
- Protect.protect (guardrail protection)
- Streaming and non-streaming response handling
- Request/response attribute extraction
"""

import pytest
from unittest.mock import Mock, MagicMock, patch, AsyncMock
from typing import AsyncGenerator
import json

from opentelemetry import trace as trace_api
from opentelemetry.trace.status import StatusCode
from opentelemetry import context as context_api

from traceai_groq import GroqInstrumentor
from traceai_groq._wrappers import (
    _CompletionsWrapper,
    _AsyncCompletionsWrapper,
    _WithTracer,
    _parse_args,
    _flatten,
)
from traceai_groq._utils import (
    _io_value_and_type,
    _as_input_attributes,
    _as_output_attributes,
    _finish_tracing,
    _extract_eval_input,
    _process_response,
    _as_raw_output,
    _as_streaming_output,
    _to_dict,
)
from traceai_groq._request_attributes_extractor import _RequestAttributesExtractor
from traceai_groq._response_attributes_extractor import _ResponseAttributesExtractor
from traceai_groq._with_span import _WithSpan


class TestGroqInstrumentor:
    """Test the main Groq instrumentor."""

    def setup_method(self):
        """Set up test fixtures."""
        self.instrumentor = GroqInstrumentor()

    def test_instrumentation_dependencies(self):
        """Test instrumentation dependencies."""
        deps = self.instrumentor.instrumentation_dependencies()
        assert deps == ("groq >= 0.9.0",)

    @patch("traceai_groq.wrap_function_wrapper")
    @patch("traceai_groq.trace_api.get_tracer")
    def test_instrument_basic(self, mock_get_tracer, mock_wrap_function):
        """Test basic instrumentation setup."""
        mock_tracer = Mock()
        mock_get_tracer.return_value = mock_tracer
        
        # Mock the Protect import to avoid ImportError
        with patch("traceai_groq.Protect") as mock_protect:
            mock_protect.protect = Mock()
            
            self.instrumentor._instrument()
        
        # Verify tracer setup
        mock_get_tracer.assert_called_once()
        
        # Verify function wrapping - should wrap completions and protect
        assert mock_wrap_function.call_count >= 2
        
        # Check that originals are stored
        assert hasattr(self.instrumentor, '_original_completions_create')
        assert hasattr(self.instrumentor, '_original_async_completions_create')

    def test_uninstrument(self):
        """Test uninstrumentation."""
        # Set up mock originals
        self.instrumentor._original_completions_create = Mock()
        self.instrumentor._original_async_completions_create = Mock()
        
        with patch("traceai_groq.import_module") as mock_import:
            mock_module = Mock()
            mock_module.Completions = Mock()
            mock_module.AsyncCompletions = Mock()
            mock_import.return_value = mock_module
            
            self.instrumentor._uninstrument()
            
            # Verify originals are restored
            assert mock_module.Completions.create == self.instrumentor._original_completions_create
            assert mock_module.AsyncCompletions.create == self.instrumentor._original_async_completions_create


class TestCompletionsWrapper:
    """Test the sync completions wrapper."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock(spec=trace_api.Tracer)
        self.mock_span = MagicMock()
        self.wrapper = _CompletionsWrapper(tracer=self.mock_tracer)

    def test_completions_wrapper_basic(self):
        """Test basic completions wrapper operation."""
        # Mock request parameters
        request_params = {
            "messages": [{"role": "user", "content": "Hello"}],
            "model": "llama-3.1-70b-versatile",
            "stream": False
        }
        
        # Mock response
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message = Mock()
        mock_response.choices[0].message.content = "Hello! How can I help?"
        
        def wrapped_func(*args, **kwargs):
            return mock_response
        
        # Mock the span context manager
        mock_span_context = MagicMock()
        mock_with_span = Mock()
        mock_span_context.__enter__.return_value = mock_with_span
        mock_span_context.__exit__.return_value = None
        self.wrapper._start_as_current_span = Mock(return_value=mock_span_context)
        
        # Test the wrapper
        result = self.wrapper(wrapped_func, None, (), request_params)
        
        assert result == mock_response
        self.wrapper._start_as_current_span.assert_called_once()

    def test_completions_wrapper_streaming(self):
        """Test streaming completions wrapper operation."""
        # Mock request parameters
        request_params = {
            "messages": [{"role": "user", "content": "Hello"}],
            "model": "llama-3.1-70b-versatile",
            "stream": True
        }
        
        # Mock streaming response
        mock_chunk1 = Mock()
        mock_chunk1.choices = [{"delta": {"content": "Hello"}}]
        mock_chunk2 = Mock()
        mock_chunk2.choices = [{"delta": {"content": " there!"}}]
        
        def mock_streaming_response():
            yield mock_chunk1
            yield mock_chunk2
        
        def wrapped_func(*args, **kwargs):
            return mock_streaming_response()
        
        # Mock the span context manager
        mock_span_context = MagicMock()
        mock_with_span = Mock()
        mock_span_context.__enter__.return_value = mock_with_span
        mock_span_context.__exit__.return_value = None
        self.wrapper._start_as_current_span = Mock(return_value=mock_span_context)
        
        # Test the wrapper
        result = self.wrapper(wrapped_func, None, (), request_params)
        
        # Consume the generator
        chunks = list(result)
        assert len(chunks) == 2

    def test_completions_wrapper_exception(self):
        """Test completions wrapper exception handling."""
        def wrapped_func(*args, **kwargs):
            raise ValueError("Test error")
        
        # Mock the span context manager
        mock_span_context = MagicMock()
        mock_with_span = Mock()
        mock_span_context.__enter__.return_value = mock_with_span
        mock_span_context.__exit__.return_value = None
        self.wrapper._start_as_current_span = Mock(return_value=mock_span_context)
        
        # Test exception handling
        with pytest.raises(ValueError, match="Test error"):
            self.wrapper(wrapped_func, None, (), {})
        
        # Verify exception was recorded
        mock_with_span.record_exception.assert_called_once()

    def test_completions_wrapper_suppressed(self):
        """Test completions wrapper when instrumentation is suppressed."""
        with patch("traceai_groq._wrappers.context_api.get_value") as mock_get_value:
            mock_get_value.return_value = True
            
            mock_response = Mock()
            def wrapped_func(*args, **kwargs):
                return mock_response
            
            result = self.wrapper(wrapped_func, None, (), {})
            
            assert result == mock_response


class TestAsyncCompletionsWrapper:
    """Test the async completions wrapper."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock(spec=trace_api.Tracer)
        self.wrapper = _AsyncCompletionsWrapper(tracer=self.mock_tracer)

    @pytest.mark.asyncio
    async def test_async_completions_wrapper_basic(self):
        """Test basic async completions wrapper operation."""
        # Mock request parameters
        request_params = {
            "messages": [{"role": "user", "content": "Hello"}],
            "model": "llama-3.1-70b-versatile",
            "stream": False
        }
        
        # Mock response
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message = Mock()
        mock_response.choices[0].message.content = "Hello! How can I help?"
        
        async def wrapped_func(*args, **kwargs):
            return mock_response
        
        # Mock the span context manager
        mock_span_context = MagicMock()
        mock_with_span = Mock()
        mock_span_context.__enter__.return_value = mock_with_span
        mock_span_context.__exit__.return_value = None
        self.wrapper._start_as_current_span = Mock(return_value=mock_span_context)
        
        # Test the wrapper
        result = await self.wrapper(wrapped_func, None, (), request_params)
        
        assert result == mock_response
        self.wrapper._start_as_current_span.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_completions_wrapper_streaming(self):
        """Test async streaming completions wrapper operation."""
        # Mock request parameters
        request_params = {
            "messages": [{"role": "user", "content": "Hello"}],
            "model": "llama-3.1-70b-versatile",
            "stream": True
        }
        
        # Mock streaming response
        async def mock_streaming_response():
            yield {"choices": [{"delta": {"content": "Hello"}}]}
            yield {"choices": [{"delta": {"content": " there!"}}]}
        
        async def wrapped_func(*args, **kwargs):
            return mock_streaming_response()
        
        # Mock the span context manager
        mock_span_context = MagicMock()
        mock_with_span = Mock()
        mock_span_context.__enter__.return_value = mock_with_span
        mock_span_context.__exit__.return_value = None
        self.wrapper._start_as_current_span = Mock(return_value=mock_span_context)
        
        # Test the wrapper
        result = await self.wrapper(wrapped_func, None, (), request_params)
        
        # Consume the async generator
        chunks = []
        async for chunk in result:
            chunks.append(chunk)
        assert len(chunks) == 2

    @pytest.mark.asyncio
    async def test_async_completions_wrapper_exception(self):
        """Test async completions wrapper exception handling."""
        async def wrapped_func(*args, **kwargs):
            raise ValueError("Test error")
        
        # Mock the span context manager
        mock_span_context = MagicMock()
        mock_with_span = Mock()
        mock_span_context.__enter__.return_value = mock_with_span
        mock_span_context.__exit__.return_value = None
        self.wrapper._start_as_current_span = Mock(return_value=mock_span_context)
        
        # Test exception handling
        with pytest.raises(ValueError, match="Test error"):
            await self.wrapper(wrapped_func, None, (), {})
        
        # Verify exception was recorded
        mock_with_span.record_exception.assert_called_once()


class TestRequestAttributesExtractor:
    """Test the request attributes extractor."""

    def setup_method(self):
        """Set up test fixtures."""
        self.extractor = _RequestAttributesExtractor()

    def test_get_attributes_from_request(self):
        """Test extracting attributes from request."""
        request_params = {
            "messages": [
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": "Hello"}
            ],
            "model": "llama-3.1-70b-versatile",
            "temperature": 0.7,
            "max_tokens": 100
        }
        
        attributes = list(self.extractor.get_attributes_from_request(request_params))
        
        # Convert to dict for easier testing
        attr_dict = dict(attributes)
        
        # Should contain some attributes (exact keys depend on implementation)
        assert len(attributes) > 0
        # Check for span kind
        assert attr_dict["gen_ai.span.kind"] == "LLM"

    def test_get_extra_attributes_from_request(self):
        """Test extracting extra attributes from request."""
        request_params = {
            "messages": [{"role": "user", "content": "Hello"}],
            "model": "llama-3.1-70b-versatile"
        }
        
        extra_attributes = list(self.extractor.get_extra_attributes_from_request(request_params))
        
        # Should return valid attributes
        assert isinstance(extra_attributes, list)


class TestResponseAttributesExtractor:
    """Test the response attributes extractor."""

    def setup_method(self):
        """Set up test fixtures."""
        self.extractor = _ResponseAttributesExtractor()

    def test_get_attributes_from_response(self):
        """Test extracting attributes from response."""
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message = Mock()
        mock_response.choices[0].message.content = "Hello! How can I help?"
        mock_response.usage = Mock()
        mock_response.usage.total_tokens = 50
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 40
        
        attributes = list(self.extractor.get_attributes(response=mock_response, is_streaming=False))
        
        # Should contain output attributes
        assert len(attributes) > 0

    def test_get_extra_attributes_from_response(self):
        """Test extracting extra attributes from response."""
        mock_response = Mock()
        request_params = {"model": "llama-3.1-70b-versatile"}
        
        extra_attributes = list(self.extractor.get_extra_attributes(
            response=mock_response,
            request_parameters=request_params,
            is_streaming=False
        ))
        
        # Should return valid attributes
        assert isinstance(extra_attributes, list)


class TestUtilityFunctions:
    """Test utility functions."""

    def test_io_value_and_type_json(self):
        """Test _io_value_and_type with JSON-serializable object."""
        mock_obj = Mock()
        mock_obj.model_dump_json.return_value = '{"test": "value"}'
        
        result = _io_value_and_type(mock_obj)
        
        assert result.value == '{"test": "value"}'
        assert result.type.value == "application/json"

    def test_io_value_and_type_dict(self):
        """Test _io_value_and_type with dict."""
        test_dict = {"test": "value"}
        
        result = _io_value_and_type(test_dict)
        
        assert '"test": "value"' in result.value
        assert result.type.value == "application/json"

    def test_io_value_and_type_string(self):
        """Test _io_value_and_type with string."""
        test_string = "Hello world"
        
        result = _io_value_and_type(test_string)
        
        assert result.value == "Hello world"
        assert result.type.value == "text/plain"

    def test_as_input_attributes(self):
        """Test _as_input_attributes function."""
        from traceai_groq._utils import _ValueAndType
        from fi_instrumentation.fi_types import FiMimeTypeValues
        
        value_and_type = _ValueAndType("test input", FiMimeTypeValues.JSON)
        
        attributes = list(_as_input_attributes(value_and_type))
        
        assert len(attributes) == 2
        attr_dict = dict(attributes)
        assert "input.value" in attr_dict
        assert "input.mime_type" in attr_dict

    def test_as_output_attributes(self):
        """Test _as_output_attributes function."""
        from traceai_groq._utils import _ValueAndType
        from fi_instrumentation.fi_types import FiMimeTypeValues
        
        value_and_type = _ValueAndType("test output", FiMimeTypeValues.TEXT)
        
        attributes = list(_as_output_attributes(value_and_type))
        
        assert len(attributes) == 1  # Only value, no mime_type for TEXT
        attr_dict = dict(attributes)
        assert "output.value" in attr_dict

    def test_extract_eval_input(self):
        """Test _extract_eval_input function."""
        messages = [
            {"content": "First message"},
            {"content": "Second message"},
            {"role": "user", "content": "User message"}
        ]
        
        attributes = list(_extract_eval_input(messages))
        
        attr_dict = dict(attributes)
        assert attr_dict == {"input.value": "First message\nSecond message\nUser message"}
        assert len(attributes) == len(attr_dict)  # No duplicate canonical keys.

    def test_process_response_dict(self):
        """Test _process_response with dict response."""
        response = {
            "choices": [
                {
                    "message": {
                        "content": "Test response content"
                    }
                }
            ]
        }
        
        result = _process_response(response)
        
        assert result == "Test response content"

    def test_process_response_object(self):
        """Test _process_response with object response."""
        mock_message = Mock()
        mock_message.content = "Test response content"
        
        mock_choice = Mock()
        mock_choice.message = mock_message
        
        mock_response = Mock()
        mock_response.choices = [mock_choice]
        
        result = _process_response(mock_response)
        
        assert result == "Test response content"

    def test_process_response_invalid(self):
        """Test _process_response with invalid response."""
        result = _process_response(None)
        
        assert result is None or result == ""

    def test_as_raw_output(self):
        """Test _as_raw_output function."""
        mock_response = Mock()
        mock_response.model_dump.return_value = {"test": "value"}
        
        attributes = list(_as_raw_output(mock_response))
        
        assert len(attributes) == 1
        key, value = attributes[0]
        assert key == "output.value"
        assert json.loads(value) == {"test": "value"}

    def test_as_streaming_output(self):
        """Test _as_streaming_output function."""
        # Mock ChatCompletionChunk objects
        mock_chunk1 = Mock()
        mock_chunk1.to_dict.return_value = {
            "choices": [{"delta": {"content": "Hello"}}],
            "model": "llama-3.1-70b-versatile"
        }
        
        mock_chunk2 = Mock()
        mock_chunk2.to_dict.return_value = {
            "choices": [{"delta": {"content": " world!"}}],
            "x_groq": {
                "usage": {
                    "total_tokens": 50,
                    "prompt_tokens": 10,
                    "completion_tokens": 40
                }
            },
            "model": "llama-3.1-70b-versatile"
        }
        
        chunks = [mock_chunk1, mock_chunk2]
        
        attributes = list(_as_streaming_output(chunks))
        
        attr_dict = dict(attributes)
        assert "output.value" in attr_dict
        assert attr_dict["output.value"] == "Hello world!"
        assert attr_dict["gen_ai.response.model"] == "llama-3.1-70b-versatile"
        assert attr_dict["gen_ai.usage.total_tokens"] == 50
        assert attr_dict["gen_ai.usage.input_tokens"] == 10
        assert attr_dict["gen_ai.usage.output_tokens"] == 40
        assert len(attributes) == len(attr_dict)  # Text must not be overwritten by raw chunks.
        assert "raw.output" not in attr_dict

    def test_to_dict_with_model_dump(self):
        """Test _to_dict with object that has __dict__."""
        class SimpleObj:
            def __init__(self):
                self.test = "value"
                self.number = 42
        
        obj = SimpleObj()
        result = _to_dict(obj)
        
        assert result == {"test": "value", "number": 42}

    def test_to_dict_with_dict(self):
        """Test _to_dict with dict object."""
        test_dict = {"test": "value"}
        
        result = _to_dict(test_dict)
        
        assert result == {"test": "value"}


class TestWrapperUtilities:
    """Test wrapper utility functions."""

    def test_parse_args(self):
        """Test _parse_args function."""
        from inspect import signature
        
        def test_func(arg1, arg2="default", *args, **kwargs):
            pass
        
        sig = signature(test_func)
        result = _parse_args(sig, "value1", "value2", extra_kwarg="extra")
        
        expected = {
            "arg1": "value1",
            "arg2": "value2",
            "args": (),
            "kwargs": {"extra_kwarg": "extra"}
        }
        
        assert result == expected

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
        from enum import Enum
        
        class TestEnum(Enum):
            VALUE = "test_value"
        
        test_dict = {
            "enum_field": TestEnum.VALUE
        }
        
        flattened = list(_flatten(test_dict))
        
        attr_dict = dict(flattened)
        assert "enum_field" in attr_dict
        assert attr_dict["enum_field"] == "test_value"


class TestWithSpan:
    """Test _WithSpan utility class."""

    def test_with_span_creation(self):
        """Test _WithSpan creation and basic functionality."""
        mock_span = Mock()
        context_attrs = {"context": "value"}
        extra_attrs = {"extra": "value"}
        
        with_span = _WithSpan(
            span=mock_span,
            context_attributes=context_attrs,
            extra_attributes=extra_attrs
        )
        
        assert with_span._span == mock_span
        assert hasattr(with_span, '_context_attributes') or hasattr(with_span, 'context_attributes')
        assert hasattr(with_span, '_extra_attributes') or hasattr(with_span, 'extra_attributes')


@pytest.mark.parametrize("hide_content", [False, True])
def test_real_sdk_stream_output_and_privacy(hide_content):
    """Use real SDK chunk models; export text once and retain token metadata."""
    from fi_instrumentation import FITracer, TraceConfig
    from groq.types.chat import ChatCompletionChunk
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    common = dict(id="local-stream", created=1, model="llama-3.1-70b-versatile",
                  object="chat.completion.chunk")
    chunks = [
        ChatCompletionChunk(**common, choices=[{"index": 0, "delta": {"content": "private-"}}],
                            x_groq={"id": "local-stream", "usage": None, "error": None}),
        ChatCompletionChunk(**common, choices=[{"index": 0, "delta": {"content": "answer"}}],
                            x_groq={"id": "local-stream", "error": None, "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}),
    ]
    attributes = list(_as_streaming_output(chunks))
    assert len(attributes) == len(dict(attributes))
    assert dict(attributes)["output.value"] == "private-answer"
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = FITracer(provider.get_tracer(__name__), config=TraceConfig(hide_outputs=hide_content))
    try:
        with tracer.start_as_current_span("groq-stream") as span:
            for key, value in attributes:
                span.set_attribute(key, value)
        finished, = exporter.get_finished_spans()
        attrs = dict(finished.attributes)
        assert attrs["gen_ai.usage.input_tokens"] == 2
        assert attrs["gen_ai.usage.output_tokens"] == 3
        assert attrs["gen_ai.usage.total_tokens"] == 5
        assert attrs["gen_ai.response.model"] == common["model"]
        assert "raw.output" not in attrs
        if hide_content:
            assert "private-answer" not in json.dumps(attrs)
        else:
            assert attrs["output.value"] == "private-answer"
    finally:
        provider.shutdown()


@pytest.fixture
def installed_groq():
    from fi_instrumentation import TraceConfig
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = GroqInstrumentor()

    def install(**privacy):
        instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**privacy))
        return exporter

    yield install
    instrumentor.uninstrument()
    provider.shutdown()


def _sdk_chunk(delta=None, *, choices=None, model="resolved-model", usage=None, top_usage=False):
    chunk = {
        "id": "local-stream", "created": 1, "model": model,
        "object": "chat.completion.chunk",
        "choices": choices if choices is not None else [{"index": 0, "delta": delta}],
        "x_groq": {"id": "local-stream", "error": None, "usage": None if top_usage else usage},
    }
    if top_usage:
        chunk["usage"] = usage
    return chunk


def _local_groq_client(asynchronous, responses):
    """Real SDK + real SSE parser; only the HTTP transport is replaced."""
    import httpx
    from groq import Groq, AsyncGroq

    consumed = []
    requests = []
    responses = iter(responses)

    class Body(httpx.SyncByteStream, httpx.AsyncByteStream):
        def __init__(self, payloads):
            self.payloads = payloads

        def __iter__(self):
            for payload in self.payloads:
                consumed.append(payload)
                yield ("data: " + json.dumps(payload) + "\n\n").encode()
            yield b"data: [DONE]\n\n"

        async def __aiter__(self):
            for part in self:
                yield part

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=Body(next(responses)))

    client_type = AsyncGroq if asynchronous else Groq
    http_type = httpx.AsyncClient if asynchronous else httpx.Client
    client = client_type(api_key="offline-placeholder", max_retries=0,
                         http_client=http_type(transport=httpx.MockTransport(handle)))
    return client, consumed, requests


async def _create_sdk_stream(client, asynchronous):
    stream = client.chat.completions.create(
        model="request-model", messages=[{"role": "user", "content": "private-question"}], stream=True,
    )
    return await stream if asynchronous else stream


async def _drain_sdk_stream(stream, asynchronous):
    return [chunk async for chunk in stream] if asynchronous else list(stream)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("top_usage", [False, True])
async def test_installed_streams_isolate_consecutive_and_interleaved_requests(installed_groq, asynchronous, top_usage):
    usage = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
    texts = ["first-response", "second-response", "third-response"]
    payloads = [[
        _sdk_chunk({"role": "assistant", "content": None}),
        _sdk_chunk({"content": text}),
        _sdk_chunk(choices=[], model="", usage=usage, top_usage=top_usage),
    ] for text in texts]
    exporter = installed_groq()
    client, consumed, requests = _local_groq_client(asynchronous, payloads)
    try:
        first = await _create_sdk_stream(client, asynchronous)
        assert consumed == []
        assert exporter.get_finished_spans() == ()
        first_chunks = await _drain_sdk_stream(first, asynchronous)
        assert [chunk.to_dict() for chunk in first_chunks] == payloads[0]
        assert len(exporter.get_finished_spans()) == 1
        second = await _create_sdk_stream(client, asynchronous)
        third = await _create_sdk_stream(client, asynchronous)
        assert consumed == payloads[0]  # Creating a stream does not drain it.
        if asynchronous:
            head_second, head_third = await second.__anext__(), await third.__anext__()
        else:
            head_second, head_third = next(second), next(third)
        third_chunks = [head_third] + await _drain_sdk_stream(third, asynchronous)
        second_chunks = [head_second] + await _drain_sdk_stream(second, asynchronous)
        assert [chunk.to_dict() for chunk in second_chunks] == payloads[1]
        assert [chunk.to_dict() for chunk in third_chunks] == payloads[2]
        spans = exporter.get_finished_spans()
        assert len(spans) == 3
        assert [span.attributes["output.value"] for span in spans] == [texts[0], texts[2], texts[1]]
        for span, text in zip(spans, (texts[0], texts[2], texts[1])):
            assert span.status.status_code == StatusCode.OK
            attrs = dict(span.attributes)
            assert attrs["gen_ai.span.kind"] == "LLM"
            assert attrs["gen_ai.request.model"] == "request-model"
            assert attrs["gen_ai.response.model"] == "resolved-model"
            assert attrs["gen_ai.usage.input_tokens"] == 2
            assert attrs["gen_ai.usage.output_tokens"] == 3
            assert attrs["gen_ai.usage.total_tokens"] == 5
            assert attrs["gen_ai.output.messages.0.message.content"] == text
            assert attrs["gen_ai.output.messages.0.message.role"] == "assistant"
            for other in set(texts) - {text}:
                assert other not in json.dumps(attrs)
        assert len(requests) == 3
    finally:
        if asynchronous:
            await client.close()
        else:
            client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("hide_content", [False, True])
async def test_installed_stream_tool_deltas_and_privacy(installed_groq, asynchronous, mixed, hide_content):
    usage = {"prompt_tokens": 0, "completion_tokens": 3, "total_tokens": 3}
    payloads = [
        _sdk_chunk({"role": "assistant", "content": "private-answer" if mixed else None,
                    "tool_calls": [{"index": 0, "id": "call_private_1", "type": "function",
                                    "function": {"name": "private_look", "arguments": '{"q":'}}]}),
        _sdk_chunk({"content": None, "tool_calls": [
            {"index": 0, "function": {"name": "up", "arguments": '"private-needle"}'}},
            {"index": 1, "id": "call_private_2", "type": "function",
             "function": {"name": "private_other", "arguments": '{"n":'}},
        ]}),
        _sdk_chunk(choices=[
            {"index": 0, "delta": {"tool_calls": [{"index": 1, "function": {"arguments": '"two"}'}}]}},
            {"index": 1, "delta": {"role": "assistant", "content": "private-alternative"}},
        ]),
        _sdk_chunk(choices=[], usage=usage),
    ]
    exporter = installed_groq(hide_inputs=hide_content, hide_outputs=hide_content)
    client, consumed, requests = _local_groq_client(asynchronous, [payloads])
    try:
        stream = await _create_sdk_stream(client, asynchronous)
        assert consumed == []
        chunks = await _drain_sdk_stream(stream, asynchronous)
        assert [chunk.to_dict() for chunk in chunks] == payloads
        span, = exporter.get_finished_spans()
        assert span.status.status_code == StatusCode.OK
        attrs = dict(span.attributes)
        assert attrs["gen_ai.usage.input_tokens"] == 0
        assert attrs["gen_ai.usage.output_tokens"] == 3
        assert attrs["gen_ai.usage.total_tokens"] == 3
        assert attrs["gen_ai.response.model"] == "resolved-model"
        assert not any(key.startswith("raw.") for key in attrs)
        if hide_content:
            assert "private" not in json.dumps(attrs)
            assert not any(key.startswith("gen_ai.output.messages") for key in attrs)
            assert attrs["output.value"] == "__REDACTED__"
        else:
            prefix = "gen_ai.output.messages.0.message.tool_calls."
            assert attrs[prefix + "0.tool_call.id"] == "call_private_1"
            assert attrs[prefix + "0.tool_call.function.name"] == "private_lookup"
            assert json.loads(attrs[prefix + "0.tool_call.function.arguments"]) == {"q": "private-needle"}
            assert attrs[prefix + "1.tool_call.id"] == "call_private_2"
            assert attrs[prefix + "1.tool_call.function.name"] == "private_other"
            assert json.loads(attrs[prefix + "1.tool_call.function.arguments"]) == {"n": "two"}
            assert attrs["output.value"] == ("private-answer" if mixed else "")
            assert attrs["gen_ai.output.messages.1.message.content"] == "private-alternative"
    finally:
        if asynchronous:
            await client.close()
        else:
            client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("extractor_method", ["get_attributes", "get_extra_attributes"])
async def test_installed_stream_finishes_when_attribute_extraction_fails(
    installed_groq, monkeypatch, asynchronous, extractor_method, caplog,
):
    def fail_after_attribute(self, **kwargs):
        yield "test.partial", "retained"
        raise ValueError("synthetic extraction failure")

    monkeypatch.setattr(_ResponseAttributesExtractor, extractor_method, fail_after_attribute)
    exporter = installed_groq()
    payloads = [_sdk_chunk({"content": "unchanged"})]
    client, _, _ = _local_groq_client(asynchronous, [payloads])
    try:
        stream = await _create_sdk_stream(client, asynchronous)
        chunks = await _drain_sdk_stream(stream, asynchronous)
        assert [chunk.to_dict() for chunk in chunks] == payloads
        span, = exporter.get_finished_spans()
        assert span.status.status_code == StatusCode.OK
        assert span.attributes["test.partial"] == "retained"
        assert "UnboundLocalError" not in caplog.text
    finally:
        if asynchronous:
            await client.close()
        else:
            client.close()


if __name__ == "__main__":
    pytest.main([__file__])
