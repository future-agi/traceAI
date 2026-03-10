"""Tests for LangGraph state tracker module."""

import pytest
from unittest.mock import MagicMock, patch

from traceai_langchain._langgraph._state_tracker import (
    StateTransitionTracker,
    safe_json_dumps,
    get_object_size,
    deep_diff,
)


class TestSafeJsonDumps:
    """Test safe_json_dumps utility function."""

    def test_simple_dict(self):
        """Test serialization of simple dictionary."""
        data = {"key": "value", "number": 42}
        result = safe_json_dumps(data)
        assert '"key": "value"' in result
        assert '"number": 42' in result

    def test_nested_dict(self):
        """Test serialization of nested dictionary."""
        data = {"outer": {"inner": "value"}}
        result = safe_json_dumps(data)
        assert "inner" in result
        assert "value" in result

    def test_truncation(self):
        """Test that long strings are truncated."""
        data = {"key": "x" * 20000}
        result = safe_json_dumps(data, max_length=100)
        assert len(result) <= 100 + len("...[truncated]")
        assert "...[truncated]" in result

    def test_non_serializable_object(self):
        """Test handling of non-serializable objects."""
        class CustomObj:
            def __repr__(self):
                return "CustomObj()"

        data = {"obj": CustomObj()}
        result = safe_json_dumps(data)
        assert "CustomObj" in result

    def test_list_data(self):
        """Test serialization of list data."""
        data = [1, 2, 3, "test"]
        result = safe_json_dumps(data)
        assert "[1, 2, 3" in result
        assert "test" in result


class TestGetObjectSize:
    """Test get_object_size utility function."""

    def test_simple_dict_size(self):
        """Test size calculation for simple dictionary."""
        data = {"key": "value"}
        size = get_object_size(data)
        assert size > 0

    def test_empty_dict_size(self):
        """Test size calculation for empty dictionary."""
        data = {}
        size = get_object_size(data)
        assert size > 0

    def test_string_size(self):
        """Test size calculation for string."""
        data = "test string"
        size = get_object_size(data)
        assert size > 0


class TestDeepDiff:
    """Test deep_diff function."""

    def test_no_changes(self):
        """Test diff when no changes."""
        before = {"a": 1, "b": 2}
        after = {"a": 1, "b": 2}
        diff = deep_diff(before, after)
        assert diff == {}

    def test_added_field(self):
        """Test diff with added field."""
        before = {"a": 1}
        after = {"a": 1, "b": 2}
        diff = deep_diff(before, after)
        assert "added" in diff
        assert diff["added"]["b"] == 2

    def test_removed_field(self):
        """Test diff with removed field."""
        before = {"a": 1, "b": 2}
        after = {"a": 1}
        diff = deep_diff(before, after)
        assert "removed" in diff
        assert diff["removed"]["b"] == 2

    def test_changed_field(self):
        """Test diff with changed field."""
        before = {"a": 1, "b": 2}
        after = {"a": 1, "b": 3}
        diff = deep_diff(before, after)
        assert "changed" in diff
        assert diff["changed"]["b"]["before"] == 2
        assert diff["changed"]["b"]["after"] == 3

    def test_multiple_changes(self):
        """Test diff with multiple types of changes."""
        before = {"a": 1, "b": 2, "c": 3}
        after = {"a": 10, "c": 3, "d": 4}
        diff = deep_diff(before, after)
        assert "changed" in diff
        assert "a" in diff["changed"]
        assert "removed" in diff
        assert "b" in diff["removed"]
        assert "added" in diff
        assert "d" in diff["added"]

    def test_empty_before(self):
        """Test diff with empty before state."""
        before = {}
        after = {"a": 1}
        diff = deep_diff(before, after)
        assert "added" in diff
        assert diff["added"]["a"] == 1

    def test_empty_after(self):
        """Test diff with empty after state."""
        before = {"a": 1}
        after = {}
        diff = deep_diff(before, after)
        assert "removed" in diff
        assert diff["removed"]["a"] == 1


