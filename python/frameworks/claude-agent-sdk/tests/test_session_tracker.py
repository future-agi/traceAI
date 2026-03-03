"""Tests for SessionTracker."""

import pytest
from unittest.mock import MagicMock, patch
import time


class TestSessionInfo:
    """Test SessionInfo dataclass."""

    def test_create_session_info(self):
        """Test creating SessionInfo."""
        from traceai_claude_agent_sdk._session_tracker import SessionInfo

        info = SessionInfo(
            session_id="sess-123",
            created_at=time.time(),
        )

        assert info.session_id == "sess-123"
        assert info.is_new is True
        assert info.total_queries == 0
        assert info.total_input_tokens == 0

    def test_session_with_previous(self):
        """Test session with previous session ID."""
        from traceai_claude_agent_sdk._session_tracker import SessionInfo

        info = SessionInfo(
            session_id="sess-456",
            created_at=time.time(),
            is_new=False,
            previous_session_id="sess-123",
        )

        assert info.previous_session_id == "sess-123"
        assert info.is_new is False


class TestSessionTracker:
    """Test SessionTracker class."""

    def test_init(self):
        """Test SessionTracker initialization."""
        from traceai_claude_agent_sdk._session_tracker import SessionTracker

        mock_tracer = MagicMock()
        tracker = SessionTracker(mock_tracer)

        assert tracker.tracer is mock_tracer
        assert tracker.session_count == 0

    def test_start_session(self):
        """Test starting a new session."""
        from traceai_claude_agent_sdk._session_tracker import SessionTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_context = MagicMock()
        mock_context.is_valid = True
        mock_context.trace_id = 12345
        mock_context.span_id = 67890
        mock_span.get_span_context.return_value = mock_context

        tracker = SessionTracker(mock_tracer)
        info = tracker.start_session("sess-123", mock_span)

        assert info.session_id == "sess-123"
        assert info.is_new is True
        assert info.total_queries == 1
        assert tracker.session_count == 1

        # Verify span attributes were set
        mock_span.set_attribute.assert_called()

    def test_start_session_sets_attributes(self):
        """Test that start_session sets correct attributes."""
        from traceai_claude_agent_sdk._session_tracker import SessionTracker
        from traceai_claude_agent_sdk._attributes import ClaudeAgentAttributes

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_context = MagicMock()
        mock_context.is_valid = False
        mock_span.get_span_context.return_value = mock_context

        tracker = SessionTracker(mock_tracer)
        tracker.start_session("sess-123", mock_span)

        calls = mock_span.set_attribute.call_args_list
        attr_dict = {c[0][0]: c[0][1] for c in calls}

        assert attr_dict[ClaudeAgentAttributes.GEN_AI_CONVERSATION_ID] == "sess-123"
        assert attr_dict[ClaudeAgentAttributes.SESSION_IS_NEW] is True
        assert attr_dict[ClaudeAgentAttributes.SESSION_IS_RESUMED] is False

    def test_start_resumed_session(self):
        """Test starting a resumed session."""
        from traceai_claude_agent_sdk._session_tracker import SessionTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_context = MagicMock()
        mock_context.is_valid = False
        mock_span.get_span_context.return_value = mock_context

        tracker = SessionTracker(mock_tracer)
        info = tracker.start_session(
            "sess-456",
            mock_span,
            is_resumed=True,
            previous_session_id="sess-123",
        )

        assert info.session_id == "sess-456"
        assert info.is_new is False
        assert info.previous_session_id == "sess-123"

    def test_continue_existing_session(self):
        """Test continuing an existing session."""
        from traceai_claude_agent_sdk._session_tracker import SessionTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_context = MagicMock()
        mock_context.is_valid = False
        mock_span.get_span_context.return_value = mock_context

        tracker = SessionTracker(mock_tracer)

        # Start session
        tracker.start_session("sess-123", mock_span)

        # Continue same session
        info = tracker.start_session("sess-123", mock_span)

        assert info.total_queries == 2
        assert tracker.session_count == 1

    def test_resume_session(self):
        """Test resuming a session."""
        from traceai_claude_agent_sdk._session_tracker import SessionTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_context = MagicMock()
        mock_context.is_valid = True
        mock_context.trace_id = 12345
        mock_context.span_id = 67890
        mock_span.get_span_context.return_value = mock_context

        tracker = SessionTracker(mock_tracer)

        # Start original session
        tracker.start_session("sess-123", mock_span)

        # Resume as new session
        info = tracker.resume_session("sess-456", "sess-123", mock_span)

        assert info.session_id == "sess-456"
        assert info.is_new is False
        assert info.previous_session_id == "sess-123"
        assert tracker.session_count == 2

    def test_resume_carries_metrics(self):
        """Test that resume carries over metrics."""
        from traceai_claude_agent_sdk._session_tracker import SessionTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_context = MagicMock()
        mock_context.is_valid = False
        mock_span.get_span_context.return_value = mock_context

        tracker = SessionTracker(mock_tracer)

        # Start and update original session
        tracker.start_session("sess-123", mock_span)
        tracker.update_session_metrics(
            "sess-123",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.005,
        )

        # Resume
        info = tracker.resume_session("sess-456", "sess-123", mock_span)

        assert info.total_input_tokens == 100
        assert info.total_output_tokens == 50
        assert info.total_cost_usd == 0.005

    def test_fork_session(self):
        """Test forking a session."""
        from traceai_claude_agent_sdk._session_tracker import SessionTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_context = MagicMock()
        mock_context.is_valid = True
        mock_context.trace_id = 12345
        mock_context.span_id = 67890
        mock_span.get_span_context.return_value = mock_context

        tracker = SessionTracker(mock_tracer)

        # Start original session
        tracker.start_session("sess-123", mock_span)

        # Fork
        info = tracker.fork_session("sess-fork", "sess-123", mock_span)

        assert info.session_id == "sess-fork"
        assert info.is_new is True  # Fork is a new branch
        assert info.fork_from_session_id == "sess-123"
        assert tracker.session_count == 2

    def test_update_session_metrics(self):
        """Test updating session metrics."""
        from traceai_claude_agent_sdk._session_tracker import SessionTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_context = MagicMock()
        mock_context.is_valid = False
        mock_span.get_span_context.return_value = mock_context

        tracker = SessionTracker(mock_tracer)
        tracker.start_session("sess-123", mock_span)

        tracker.update_session_metrics(
            "sess-123",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.005,
            turns=3,
        )

        info = tracker.get_session("sess-123")
        assert info.total_input_tokens == 100
        assert info.total_output_tokens == 50
        assert info.total_cost_usd == 0.005
        assert info.total_turns == 3

    def test_update_metrics_accumulates(self):
        """Test that metrics accumulate."""
        from traceai_claude_agent_sdk._session_tracker import SessionTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_context = MagicMock()
        mock_context.is_valid = False
        mock_span.get_span_context.return_value = mock_context

        tracker = SessionTracker(mock_tracer)
        tracker.start_session("sess-123", mock_span)

        tracker.update_session_metrics("sess-123", input_tokens=100)
        tracker.update_session_metrics("sess-123", input_tokens=50)

        info = tracker.get_session("sess-123")
        assert info.total_input_tokens == 150

    def test_get_session(self):
        """Test getting session info."""
        from traceai_claude_agent_sdk._session_tracker import SessionTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_context = MagicMock()
        mock_context.is_valid = False
        mock_span.get_span_context.return_value = mock_context

        tracker = SessionTracker(mock_tracer)
        tracker.start_session("sess-123", mock_span)

        info = tracker.get_session("sess-123")
        assert info is not None
        assert info.session_id == "sess-123"

        info = tracker.get_session("nonexistent")
        assert info is None

    def test_get_active_session(self):
        """Test getting active session."""
        from traceai_claude_agent_sdk._session_tracker import SessionTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_context = MagicMock()
        mock_context.is_valid = False
        mock_span.get_span_context.return_value = mock_context

        tracker = SessionTracker(mock_tracer)

        assert tracker.get_active_session() is None

        tracker.start_session("sess-123", mock_span)
        info = tracker.get_active_session()
        assert info is not None
        assert info.session_id == "sess-123"

    def test_end_session(self):
        """Test ending a session."""
        from traceai_claude_agent_sdk._session_tracker import SessionTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_context = MagicMock()
        mock_context.is_valid = False
        mock_span.get_span_context.return_value = mock_context

        tracker = SessionTracker(mock_tracer)
        tracker.start_session("sess-123", mock_span)
        tracker.update_session_metrics(
            "sess-123",
            input_tokens=100,
            output_tokens=50,
        )

        info = tracker.end_session("sess-123", mock_span)

        assert info is not None

        # Verify final metrics were recorded
        calls = mock_span.set_attribute.call_args_list
        attr_dict = {c[0][0]: c[0][1] for c in calls}
        assert "claude_agent.session.total_input_tokens" in attr_dict

    def test_get_session_history(self):
        """Test getting session history chain."""
        from traceai_claude_agent_sdk._session_tracker import SessionTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_context = MagicMock()
        mock_context.is_valid = False
        mock_span.get_span_context.return_value = mock_context

        tracker = SessionTracker(mock_tracer)

        # Create chain: sess-1 -> sess-2 -> sess-3
        tracker.start_session("sess-1", mock_span)
        tracker.resume_session("sess-2", "sess-1", mock_span)
        tracker.resume_session("sess-3", "sess-2", mock_span)

        history = tracker.get_session_history("sess-3")
        assert history == ["sess-1", "sess-2", "sess-3"]

    def test_clear(self):
        """Test clearing tracker."""
        from traceai_claude_agent_sdk._session_tracker import SessionTracker

        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_context = MagicMock()
        mock_context.is_valid = False
        mock_span.get_span_context.return_value = mock_context

        tracker = SessionTracker(mock_tracer)
        tracker.start_session("sess-123", mock_span)

        tracker.clear()

        assert tracker.session_count == 0
        assert tracker.get_active_session() is None
