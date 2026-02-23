"""Tests for SubagentTracker."""

import pytest
from unittest.mock import MagicMock, patch
import time


class TestSubagentInfo:
    """Test SubagentInfo dataclass."""

    def test_create_subagent_info(self):
        """Test creating SubagentInfo."""
        from traceai_claude_agent_sdk._subagent_tracker import SubagentInfo

        mock_span = MagicMock()
        info = SubagentInfo(
            tool_use_id="test-id",
            span=mock_span,
            start_time=time.time(),
            subagent_type="Explore",
        )

        assert info.tool_use_id == "test-id"
        assert info.span is mock_span
        assert info.subagent_type == "Explore"
        assert info.total_input_tokens == 0
        assert info.total_output_tokens == 0

    def test_default_values(self):
        """Test default values in SubagentInfo."""
        from traceai_claude_agent_sdk._subagent_tracker import SubagentInfo

        info = SubagentInfo(
            tool_use_id="test",
            span=MagicMock(),
            start_time=time.time(),
        )

        assert info.subagent_type is None
        assert info.description is None
        assert info.prompt is None
        assert info.parent_tool_use_id is None
        assert info.child_subagent_ids == []


class TestSubagentTracker:
    """Test SubagentTracker class."""

    def test_init(self):
        """Test SubagentTracker initialization."""
        from traceai_claude_agent_sdk._subagent_tracker import SubagentTracker

        mock_tracer = MagicMock()
        tracker = SubagentTracker(mock_tracer)

        assert tracker.tracer is mock_tracer
        assert tracker.active_count == 0
        assert tracker.completed_count == 0

    def test_start_subagent(self):
        """Test starting a subagent."""
        from traceai_claude_agent_sdk._subagent_tracker import SubagentTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_span.return_value = mock_span

        tracker = SubagentTracker(mock_tracer)

        input_data = {
            "subagent_type": "Explore",
            "description": "Search for files",
            "prompt": "Find all Python files",
        }

        span = tracker.start_subagent("tool-123", input_data)

        assert span is mock_span
        assert tracker.active_count == 1
        mock_tracer.start_span.assert_called_once()

    def test_start_subagent_with_parent_span(self):
        """Test starting subagent with parent span."""
        from traceai_claude_agent_sdk._subagent_tracker import SubagentTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_parent = MagicMock()
        mock_tracer.start_span.return_value = mock_span

        tracker = SubagentTracker(mock_tracer)

        span = tracker.start_subagent(
            "tool-123",
            {"subagent_type": "Plan"},
            parent_span=mock_parent,
        )

        assert span is mock_span
        # Verify context was set
        call_kwargs = mock_tracer.start_span.call_args[1]
        assert "context" in call_kwargs

    def test_start_nested_subagent(self):
        """Test starting a nested subagent."""
        from traceai_claude_agent_sdk._subagent_tracker import SubagentTracker

        mock_tracer = MagicMock()
        mock_tracer.start_span.return_value = MagicMock()

        tracker = SubagentTracker(mock_tracer)

        # Start parent subagent
        tracker.start_subagent("parent-123", {"subagent_type": "Explore"})

        # Start nested subagent
        tracker.start_subagent(
            "child-456",
            {"subagent_type": "Plan"},
            parent_tool_use_id="parent-123",
        )

        assert tracker.active_count == 2

        # Verify parent has child reference
        parent_info = tracker.get_active_subagent("parent-123")
        assert "child-456" in parent_info.child_subagent_ids

    def test_end_subagent(self):
        """Test ending a subagent."""
        from traceai_claude_agent_sdk._subagent_tracker import SubagentTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_span.return_value = mock_span

        tracker = SubagentTracker(mock_tracer)
        tracker.start_subagent("tool-123", {"subagent_type": "Explore"})

        info = tracker.end_subagent("tool-123", result="Found 5 files")

        assert info is not None
        assert info.tool_use_id == "tool-123"
        assert tracker.active_count == 0
        assert tracker.completed_count == 1
        mock_span.end.assert_called_once()

    def test_end_subagent_with_usage(self):
        """Test ending subagent with usage metrics."""
        from traceai_claude_agent_sdk._subagent_tracker import SubagentTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_span.return_value = mock_span

        tracker = SubagentTracker(mock_tracer)
        tracker.start_subagent("tool-123", {"subagent_type": "Explore"})

        usage = {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_cost_usd": 0.005,
        }
        info = tracker.end_subagent("tool-123", usage=usage)

        assert info.total_input_tokens == 100
        assert info.total_output_tokens == 50
        assert info.total_cost_usd == 0.005

    def test_end_subagent_with_error(self):
        """Test ending subagent with error."""
        from traceai_claude_agent_sdk._subagent_tracker import SubagentTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_span.return_value = mock_span

        tracker = SubagentTracker(mock_tracer)
        tracker.start_subagent("tool-123", {"subagent_type": "Explore"})

        error = ValueError("Something went wrong")
        info = tracker.end_subagent("tool-123", error=error)

        mock_span.record_exception.assert_called_once_with(error)

    def test_end_nonexistent_subagent(self):
        """Test ending non-existent subagent."""
        from traceai_claude_agent_sdk._subagent_tracker import SubagentTracker

        mock_tracer = MagicMock()
        tracker = SubagentTracker(mock_tracer)

        info = tracker.end_subagent("nonexistent")
        assert info is None

    def test_get_active_subagent(self):
        """Test getting active subagent info."""
        from traceai_claude_agent_sdk._subagent_tracker import SubagentTracker

        mock_tracer = MagicMock()
        mock_tracer.start_span.return_value = MagicMock()

        tracker = SubagentTracker(mock_tracer)
        tracker.start_subagent("tool-123", {"subagent_type": "Plan"})

        info = tracker.get_active_subagent("tool-123")
        assert info is not None
        assert info.subagent_type == "Plan"

        info = tracker.get_active_subagent("nonexistent")
        assert info is None

    def test_get_parent_subagent_span(self):
        """Test getting parent subagent span."""
        from traceai_claude_agent_sdk._subagent_tracker import SubagentTracker

        mock_tracer = MagicMock()
        mock_parent_span = MagicMock()
        mock_tracer.start_span.return_value = mock_parent_span

        tracker = SubagentTracker(mock_tracer)
        tracker.start_subagent("parent-123", {"subagent_type": "Explore"})

        span = tracker.get_parent_subagent_span("parent-123")
        assert span is mock_parent_span

        span = tracker.get_parent_subagent_span("nonexistent")
        assert span is None

    def test_is_nested_subagent(self):
        """Test checking if subagent is nested."""
        from traceai_claude_agent_sdk._subagent_tracker import SubagentTracker

        mock_tracer = MagicMock()
        mock_tracer.start_span.return_value = MagicMock()

        tracker = SubagentTracker(mock_tracer)

        # Start parent
        tracker.start_subagent("parent-123", {"subagent_type": "Explore"})

        # Start nested child
        tracker.start_subagent(
            "child-456",
            {"subagent_type": "Plan"},
            parent_tool_use_id="parent-123",
        )

        assert not tracker.is_nested_subagent("parent-123")
        assert tracker.is_nested_subagent("child-456")

    def test_record_subagent_turn(self):
        """Test recording turns in subagent."""
        from traceai_claude_agent_sdk._subagent_tracker import SubagentTracker

        mock_tracer = MagicMock()
        mock_tracer.start_span.return_value = MagicMock()

        tracker = SubagentTracker(mock_tracer)
        tracker.start_subagent("tool-123", {"subagent_type": "Explore"})

        tracker.record_subagent_turn("tool-123")
        tracker.record_subagent_turn("tool-123")

        info = tracker.get_active_subagent("tool-123")
        assert info.turns == 2

    def test_record_subagent_tool_call(self):
        """Test recording tool calls in subagent."""
        from traceai_claude_agent_sdk._subagent_tracker import SubagentTracker

        mock_tracer = MagicMock()
        mock_tracer.start_span.return_value = MagicMock()

        tracker = SubagentTracker(mock_tracer)
        tracker.start_subagent("tool-123", {"subagent_type": "Explore"})

        tracker.record_subagent_tool_call("tool-123")
        tracker.record_subagent_tool_call("tool-123")
        tracker.record_subagent_tool_call("tool-123")

        info = tracker.get_active_subagent("tool-123")
        assert info.tool_calls == 3

    def test_get_hierarchy_costs(self):
        """Test getting hierarchy costs."""
        from traceai_claude_agent_sdk._subagent_tracker import SubagentTracker

        mock_tracer = MagicMock()
        mock_tracer.start_span.return_value = MagicMock()

        tracker = SubagentTracker(mock_tracer)
        tracker.start_subagent("parent-123", {"subagent_type": "Explore"})

        usage = {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_cost_usd": 0.005,
        }
        tracker.end_subagent("parent-123", usage=usage)

        costs = tracker.get_hierarchy_costs("parent-123")
        assert costs["input_tokens"] == 100
        assert costs["output_tokens"] == 50
        assert costs["total_cost_usd"] == 0.005

    def test_clear(self):
        """Test clearing tracker."""
        from traceai_claude_agent_sdk._subagent_tracker import SubagentTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_span.return_value = mock_span

        tracker = SubagentTracker(mock_tracer)
        tracker.start_subagent("tool-123", {"subagent_type": "Explore"})

        tracker.clear()

        assert tracker.active_count == 0
        assert tracker.completed_count == 0
        mock_span.end.assert_called_once()


class TestSpanName:
    """Test span name constant."""

    def test_subagent_span_name(self):
        """Test subagent span name."""
        from traceai_claude_agent_sdk._subagent_tracker import SUBAGENT_SPAN_NAME

        assert SUBAGENT_SPAN_NAME == "claude_agent.subagent"