class TestStateTransitionTracker:
    """Test StateTransitionTracker class."""

    def test_initialization(self):
        """Test tracker initialization."""
        tracker = StateTransitionTracker()
        assert tracker._max_history == 100
        assert tracker._enable_memory_tracking is True
        assert tracker._state_history == []
        assert tracker._memory_snapshots == []

    def test_initialization_custom_settings(self):
        """Test tracker initialization with custom settings."""
        tracker = StateTransitionTracker(max_history=50, enable_memory_tracking=False)
        assert tracker._max_history == 50
        assert tracker._enable_memory_tracking is False

    def test_record_transition(self):
        """Test recording a state transition."""
        tracker = StateTransitionTracker()
        mock_span = MagicMock()
        mock_span.set_attribute = MagicMock()
        mock_span.add_event = MagicMock()

        before = {"messages": []}
        after = {"messages": ["hello"]}

        diff = tracker.record_transition(
            node_name="test_node",
            before_state=before,
            after_state=after,
            span=mock_span,
        )

        assert "changed" in diff
        assert len(tracker._state_history) == 1
        assert tracker._state_history[0]["node"] == "test_node"

    def test_record_transition_with_added_field(self):
        """Test recording transition with new field."""
        tracker = StateTransitionTracker()
        mock_span = MagicMock()

        before = {"a": 1}
        after = {"a": 1, "b": 2}

        diff = tracker.record_transition(
            node_name="test_node",
            before_state=before,
            after_state=after,
            span=mock_span,
        )

        assert "added" in diff
        assert diff["added"]["b"] == 2

    def test_history_limit(self):
        """Test that history is limited to max_history."""
        tracker = StateTransitionTracker(max_history=3)
        mock_span = MagicMock()

        for i in range(5):
            tracker.record_transition(
                node_name=f"node_{i}",
                before_state={"count": i},
                after_state={"count": i + 1},
                span=mock_span,
            )

        assert len(tracker._state_history) == 3
        assert tracker._state_history[0]["node"] == "node_2"

    def test_get_state_at_node(self):
        """Test getting state at a specific node."""
        tracker = StateTransitionTracker()
        mock_span = MagicMock()

        tracker.record_transition(
            node_name="node_a",
            before_state={},
            after_state={"result": "a"},
            span=mock_span,
        )
        tracker.record_transition(
            node_name="node_b",
            before_state={"result": "a"},
            after_state={"result": "b"},
            span=mock_span,
        )

        state = tracker.get_state_at_node("node_a")
        assert state == {"result": "a"}

        state = tracker.get_state_at_node("node_b")
        assert state == {"result": "b"}

    def test_get_state_at_node_not_found(self):
        """Test getting state for non-existent node."""
        tracker = StateTransitionTracker()
        state = tracker.get_state_at_node("nonexistent")
        assert state is None

    def test_get_history(self):
        """Test getting full history."""
        tracker = StateTransitionTracker()
        mock_span = MagicMock()

        tracker.record_transition(
            node_name="node_1",
            before_state={},
            after_state={"a": 1},
            span=mock_span,
        )
        tracker.record_transition(
            node_name="node_2",
            before_state={"a": 1},
            after_state={"a": 2},
            span=mock_span,
        )

        history = tracker.get_history()
        assert len(history) == 2
        assert history[0]["node"] == "node_1"
        assert history[1]["node"] == "node_2"

    def test_get_field_history(self):
        """Test getting history for a specific field."""
        tracker = StateTransitionTracker()
        mock_span = MagicMock()

        tracker.record_transition(
            node_name="node_1",
            before_state={},
            after_state={"count": 1},
            span=mock_span,
        )
        tracker.record_transition(
            node_name="node_2",
            before_state={"count": 1},
            after_state={"count": 2},
            span=mock_span,
        )
        tracker.record_transition(
            node_name="node_3",
            before_state={"count": 2},
            after_state={"count": 2, "other": "x"},
            span=mock_span,
        )

        field_history = tracker.get_field_history("count")
        assert len(field_history) == 2  # Added and changed
        assert field_history[0]["node"] == "node_1"
        assert field_history[1]["node"] == "node_2"

    def test_memory_tracking(self):
        """Test memory tracking functionality."""
        tracker = StateTransitionTracker(enable_memory_tracking=True)
        mock_span = MagicMock()

        tracker.record_transition(
            node_name="node_1",
            before_state={},
            after_state={"data": "x" * 1000},
            span=mock_span,
        )

        assert len(tracker._memory_snapshots) == 1
        assert tracker._memory_snapshots[0] > 0

    def test_memory_tracking_disabled(self):
        """Test that memory tracking can be disabled."""
        tracker = StateTransitionTracker(enable_memory_tracking=False)
        mock_span = MagicMock()

        tracker.record_transition(
            node_name="node_1",
            before_state={},
            after_state={"data": "test"},
            span=mock_span,
        )

        assert len(tracker._memory_snapshots) == 0

    def test_get_memory_stats(self):
        """Test getting memory statistics."""
        tracker = StateTransitionTracker()
        mock_span = MagicMock()

        for i in range(3):
            tracker.record_transition(
                node_name=f"node_{i}",
                before_state={},
                after_state={"data": "x" * (100 * (i + 1))},
                span=mock_span,
            )

        stats = tracker.get_memory_stats()
        assert "current_bytes" in stats
        assert "peak_bytes" in stats
        assert "min_bytes" in stats
        assert "avg_bytes" in stats
        assert "sample_count" in stats
        assert stats["sample_count"] == 3

    def test_get_memory_stats_no_data(self):
        """Test getting memory stats with no data."""
        tracker = StateTransitionTracker()
        stats = tracker.get_memory_stats()
        assert "error" in stats

    def test_reset(self):
        """Test resetting the tracker."""
        tracker = StateTransitionTracker()
        mock_span = MagicMock()

        tracker.record_transition(
            node_name="node_1",
            before_state={},
            after_state={"a": 1},
            span=mock_span,
        )

        assert len(tracker._state_history) == 1
        assert len(tracker._memory_snapshots) == 1

        tracker.reset()

        assert len(tracker._state_history) == 0
        assert len(tracker._memory_snapshots) == 0
