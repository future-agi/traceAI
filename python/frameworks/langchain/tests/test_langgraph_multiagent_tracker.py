"""Tests for LangGraph multi-agent tracker module."""

import pytest
from unittest.mock import MagicMock


class TestAgentMessage:
    """Test AgentMessage class."""

    def test_initialization(self):
        """Test AgentMessage initialization."""
        from traceai_langchain._langgraph._multiagent_tracker import AgentMessage

        msg = AgentMessage(
            from_agent="researcher",
            to_agent="coder",
            message_type="task",
        )

        assert msg.from_agent == "researcher"
        assert msg.to_agent == "coder"
        assert msg.message_type == "task"
        assert msg.correlation_id is not None
        assert msg.timestamp is not None

    def test_with_correlation_id(self):
        """Test with provided correlation ID."""
        from traceai_langchain._langgraph._multiagent_tracker import AgentMessage

        msg = AgentMessage(
            from_agent="a",
            to_agent="b",
            correlation_id="custom_123",
        )

        assert msg.correlation_id == "custom_123"

    def test_to_dict(self):
        """Test to_dict conversion."""
        from traceai_langchain._langgraph._multiagent_tracker import AgentMessage

        msg = AgentMessage("a", "b", "result")
        msg.content_preview = "Some result data"

        result = msg.to_dict()

        assert result["from_agent"] == "a"
        assert result["to_agent"] == "b"
        assert result["message_type"] == "result"
        assert result["content_preview"] == "Some result data"


class TestSupervisorDecision:
    """Test SupervisorDecision class."""

    def test_initialization(self):
        """Test SupervisorDecision initialization."""
        from traceai_langchain._langgraph._multiagent_tracker import SupervisorDecision

        decision = SupervisorDecision(
            supervisor_name="main_supervisor",
            selected_agent="coder",
            available_agents=["researcher", "coder", "analyst"],
            reason="Code task identified",
        )

        assert decision.supervisor_name == "main_supervisor"
        assert decision.selected_agent == "coder"
        assert decision.available_agents == ["researcher", "coder", "analyst"]
        assert decision.reason == "Code task identified"

    def test_to_dict(self):
        """Test to_dict conversion."""
        from traceai_langchain._langgraph._multiagent_tracker import SupervisorDecision

        decision = SupervisorDecision("sup", "agent1", ["agent1", "agent2"])

        result = decision.to_dict()

        assert result["supervisor_name"] == "sup"
        assert result["selected_agent"] == "agent1"
        assert "agent1" in result["available_agents"]


class TestAgentExecution:
    """Test AgentExecution class."""

    def test_initialization(self):
        """Test AgentExecution initialization."""
        from traceai_langchain._langgraph._multiagent_tracker import AgentExecution

        execution = AgentExecution(
            agent_name="coder",
            agent_type="worker",
        )

        assert execution.agent_name == "coder"
        assert execution.agent_type == "worker"
        assert execution.execution_count == 0
        assert execution.total_duration_ms == 0.0

    def test_to_dict(self):
        """Test to_dict conversion."""
        from traceai_langchain._langgraph._multiagent_tracker import AgentExecution

        execution = AgentExecution("agent1", "supervisor")
        execution.execution_count = 5
        execution.tasks_completed = 4
        execution.errors = 1

        result = execution.to_dict()

        assert result["agent_name"] == "agent1"
        assert result["agent_type"] == "supervisor"
        assert result["execution_count"] == 5
        assert result["tasks_completed"] == 4
        assert result["errors"] == 1


