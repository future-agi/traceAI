"""Tests for LangGraph node wrapper module."""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
import asyncio

from traceai_langchain._langgraph._node_wrapper import NodeWrapper, AsyncNodeWrapper
from traceai_langchain._langgraph._state_tracker import StateTransitionTracker


class TestNodeWrapper:
    """Test NodeWrapper class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock()
        self.mock_span = MagicMock()
        self.mock_span.is_recording.return_value = True
        self.mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=self.mock_span
        )
        self.mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=False
        )
        self.state_tracker = StateTransitionTracker()

    def test_initialization(self):
        """Test NodeWrapper initialization."""
        wrapper = NodeWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )
        assert wrapper._tracer == self.mock_tracer
        assert wrapper._state_tracker == self.state_tracker
        assert wrapper._graph_name is None
        assert wrapper._node_execution_counts == {}

    def test_initialization_with_graph_name(self):
        """Test NodeWrapper initialization with graph name."""
        wrapper = NodeWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
            graph_name="test_graph",
        )
        assert wrapper._graph_name == "test_graph"

    def test_wrap_node_creates_wrapper(self):
        """Test that wrap_node creates a wrapped function."""
        wrapper = NodeWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )

        def original_func(state):
            return {"result": "done"}

        wrapped = wrapper.wrap_node(
            node_name="test_node",
            node_func=original_func,
        )

        assert callable(wrapped)
        assert wrapped._langgraph_wrapped is True
        assert wrapped._original_func == original_func

    def test_wrap_node_preserves_name(self):
        """Test that wrap_node preserves function name."""
        wrapper = NodeWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )

        def my_custom_node(state):
            return {"result": "done"}

        wrapped = wrapper.wrap_node(
            node_name="my_custom_node",
            node_func=my_custom_node,
        )

        assert wrapped.__name__ == "my_custom_node"

    def test_wrap_node_executes_function(self):
        """Test that wrapped node executes the original function."""
        wrapper = NodeWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )

        def original_func(state):
            return {"count": state.get("count", 0) + 1}

        wrapped = wrapper.wrap_node(
            node_name="counter",
            node_func=original_func,
        )

        result = wrapped({"count": 5})
        assert result == {"count": 6}

    def test_wrap_node_tracks_execution_count(self):
        """Test that wrapped node tracks execution count."""
        wrapper = NodeWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )

        def original_func(state):
            return {"done": True}

        wrapped = wrapper.wrap_node(
            node_name="repeated_node",
            node_func=original_func,
        )

        wrapped({})
        wrapped({})
        wrapped({})

        counts = wrapper.get_execution_counts()
        assert counts["repeated_node"] == 3

    def test_wrap_node_creates_span(self):
        """Test that wrapped node creates a span."""
        wrapper = NodeWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )

        def original_func(state):
            return {"done": True}

        wrapped = wrapper.wrap_node(
            node_name="span_test",
            node_func=original_func,
        )

        wrapped({})

        self.mock_tracer.start_as_current_span.assert_called()
        call_args = self.mock_tracer.start_as_current_span.call_args
        assert "langgraph.node.span_test" in call_args[0][0]

    def test_wrap_node_with_config(self):
        """Test that wrapped node passes config to function."""
        wrapper = NodeWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )

        received_config = {}

        def original_func(state, config):
            received_config.update(config or {})
            return {"done": True}

        wrapped = wrapper.wrap_node(
            node_name="config_test",
            node_func=original_func,
        )

        wrapped({"input": "data"}, {"thread_id": "123"})
        assert received_config == {"thread_id": "123"}

    def test_wrap_node_handles_exception(self):
        """Test that wrapped node handles exceptions."""
        wrapper = NodeWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )

        def failing_func(state):
            raise ValueError("Test error")

        wrapped = wrapper.wrap_node(
            node_name="failing_node",
            node_func=failing_func,
        )

        with pytest.raises(ValueError, match="Test error"):
            wrapped({})

    def test_wrap_node_entry_attribute(self):
        """Test that entry node is marked correctly."""
        wrapper = NodeWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )

        def entry_func(state):
            return {"started": True}

        wrapped = wrapper.wrap_node(
            node_name="entry_node",
            node_func=entry_func,
            is_entry=True,
        )

        wrapped({})

        # Verify span attributes were set
        self.mock_span.set_attribute.assert_called()

    def test_wrap_node_end_attribute(self):
        """Test that end node is marked correctly."""
        wrapper = NodeWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )

        def end_func(state):
            return {"completed": True}

        wrapped = wrapper.wrap_node(
            node_name="end_node",
            node_func=end_func,
            is_end=True,
        )

        wrapped({})

        self.mock_span.set_attribute.assert_called()

    def test_reset_counts(self):
        """Test resetting execution counts."""
        wrapper = NodeWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )

        def original_func(state):
            return {}

        wrapped = wrapper.wrap_node(
            node_name="reset_test",
            node_func=original_func,
        )

        wrapped({})
        wrapped({})
        assert wrapper.get_execution_counts()["reset_test"] == 2

        wrapper.reset_counts()
        assert wrapper.get_execution_counts() == {}


class TestAsyncNodeWrapper:
    """Test AsyncNodeWrapper class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock()
        self.mock_span = MagicMock()
        self.mock_span.is_recording.return_value = True
        self.mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=self.mock_span
        )
        self.mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=False
        )
        self.state_tracker = StateTransitionTracker()

    def test_initialization(self):
        """Test AsyncNodeWrapper initialization."""
        wrapper = AsyncNodeWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )
        assert wrapper._tracer == self.mock_tracer
        assert wrapper._state_tracker == self.state_tracker

    def test_wrap_node_creates_async_wrapper(self):
        """Test that wrap_node creates an async wrapped function."""
        wrapper = AsyncNodeWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )

        async def original_async_func(state):
            return {"result": "async_done"}

        wrapped = wrapper.wrap_node(
            node_name="async_test",
            node_func=original_async_func,
        )

        assert callable(wrapped)
        assert wrapped._langgraph_wrapped is True
        assert wrapped._original_func == original_async_func

    @pytest.mark.asyncio
    async def test_wrap_node_executes_async_function(self):
        """Test that wrapped async node executes correctly."""
        wrapper = AsyncNodeWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )

        async def original_async_func(state):
            await asyncio.sleep(0.01)  # Simulate async work
            return {"count": state.get("count", 0) + 1}

        wrapped = wrapper.wrap_node(
            node_name="async_counter",
            node_func=original_async_func,
        )

        result = await wrapped({"count": 5})
        assert result == {"count": 6}

    @pytest.mark.asyncio
    async def test_wrap_node_tracks_async_execution_count(self):
        """Test that wrapped async node tracks execution count."""
        wrapper = AsyncNodeWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )

        async def original_async_func(state):
            return {"done": True}

        wrapped = wrapper.wrap_node(
            node_name="async_repeated",
            node_func=original_async_func,
        )

        await wrapped({})
        await wrapped({})
        await wrapped({})

        counts = wrapper.get_execution_counts()
        assert counts["async_repeated"] == 3

    @pytest.mark.asyncio
    async def test_wrap_node_handles_async_exception(self):
        """Test that wrapped async node handles exceptions."""
        wrapper = AsyncNodeWrapper(
            tracer=self.mock_tracer,
            state_tracker=self.state_tracker,
        )

        async def failing_async_func(state):
            raise ValueError("Async test error")

        wrapped = wrapper.wrap_node(
            node_name="async_failing",
            node_func=failing_async_func,
        )

        with pytest.raises(ValueError, match="Async test error"):
            await wrapped({})
