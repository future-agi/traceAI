"""Tests for LangGraph checkpoint tracer module."""

import pytest
from unittest.mock import MagicMock, patch


class TestCheckpointOperation:
    """Test CheckpointOperation class."""

    def test_import(self):
        """Test that CheckpointOperation can be imported."""
        from traceai_langchain._langgraph._checkpoint_tracer import CheckpointOperation
        assert CheckpointOperation is not None

    def test_initialization_save(self):
        """Test CheckpointOperation initialization for save."""
        from traceai_langchain._langgraph._checkpoint_tracer import CheckpointOperation

        op = CheckpointOperation("save", thread_id="t1", checkpoint_id="c1")
        assert op.operation == "save"
        assert op.thread_id == "t1"
        assert op.checkpoint_id == "c1"
        assert op.timestamp is not None
        assert op.success is False

    def test_initialization_load(self):
        """Test CheckpointOperation initialization for load."""
        from traceai_langchain._langgraph._checkpoint_tracer import CheckpointOperation

        op = CheckpointOperation("load", thread_id="t2")
        assert op.operation == "load"
        assert op.thread_id == "t2"
        assert op.checkpoint_id is None

    def test_to_dict(self):
        """Test to_dict conversion."""
        from traceai_langchain._langgraph._checkpoint_tracer import CheckpointOperation

        op = CheckpointOperation("save", thread_id="t1")
        op.duration_ms = 50.5
        op.size_bytes = 1024
        op.success = True

        result = op.to_dict()

        assert result["operation"] == "save"
        assert result["thread_id"] == "t1"
        assert result["duration_ms"] == 50.5
        assert result["size_bytes"] == 1024
        assert result["success"] is True


class TestCheckpointTracer:
    """Test CheckpointTracer class."""

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

    def test_initialization(self):
        """Test CheckpointTracer initialization."""
        from traceai_langchain._langgraph._checkpoint_tracer import CheckpointTracer

        tracer = CheckpointTracer(self.mock_tracer)
        assert tracer._tracer == self.mock_tracer
        assert tracer._operation_history == []

    def test_wrap_checkpoint_saver(self):
        """Test wrapping a checkpoint saver."""
        from traceai_langchain._langgraph._checkpoint_tracer import CheckpointTracer

        tracer = CheckpointTracer(self.mock_tracer)

        mock_saver = MagicMock()
        mock_saver.put = MagicMock()
        mock_saver.get = MagicMock()
        mock_saver.list = MagicMock()

        wrapped = tracer.wrap_checkpoint_saver(mock_saver)

        # Original methods should be stored
        assert 'put' in tracer._original_methods
        assert 'get' in tracer._original_methods
        assert 'list' in tracer._original_methods

    def test_put_wrapper_success(self):
        """Test put wrapper on success."""
        from traceai_langchain._langgraph._checkpoint_tracer import CheckpointTracer

        tracer = CheckpointTracer(self.mock_tracer)

        mock_saver = MagicMock()
        mock_saver.put = MagicMock(return_value="checkpoint_123")

        tracer.wrap_checkpoint_saver(mock_saver)

        config = {"configurable": {"thread_id": "thread_1"}}
        checkpoint = {"state": "data"}

        result = mock_saver.put(config, checkpoint)

        assert result == "checkpoint_123"
        assert len(tracer._operation_history) == 1
        assert tracer._operation_history[0].operation == "save"
        assert tracer._operation_history[0].success is True

    def test_put_wrapper_error(self):
        """Test put wrapper on error."""
        from traceai_langchain._langgraph._checkpoint_tracer import CheckpointTracer

        tracer = CheckpointTracer(self.mock_tracer)

        mock_saver = MagicMock()

        def failing_put(config, checkpoint, metadata=None):
            raise ValueError("Save failed")

        mock_saver.put = failing_put

        tracer.wrap_checkpoint_saver(mock_saver)

        config = {"configurable": {"thread_id": "thread_1"}}

        with pytest.raises(ValueError, match="Save failed"):
            mock_saver.put(config, {"data": "test"})

        assert len(tracer._operation_history) == 1
        assert tracer._operation_history[0].success is False
        assert tracer._operation_history[0].error == "Save failed"

    def test_get_wrapper_found(self):
        """Test get wrapper when checkpoint found."""
        from traceai_langchain._langgraph._checkpoint_tracer import CheckpointTracer

        tracer = CheckpointTracer(self.mock_tracer)

        mock_saver = MagicMock()
        mock_saver.get = MagicMock(return_value={"state": "loaded_data"})

        tracer.wrap_checkpoint_saver(mock_saver)

        config = {"configurable": {"thread_id": "thread_1"}}
        result = mock_saver.get(config)

        assert result == {"state": "loaded_data"}
        assert len(tracer._operation_history) == 1
        assert tracer._operation_history[0].operation == "load"
        assert tracer._operation_history[0].success is True

    def test_get_wrapper_not_found(self):
        """Test get wrapper when checkpoint not found."""
        from traceai_langchain._langgraph._checkpoint_tracer import CheckpointTracer

        tracer = CheckpointTracer(self.mock_tracer)

        mock_saver = MagicMock()
        mock_saver.get = MagicMock(return_value=None)

        tracer.wrap_checkpoint_saver(mock_saver)

        config = {"configurable": {"thread_id": "thread_1"}}
        result = mock_saver.get(config)

        assert result is None
        # Still success, just no checkpoint found
        assert tracer._operation_history[0].success is True

    def test_list_wrapper(self):
        """Test list wrapper."""
        from traceai_langchain._langgraph._checkpoint_tracer import CheckpointTracer

        tracer = CheckpointTracer(self.mock_tracer)

        mock_saver = MagicMock()
        mock_saver.list = MagicMock(return_value=iter([
            {"id": "c1"},
            {"id": "c2"},
            {"id": "c3"},
        ]))

        tracer.wrap_checkpoint_saver(mock_saver)

        config = {"configurable": {"thread_id": "thread_1"}}
        results = list(mock_saver.list(config))

        assert len(results) == 3
        assert len(tracer._operation_history) == 1
        assert tracer._operation_history[0].operation == "list"

    def test_get_operation_history(self):
        """Test getting operation history."""
        from traceai_langchain._langgraph._checkpoint_tracer import CheckpointTracer

        tracer = CheckpointTracer(self.mock_tracer)

        mock_saver = MagicMock()
        mock_saver.put = MagicMock(return_value="c1")
        mock_saver.get = MagicMock(return_value={"state": "data"})

        tracer.wrap_checkpoint_saver(mock_saver)

        mock_saver.put({"configurable": {"thread_id": "t1"}}, {})
        mock_saver.get({"configurable": {"thread_id": "t1"}})

        history = tracer.get_operation_history()

        assert len(history) == 2
        assert history[0]["operation"] == "save"
        assert history[1]["operation"] == "load"

    def test_get_stats(self):
        """Test getting statistics."""
        from traceai_langchain._langgraph._checkpoint_tracer import CheckpointTracer

        tracer = CheckpointTracer(self.mock_tracer)

        mock_saver = MagicMock()
        mock_saver.put = MagicMock(return_value="c1")
        mock_saver.get = MagicMock(return_value={"state": "data"})

        tracer.wrap_checkpoint_saver(mock_saver)

        # Perform operations
        mock_saver.put({"configurable": {"thread_id": "t1"}}, {"data": "x"})
        mock_saver.put({"configurable": {"thread_id": "t2"}}, {"data": "y"})
        mock_saver.get({"configurable": {"thread_id": "t1"}})

        stats = tracer.get_stats()

        assert stats["total_operations"] == 3
        assert stats["save_count"] == 2
        assert stats["load_count"] == 1

    def test_get_stats_no_data(self):
        """Test getting stats with no data."""
        from traceai_langchain._langgraph._checkpoint_tracer import CheckpointTracer

        tracer = CheckpointTracer(self.mock_tracer)
        stats = tracer.get_stats()
        assert "error" in stats

    def test_reset(self):
        """Test resetting operation history."""
        from traceai_langchain._langgraph._checkpoint_tracer import CheckpointTracer

        tracer = CheckpointTracer(self.mock_tracer)

        mock_saver = MagicMock()
        mock_saver.put = MagicMock(return_value="c1")

        tracer.wrap_checkpoint_saver(mock_saver)
        mock_saver.put({"configurable": {"thread_id": "t1"}}, {})

        assert len(tracer.get_operation_history()) == 1

        tracer.reset()

        assert len(tracer.get_operation_history()) == 0


