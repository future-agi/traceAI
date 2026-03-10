"""Tests for LangGraph superstep tracker module."""

import pytest
from unittest.mock import MagicMock, patch


class TestSuperstepInfo:
    """Test SuperstepInfo class."""

    def test_import(self):
        """Test that SuperstepInfo can be imported."""
        from traceai_langchain._langgraph._superstep_tracker import SuperstepInfo
        assert SuperstepInfo is not None

    def test_initialization(self):
        """Test SuperstepInfo initialization."""
        from traceai_langchain._langgraph._superstep_tracker import SuperstepInfo

        info = SuperstepInfo(1)
        assert info.superstep_number == 1
        assert info.nodes_executed == []
        assert info.start_time is None
        assert info.end_time is None
        assert info.state_before is None
        assert info.state_after is None
        assert info.errors == []

    def test_duration_ms_no_times(self):
        """Test duration_ms when times not set."""
        from traceai_langchain._langgraph._superstep_tracker import SuperstepInfo

        info = SuperstepInfo(1)
        assert info.duration_ms is None

    def test_duration_ms_with_times(self):
        """Test duration_ms calculation."""
        from traceai_langchain._langgraph._superstep_tracker import SuperstepInfo

        info = SuperstepInfo(1)
        info.start_time = 1.0
        info.end_time = 1.5
        assert info.duration_ms == 500.0

    def test_to_dict(self):
        """Test to_dict conversion."""
        from traceai_langchain._langgraph._superstep_tracker import SuperstepInfo

        info = SuperstepInfo(2)
        info.nodes_executed = ["node1", "node2"]
        info.start_time = 1.0
        info.end_time = 1.1
        info.errors.append({"error": "test"})

        result = info.to_dict()

        assert result["superstep_number"] == 2
        assert result["nodes_executed"] == ["node1", "node2"]
        assert abs(result["duration_ms"] - 100.0) < 0.001  # floating-point comparison
        assert result["error_count"] == 1


