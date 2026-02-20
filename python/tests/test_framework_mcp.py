import asyncio
import json
from typing import Any, AsyncGenerator, Dict, List, Optional
from unittest.mock import MagicMock, Mock, patch, AsyncMock, call
from uuid import uuid4

import pytest
from opentelemetry import context as otel_context
from opentelemetry import propagate
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, ConsoleSpanExporter

from traceai_mcp import (
    MCPInstrumentor,
    InstrumentedStreamReader,
    InstrumentedStreamWriter,
    ContextSavingStreamWriter,
    ContextAttachingStreamReader,
    ItemWithContext,
)


@pytest.fixture
def tracer_provider():
    """Create a test TracerProvider with console export for testing."""
    provider = TracerProvider()
    exporter = ConsoleSpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider


@pytest.fixture
def mock_mcp_modules():
    """Mock MCP modules and classes."""
    mock_modules = {}
    
    # Mock SessionMessage and JSONRPCRequest
    mock_session_message = MagicMock()
    mock_jsonrpc_request = MagicMock()
    mock_jsonrpc_request.params = {"test": "value"}
    
    mock_modules["session_message"] = mock_session_message
    mock_modules["jsonrpc_request"] = mock_jsonrpc_request
    
    return mock_modules


@pytest.fixture
def mock_stream():
    """Create a mock stream for testing."""
    mock_stream = AsyncMock()
    mock_stream.__aenter__ = AsyncMock(return_value=mock_stream)
    mock_stream.__aexit__ = AsyncMock(return_value=None)
    mock_stream.__aiter__ = AsyncMock()
    mock_stream.send = AsyncMock()
    return mock_stream


class TestMCPInstrumentor:
    """⚡ Test MCPInstrumentor lifecycle and basic functionality."""

    def test_instrumentation_dependencies(self):
        """Test instrumentor dependencies are properly declared."""
        instrumentor = MCPInstrumentor()
        dependencies = instrumentor.instrumentation_dependencies()
        
        assert isinstance(dependencies, tuple)
        assert "mcp >= 0.1.0" in dependencies
        assert len(dependencies) >= 1

    def test_instrument_registers_post_import_hooks(self):
        """Test that instrumentation registers all necessary post-import hooks."""
        instrumentor = MCPInstrumentor()
        
        with patch("traceai_mcp.register_post_import_hook") as mock_register:
            instrumentor._instrument()
            
            # Verify all expected post-import hooks are registered
            expected_modules = [
                "mcp.client.streamable_http",
                "mcp.server.streamable_http", 
                "mcp.client.sse",
                "mcp.server.sse",
                "mcp.client.stdio",
                "mcp.server.stdio",
                "mcp.server.session"
            ]
            
            assert mock_register.call_count == len(expected_modules)
            
            # Check that each expected module was registered
            called_modules = [call.args[1] for call in mock_register.call_args_list]
            for module in expected_modules:
                assert module in called_modules

    def test_uninstrument_unwraps_functions(self):
        """Test that uninstrumentation properly unwraps wrapped functions."""
        instrumentor = MCPInstrumentor()
        
        with patch("traceai_mcp.unwrap") as mock_unwrap:
            instrumentor._uninstrument()
            
            # Verify specific functions are unwrapped
            expected_calls = [
                call("mcp.client.stdio", "stdio_client"),
                call("mcp.server.stdio", "stdio_server"),
            ]
            
            mock_unwrap.assert_has_calls(expected_calls, any_order=True)

    def test_wrap_transport_with_callback(self):
        """Test _wrap_transport_with_callback async context manager."""
        instrumentor = MCPInstrumentor()
        
        async def test_wrapper():
            mock_read_stream = AsyncMock()
            mock_write_stream = AsyncMock()
            mock_callback = MagicMock()
            
            # Create a proper async context manager class
            class MockContextManager:
                async def __aenter__(self):
                    return (mock_read_stream, mock_write_stream, mock_callback)
                
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    return None
            
            # Create a mock wrapped function that returns the context manager
            def mock_wrapped(*args, **kwargs):
                return MockContextManager()
            
            async with instrumentor._wrap_transport_with_callback(
                mock_wrapped, None, (), {}
            ) as (reader, writer, callback):
                assert isinstance(reader, InstrumentedStreamReader)
                assert isinstance(writer, InstrumentedStreamWriter)
                assert callback == mock_callback
                
                # Verify the streams are properly wrapped with instrumented versions
                assert hasattr(reader, '_ObjectProxy__wrapped')
                assert hasattr(writer, '_ObjectProxy__wrapped')
        
        # Run the async test
        asyncio.run(test_wrapper())

    def test_wrap_plain_transport(self):
        """Test _wrap_plain_transport async context manager."""
        instrumentor = MCPInstrumentor()
        
        async def test_wrapper():
            mock_read_stream = AsyncMock()
            mock_write_stream = AsyncMock()
            
            # Create a proper async context manager class
            class MockContextManager:
                async def __aenter__(self):
                    return (mock_read_stream, mock_write_stream)
                
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    return None
            
            # Create a mock wrapped function that returns the context manager
            def mock_wrapped(*args, **kwargs):
                return MockContextManager()
            
            async with instrumentor._wrap_plain_transport(
                mock_wrapped, None, (), {}
            ) as (reader, writer):
                assert isinstance(reader, InstrumentedStreamReader)
                assert isinstance(writer, InstrumentedStreamWriter)
                
                # Verify the streams are properly wrapped with instrumented versions
                assert hasattr(reader, '_ObjectProxy__wrapped')
                assert hasattr(writer, '_ObjectProxy__wrapped')
        
        # Run the async test
        asyncio.run(test_wrapper())

    def test_base_session_init_wrapper(self):
        """Test _base_session_init_wrapper modifies session streams."""
        instrumentor = MCPInstrumentor()
        
        # Mock the session instance
        mock_instance = MagicMock()
        mock_reader = MagicMock()
        mock_writer = MagicMock()
        mock_instance._incoming_message_stream_reader = mock_reader
        mock_instance._incoming_message_stream_writer = mock_writer
        
        # Mock the wrapped function
        mock_wrapped = MagicMock()
        
        # Call the wrapper
        instrumentor._base_session_init_wrapper(mock_wrapped, mock_instance, (), {})
        
        # Verify the wrapped function was called
        mock_wrapped.assert_called_once_with()
        
        # Verify the streams were replaced with context-aware versions
        assert isinstance(
            mock_instance._incoming_message_stream_reader,
            ContextAttachingStreamReader
        )
        assert isinstance(
            mock_instance._incoming_message_stream_writer,
            ContextSavingStreamWriter
        )

    def test_base_session_init_wrapper_missing_streams(self):
        """Test _base_session_init_wrapper when streams are missing."""
        instrumentor = MCPInstrumentor()
        
        # Mock the session instance without streams
        mock_instance = MagicMock()
        mock_instance._incoming_message_stream_reader = None
        mock_instance._incoming_message_stream_writer = None
        
        # Mock the wrapped function
        mock_wrapped = MagicMock()
        
        # Call the wrapper - should not raise errors
        instrumentor._base_session_init_wrapper(mock_wrapped, mock_instance, (), {})
        
        # Verify the wrapped function was called
        mock_wrapped.assert_called_once_with()
        
        # Verify streams remain None
        assert mock_instance._incoming_message_stream_reader is None
        assert mock_instance._incoming_message_stream_writer is None