class TestCheckpointTracerAsync:
    """Test CheckpointTracer async methods."""

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

    def test_wrap_async_methods(self):
        """Test that async methods are wrapped."""
        from traceai_langchain._langgraph._checkpoint_tracer import CheckpointTracer

        tracer = CheckpointTracer(self.mock_tracer)

        mock_saver = MagicMock()
        mock_saver.aput = MagicMock()
        mock_saver.aget = MagicMock()

        tracer.wrap_checkpoint_saver(mock_saver)

        assert 'aput' in tracer._original_methods
        assert 'aget' in tracer._original_methods

    @pytest.mark.asyncio
    async def test_aput_wrapper(self):
        """Test async put wrapper."""
        from traceai_langchain._langgraph._checkpoint_tracer import CheckpointTracer

        tracer = CheckpointTracer(self.mock_tracer)

        mock_saver = MagicMock()

        async def mock_aput(config, checkpoint, metadata=None):
            return "async_checkpoint_123"

        mock_saver.aput = mock_aput

        tracer.wrap_checkpoint_saver(mock_saver)

        config = {"configurable": {"thread_id": "t1"}}
        result = await mock_saver.aput(config, {"data": "test"})

        assert result == "async_checkpoint_123"
        assert len(tracer._operation_history) == 1

    @pytest.mark.asyncio
    async def test_aget_wrapper(self):
        """Test async get wrapper."""
        from traceai_langchain._langgraph._checkpoint_tracer import CheckpointTracer

        tracer = CheckpointTracer(self.mock_tracer)

        mock_saver = MagicMock()

        async def mock_aget(config):
            return {"loaded": "async_data"}

        mock_saver.aget = mock_aget

        tracer.wrap_checkpoint_saver(mock_saver)

        config = {"configurable": {"thread_id": "t1"}}
        result = await mock_saver.aget(config)

        assert result == {"loaded": "async_data"}
        assert len(tracer._operation_history) == 1