class TestSuperstepTracker:
    """Test SuperstepTracker class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock()
        self.mock_span = MagicMock()
        self.mock_span.is_recording.return_value = True
        self.mock_tracer.start_span.return_value = self.mock_span

    def test_initialization(self):
        """Test SuperstepTracker initialization."""
        from traceai_langchain._langgraph._superstep_tracker import SuperstepTracker

        tracker = SuperstepTracker(self.mock_tracer)
        assert tracker._tracer == self.mock_tracer
        assert tracker._superstep_count == 0
        assert tracker._current_superstep is None
        assert tracker._superstep_history == []

    def test_start_superstep(self):
        """Test starting a superstep."""
        from traceai_langchain._langgraph._superstep_tracker import SuperstepTracker

        tracker = SuperstepTracker(self.mock_tracer)
        superstep = tracker.start_superstep({"input": "test"})

        assert superstep.superstep_number == 1
        assert superstep.start_time is not None
        assert superstep.state_before == {"input": "test"}
        assert tracker._superstep_count == 1
        assert tracker._current_superstep == superstep

    def test_start_multiple_supersteps(self):
        """Test starting multiple supersteps increments count."""
        from traceai_langchain._langgraph._superstep_tracker import SuperstepTracker

        tracker = SuperstepTracker(self.mock_tracer)

        s1 = tracker.start_superstep()
        tracker.end_superstep()

        s2 = tracker.start_superstep()
        tracker.end_superstep()

        s3 = tracker.start_superstep()

        assert s1.superstep_number == 1
        assert s2.superstep_number == 2
        assert s3.superstep_number == 3
        assert tracker._superstep_count == 3

    def test_record_node_execution(self):
        """Test recording node execution."""
        from traceai_langchain._langgraph._superstep_tracker import SuperstepTracker

        tracker = SuperstepTracker(self.mock_tracer)
        tracker.start_superstep()

        tracker.record_node_execution("node_a")
        tracker.record_node_execution("node_b")

        assert tracker._current_superstep.nodes_executed == ["node_a", "node_b"]

    def test_record_node_execution_no_superstep(self):
        """Test recording node execution without active superstep."""
        from traceai_langchain._langgraph._superstep_tracker import SuperstepTracker

        tracker = SuperstepTracker(self.mock_tracer)
        # Should not raise an error
        tracker.record_node_execution("node_a")

    def test_record_error(self):
        """Test recording an error."""
        from traceai_langchain._langgraph._superstep_tracker import SuperstepTracker

        tracker = SuperstepTracker(self.mock_tracer)
        tracker.start_superstep()

        error = ValueError("test error")
        tracker.record_error("failing_node", error)

        assert len(tracker._current_superstep.errors) == 1
        assert tracker._current_superstep.errors[0]["node"] == "failing_node"
        assert tracker._current_superstep.errors[0]["error_type"] == "ValueError"

    def test_end_superstep(self):
        """Test ending a superstep."""
        from traceai_langchain._langgraph._superstep_tracker import SuperstepTracker

        tracker = SuperstepTracker(self.mock_tracer)
        tracker.start_superstep({"before": True})
        tracker.record_node_execution("node_a")

        result = tracker.end_superstep({"after": True})

        assert result.state_after == {"after": True}
        assert result.end_time is not None
        assert tracker._current_superstep is None
        assert len(tracker._superstep_history) == 1

    def test_end_superstep_no_active(self):
        """Test ending superstep when none active."""
        from traceai_langchain._langgraph._superstep_tracker import SuperstepTracker

        tracker = SuperstepTracker(self.mock_tracer)
        result = tracker.end_superstep()
        assert result is None

    def test_get_current_superstep(self):
        """Test getting current superstep."""
        from traceai_langchain._langgraph._superstep_tracker import SuperstepTracker

        tracker = SuperstepTracker(self.mock_tracer)
        assert tracker.get_current_superstep() is None

        tracker.start_superstep()
        assert tracker.get_current_superstep() is not None

        tracker.end_superstep()
        assert tracker.get_current_superstep() is None

    def test_get_superstep_count(self):
        """Test getting superstep count."""
        from traceai_langchain._langgraph._superstep_tracker import SuperstepTracker

        tracker = SuperstepTracker(self.mock_tracer)
        assert tracker.get_superstep_count() == 0

        tracker.start_superstep()
        assert tracker.get_superstep_count() == 1

        tracker.end_superstep()
        tracker.start_superstep()
        assert tracker.get_superstep_count() == 2

    def test_get_history(self):
        """Test getting superstep history."""
        from traceai_langchain._langgraph._superstep_tracker import SuperstepTracker

        tracker = SuperstepTracker(self.mock_tracer)

        tracker.start_superstep()
        tracker.record_node_execution("a")
        tracker.end_superstep()

        tracker.start_superstep()
        tracker.record_node_execution("b")
        tracker.end_superstep()

        history = tracker.get_history()

        assert len(history) == 2
        assert history[0]["superstep_number"] == 1
        assert history[1]["superstep_number"] == 2

    def test_get_stats(self):
        """Test getting statistics."""
        from traceai_langchain._langgraph._superstep_tracker import SuperstepTracker

        tracker = SuperstepTracker(self.mock_tracer)

        tracker.start_superstep()
        tracker.record_node_execution("a")
        tracker.record_node_execution("b")
        tracker.end_superstep()

        tracker.start_superstep()
        tracker.record_node_execution("c")
        tracker.end_superstep()

        stats = tracker.get_stats()

        assert stats["total_supersteps"] == 2
        assert stats["total_nodes_executed"] == 3
        assert stats["avg_nodes_per_superstep"] == 1.5

    def test_get_stats_no_data(self):
        """Test getting stats with no data."""
        from traceai_langchain._langgraph._superstep_tracker import SuperstepTracker

        tracker = SuperstepTracker(self.mock_tracer)
        stats = tracker.get_stats()
        assert "error" in stats

    def test_reset(self):
        """Test resetting tracker."""
        from traceai_langchain._langgraph._superstep_tracker import SuperstepTracker

        tracker = SuperstepTracker(self.mock_tracer)

        tracker.start_superstep()
        tracker.record_node_execution("a")
        tracker.end_superstep()

        assert tracker.get_superstep_count() == 1

        tracker.reset()

        assert tracker.get_superstep_count() == 0
        assert tracker.get_history() == []


class TestReducerTracker:
    """Test ReducerTracker class."""

    def test_initialization(self):
        """Test ReducerTracker initialization."""
        from traceai_langchain._langgraph._superstep_tracker import ReducerTracker

        tracker = ReducerTracker()
        assert tracker._reducer_map == {}
        assert tracker._attribution_history == []

    def test_register_reducer(self):
        """Test registering a reducer."""
        from traceai_langchain._langgraph._superstep_tracker import ReducerTracker

        tracker = ReducerTracker()
        tracker.register_reducer("messages", "add_messages")

        assert tracker._reducer_map["messages"] == "add_messages"

    def test_attribute_change(self):
        """Test attributing a change to a reducer."""
        from traceai_langchain._langgraph._superstep_tracker import ReducerTracker

        tracker = ReducerTracker()
        tracker.register_reducer("messages", "add_messages")

        result = tracker.attribute_change(
            field_name="messages",
            old_value=[],
            new_value=["hello"],
            node_name="chat_node",
        )

        assert result == "add_messages"
        assert len(tracker._attribution_history) == 1
        assert tracker._attribution_history[0]["field"] == "messages"
        assert tracker._attribution_history[0]["reducer"] == "add_messages"

    def test_attribute_change_with_span(self):
        """Test attributing a change with span recording."""
        from traceai_langchain._langgraph._superstep_tracker import ReducerTracker

        tracker = ReducerTracker()
        tracker.register_reducer("count", "increment_count")

        mock_span = MagicMock()
        mock_span.is_recording.return_value = True

        result = tracker.attribute_change(
            field_name="count",
            old_value=0,
            new_value=1,
            node_name="counter",
            span=mock_span,
        )

        assert result == "increment_count"
        mock_span.set_attribute.assert_called()
        mock_span.add_event.assert_called()

    def test_attribute_change_unknown_reducer(self):
        """Test attributing a change for unknown reducer."""
        from traceai_langchain._langgraph._superstep_tracker import ReducerTracker

        tracker = ReducerTracker()

        result = tracker.attribute_change(
            field_name="unknown_field",
            old_value="a",
            new_value="b",
            node_name="some_node",
        )

        assert result is None
        assert len(tracker._attribution_history) == 1
        assert tracker._attribution_history[0]["reducer"] is None

    def test_get_field_reducer(self):
        """Test getting reducer for a field."""
        from traceai_langchain._langgraph._superstep_tracker import ReducerTracker

        tracker = ReducerTracker()
        tracker.register_reducer("items", "append_items")

        assert tracker.get_field_reducer("items") == "append_items"
        assert tracker.get_field_reducer("unknown") is None

    def test_get_attribution_history(self):
        """Test getting attribution history."""
        from traceai_langchain._langgraph._superstep_tracker import ReducerTracker

        tracker = ReducerTracker()

        tracker.attribute_change("a", 1, 2, "node1")
        tracker.attribute_change("b", "x", "y", "node2")

        history = tracker.get_attribution_history()

        assert len(history) == 2
        assert history[0]["field"] == "a"
        assert history[1]["field"] == "b"

    def test_reset(self):
        """Test resetting tracker (keeps reducer map)."""
        from traceai_langchain._langgraph._superstep_tracker import ReducerTracker

        tracker = ReducerTracker()
        tracker.register_reducer("messages", "add_messages")
        tracker.attribute_change("messages", [], ["a"], "node1")

        assert len(tracker.get_attribution_history()) == 1

        tracker.reset()

        assert len(tracker.get_attribution_history()) == 0
        # Reducer map should be preserved
        assert tracker.get_field_reducer("messages") == "add_messages"