class TestInstrumentedStreamReader:
    """📖 Test InstrumentedStreamReader functionality."""

    def test_context_manager_support(self, mock_stream):
        """Test async context manager support."""
        reader = InstrumentedStreamReader(mock_stream)
        
        async def test_context():
            async with reader as stream:
                assert stream == mock_stream
            
            mock_stream.__aenter__.assert_called_once()
            mock_stream.__aexit__.assert_called_once()
        
        asyncio.run(test_context())

    def test_async_iteration_without_jsonrpc(self, mock_stream):
        """Test async iteration with non-JSONRPCRequest messages."""
        reader = InstrumentedStreamReader(mock_stream)

        # Create a mock JSONRPCRequest class that isinstance() checks will fail against
        mock_jsonrpc_request_cls = type("JSONRPCRequest", (), {})

        # Mock message whose root is NOT a JSONRPCRequest instance
        mock_message = MagicMock()
        mock_message.message.root = MagicMock(spec=[])

        # Create an async iterator manually
        async def async_iter():
            for item in [mock_message]:
                yield item

        # Mock the __aiter__ method directly
        mock_stream.__aiter__ = lambda self: async_iter()

        # Patch the mcp imports used inside __aiter__
        mock_session_message = MagicMock()
        mock_mcp_shared = MagicMock()
        mock_mcp_shared.message.SessionMessage = mock_session_message
        mock_mcp_types = MagicMock()
        mock_mcp_types.JSONRPCRequest = mock_jsonrpc_request_cls

        import sys

        async def test_iteration():
            with patch.dict(sys.modules, {
                "mcp": MagicMock(),
                "mcp.shared": mock_mcp_shared,
                "mcp.shared.message": mock_mcp_shared.message,
                "mcp.types": mock_mcp_types,
            }):
                items = []
                async for item in reader:
                    items.append(item)
                return items

        items = asyncio.run(test_iteration())
        assert len(items) == 1
        assert items[0] == mock_message