class TestMultiAgentTracker:
    """Test MultiAgentTracker class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock()

    def test_initialization(self):
        """Test MultiAgentTracker initialization."""
        from traceai_langchain._langgraph._multiagent_tracker import MultiAgentTracker

        tracker = MultiAgentTracker(self.mock_tracer)

        assert tracker._tracer == self.mock_tracer
        assert tracker._messages == []
        assert tracker._supervisor_decisions == []
        assert tracker._agent_stats == {}

    def test_track_agent_message(self):
        """Test tracking an agent message."""
        from traceai_langchain._langgraph._multiagent_tracker import MultiAgentTracker

        tracker = MultiAgentTracker(self.mock_tracer)

        correlation_id = tracker.track_agent_message(
            from_agent="researcher",
            to_agent="coder",
            message="Please implement this feature",
            message_type="task",
        )

        assert correlation_id is not None
        assert len(tracker._messages) == 1
        assert tracker._messages[0].from_agent == "researcher"
        assert tracker._messages[0].to_agent == "coder"

        # Check agent stats updated
        assert tracker._agent_stats["researcher"].messages_sent == 1
        assert tracker._agent_stats["coder"].messages_received == 1

    def test_track_agent_message_with_span(self):
        """Test tracking message with span."""
        from traceai_langchain._langgraph._multiagent_tracker import MultiAgentTracker

        tracker = MultiAgentTracker(self.mock_tracer)

        mock_span = MagicMock()
        mock_span.is_recording.return_value = True

        tracker.track_agent_message(
            from_agent="a",
            to_agent="b",
            message="test",
            span=mock_span,
        )

        mock_span.set_attribute.assert_called()
        mock_span.add_event.assert_called()

    def test_track_agent_message_correlation(self):
        """Test message correlation."""
        from traceai_langchain._langgraph._multiagent_tracker import MultiAgentTracker

        tracker = MultiAgentTracker(self.mock_tracer)

        # First message starts a conversation
        corr_id = tracker.track_agent_message("a", "b", "task1")

        # Second message continues the conversation
        tracker.track_agent_message("b", "a", "result1", correlation_id=corr_id)

        assert len(tracker._correlation_map[corr_id]) == 2

    def test_track_supervisor_routing(self):
        """Test tracking supervisor routing decision."""
        from traceai_langchain._langgraph._multiagent_tracker import MultiAgentTracker

        tracker = MultiAgentTracker(self.mock_tracer)

        decision = tracker.track_supervisor_routing(
            supervisor_name="main_sup",
            selected_agent="coder",
            available_agents=["researcher", "coder", "analyst"],
            reason="Code task needed",
        )

        assert decision.supervisor_name == "main_sup"
        assert decision.selected_agent == "coder"
        assert len(tracker._supervisor_decisions) == 1

        # Check supervisor agent type
        assert tracker._agent_stats["main_sup"].agent_type == "supervisor"

    def test_track_supervisor_routing_with_span(self):
        """Test tracking supervisor routing with span."""
        from traceai_langchain._langgraph._multiagent_tracker import MultiAgentTracker

        tracker = MultiAgentTracker(self.mock_tracer)

        mock_span = MagicMock()
        mock_span.is_recording.return_value = True

        tracker.track_supervisor_routing(
            supervisor_name="sup",
            selected_agent="agent1",
            available_agents=["agent1", "agent2"],
            reason="Best fit",
            span=mock_span,
        )

        mock_span.set_attribute.assert_called()
        mock_span.add_event.assert_called()

    def test_track_agent_execution(self):
        """Test tracking agent execution."""
        from traceai_langchain._langgraph._multiagent_tracker import MultiAgentTracker

        tracker = MultiAgentTracker(self.mock_tracer)

        tracker.track_agent_execution("coder", 100.0, success=True)
        tracker.track_agent_execution("coder", 50.0, success=True)
        tracker.track_agent_execution("coder", 75.0, success=False)

        stats = tracker._agent_stats["coder"]
        assert stats.execution_count == 3
        assert stats.total_duration_ms == 225.0
        assert stats.tasks_completed == 2
        assert stats.errors == 1

    def test_get_messages(self):
        """Test getting messages."""
        from traceai_langchain._langgraph._multiagent_tracker import MultiAgentTracker

        tracker = MultiAgentTracker(self.mock_tracer)

        corr1 = tracker.track_agent_message("a", "b", "msg1")
        tracker.track_agent_message("c", "d", "msg2")
        tracker.track_agent_message("a", "b", "msg3", correlation_id=corr1)

        all_msgs = tracker.get_messages()
        assert len(all_msgs) == 3

        corr1_msgs = tracker.get_messages(correlation_id=corr1)
        assert len(corr1_msgs) == 2

    def test_get_supervisor_decisions(self):
        """Test getting supervisor decisions."""
        from traceai_langchain._langgraph._multiagent_tracker import MultiAgentTracker

        tracker = MultiAgentTracker(self.mock_tracer)

        tracker.track_supervisor_routing("sup1", "a", ["a", "b"])
        tracker.track_supervisor_routing("sup2", "b", ["a", "b"])

        decisions = tracker.get_supervisor_decisions()
        assert len(decisions) == 2

    def test_get_agent_stats(self):
        """Test getting agent statistics."""
        from traceai_langchain._langgraph._multiagent_tracker import MultiAgentTracker

        tracker = MultiAgentTracker(self.mock_tracer)

        tracker.track_agent_message("agent1", "agent2", "msg")
        tracker.track_agent_execution("agent1", 100.0)

        # Get all stats
        all_stats = tracker.get_agent_stats()
        assert "agent1" in all_stats
        assert "agent2" in all_stats

        # Get specific agent stats
        agent1_stats = tracker.get_agent_stats("agent1")
        assert agent1_stats["messages_sent"] == 1
        assert agent1_stats["execution_count"] == 1

        # Get non-existent agent
        missing = tracker.get_agent_stats("nonexistent")
        assert "error" in missing

    def test_get_conversation_thread(self):
        """Test getting conversation thread."""
        from traceai_langchain._langgraph._multiagent_tracker import MultiAgentTracker

        tracker = MultiAgentTracker(self.mock_tracer)

        corr_id = tracker.track_agent_message("user", "agent", "request")
        tracker.track_agent_message("agent", "tool", "call_tool", correlation_id=corr_id)
        tracker.track_agent_message("tool", "agent", "tool_result", correlation_id=corr_id)
        tracker.track_agent_message("agent", "user", "response", correlation_id=corr_id)

        thread = tracker.get_conversation_thread(corr_id)
        assert len(thread) == 4

    def test_get_stats(self):
        """Test getting aggregate statistics."""
        from traceai_langchain._langgraph._multiagent_tracker import MultiAgentTracker

        tracker = MultiAgentTracker(self.mock_tracer)

        # Create some activity
        corr1 = tracker.track_agent_message("a", "b", "task", message_type="task")
        tracker.track_agent_message("b", "a", "result", message_type="result", correlation_id=corr1)

        tracker.track_supervisor_routing("sup", "a", ["a", "b"])
        tracker.track_supervisor_routing("sup", "b", ["a", "b"])

        tracker.track_agent_execution("a", 100.0)
        tracker.track_agent_execution("b", 50.0, success=False)

        stats = tracker.get_stats()

        assert stats["total_agents"] == 3  # a, b, sup
        assert stats["total_messages"] == 2
        assert stats["total_supervisor_decisions"] == 2
        assert stats["total_executions"] == 2
        assert stats["total_errors"] == 1
        assert stats["message_types"]["task"] == 1
        assert stats["message_types"]["result"] == 1
        assert stats["agent_selection_frequency"]["a"] == 1
        assert stats["agent_selection_frequency"]["b"] == 1

    def test_reset(self):
        """Test resetting the tracker."""
        from traceai_langchain._langgraph._multiagent_tracker import MultiAgentTracker

        tracker = MultiAgentTracker(self.mock_tracer)

        tracker.track_agent_message("a", "b", "msg")
        tracker.track_supervisor_routing("sup", "a", ["a", "b"])
        tracker.track_agent_execution("a", 100.0)

        assert len(tracker._messages) > 0
        assert len(tracker._supervisor_decisions) > 0
        assert len(tracker._agent_stats) > 0

        tracker.reset()

        assert len(tracker._messages) == 0
        assert len(tracker._supervisor_decisions) == 0
        assert len(tracker._agent_stats) == 0