class TestInstrumentedStreamWriter:
    """✍️ Test InstrumentedStreamWriter functionality."""

    def test_context_manager_support(self, mock_stream):
        """Test async context manager support."""
        writer = InstrumentedStreamWriter(mock_stream)
        
        async def test_context():
            async with writer as stream:
                assert stream == mock_stream
            
            mock_stream.__aenter__.assert_called_once()
            mock_stream.__aexit__.assert_called_once()
        
        asyncio.run(test_context())


class TestContextSavingStreamWriter:
    """💾 Test ContextSavingStreamWriter functionality."""

    def test_context_manager_support(self, mock_stream):
        """Test async context manager support."""
        writer = ContextSavingStreamWriter(mock_stream)
        
        async def test_context():
            async with writer as stream:
                assert stream == mock_stream
            
            mock_stream.__aenter__.assert_called_once()
            mock_stream.__aexit__.assert_called_once()
        
        asyncio.run(test_context())

    def test_send_saves_context(self, mock_stream):
        """Test that send saves current context with item."""
        writer = ContextSavingStreamWriter(mock_stream)
        
        mock_item = MagicMock()
        
        async def test_send():
            with patch("opentelemetry.context.get_current") as mock_get_context:
                mock_context = MagicMock()
                mock_get_context.return_value = mock_context
                
                await writer.send(mock_item)
                
                # Verify context was captured and sent with item
                mock_get_context.assert_called_once()
                
                # Check that send was called with ItemWithContext
                args, kwargs = mock_stream.send.call_args
                sent_item = args[0]
                assert isinstance(sent_item, ItemWithContext)
                assert sent_item.item == mock_item
                assert sent_item.ctx == mock_context
        
        asyncio.run(test_send())


class TestContextAttachingStreamReader:
    """🔗 Test ContextAttachingStreamReader functionality."""

    def test_context_manager_support(self, mock_stream):
        """Test async context manager support."""
        reader = ContextAttachingStreamReader(mock_stream)
        
        async def test_context():
            async with reader as stream:
                assert stream == mock_stream
            
            mock_stream.__aenter__.assert_called_once()
            mock_stream.__aexit__.assert_called_once()
        
        asyncio.run(test_context())


class TestItemWithContext:
    """📦 Test ItemWithContext dataclass."""

    def test_item_with_context_creation(self):
        """Test ItemWithContext can be created and holds data."""
        mock_item = MagicMock()
        mock_context = MagicMock()
        
        item_with_context = ItemWithContext(item=mock_item, ctx=mock_context)
        
        assert item_with_context.item == mock_item
        assert item_with_context.ctx == mock_context

    def test_item_with_context_immutable(self):
        """Test ItemWithContext is frozen (immutable)."""
        mock_item = MagicMock()
        mock_context = MagicMock()
        
        item_with_context = ItemWithContext(item=mock_item, ctx=mock_context)
        
        # Should not be able to modify frozen dataclass
        with pytest.raises(AttributeError):
            item_with_context.item = MagicMock()
        
        with pytest.raises(AttributeError):
            item_with_context.ctx = MagicMock()


class TestErrorHandling:
    """💥 Test error handling scenarios."""

    def test_instrumentation_with_missing_dependencies(self):
        """Test instrumentation behavior with missing dependencies."""
        instrumentor = MCPInstrumentor()
        
        # Should not raise errors even if some components are missing
        try:
            deps = instrumentor.instrumentation_dependencies()
            assert isinstance(deps, tuple)
        except ImportError:
            # Expected if MCP is not available
            pass


class TestIntegrationScenarios:
    """🎭 Test integration scenarios and real-world usage patterns."""

    def test_full_instrumentation_lifecycle(self):
        """Test complete MCP instrumentation lifecycle."""
        instrumentor = MCPInstrumentor()
        
        with patch("traceai_mcp.register_post_import_hook") as mock_register:
            with patch("traceai_mcp.unwrap") as mock_unwrap:
                # Test instrumentation
                instrumentor._instrument()
                assert mock_register.call_count >= 7  # All transport hooks
                
                # Test uninstrumentation
                instrumentor._uninstrument()
                assert mock_unwrap.call_count >= 2  # Stdio client/server unwrapped

    def test_multiple_transport_support(self):
        """Test that multiple transport types are supported."""
        instrumentor = MCPInstrumentor()
        
        # Test that all expected transports have wrappers
        transport_wrappers = {
            "streamable_http": instrumentor._wrap_transport_with_callback,
            "sse": instrumentor._wrap_plain_transport,
            "stdio": instrumentor._wrap_plain_transport,
        }
        
        for transport_name, wrapper in transport_wrappers.items():
            assert callable(wrapper)
            assert hasattr(wrapper, "__call__")


if __name__ == "__main__":
    pytest.main([__file__, "-v"]) 