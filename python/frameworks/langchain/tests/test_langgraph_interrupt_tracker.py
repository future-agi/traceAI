"""Tests for LangGraph interrupt/resume tracker module."""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone


class TestInterruptInfo:
    """Test InterruptInfo class."""

    def test_import(self):
        """Test that InterruptInfo can be imported."""
        from traceai_langchain._langgraph._interrupt_tracker import InterruptInfo
        assert InterruptInfo is not None

    def test_initialization(self):
        """Test InterruptInfo initialization."""
        from traceai_langchain._langgraph._interrupt_tracker import InterruptInfo

        info = InterruptInfo(
            thread_id="t1",
            node_name="approval_node",
            reason="needs_human_approval",
        )

        assert info.thread_id == "t1"
        assert info.node_name == "approval_node"
        assert info.reason == "needs_human_approval"
        assert info.is_intentional is True
        assert info.timestamp is not None

    def test_to_dict(self):
        """Test to_dict conversion."""
        from traceai_langchain._langgraph._interrupt_tracker import InterruptInfo

        info = InterruptInfo("t1", "node1", "reason1")
        info.state_snapshot = {"data": "test"}

        result = info.to_dict()

        assert result["thread_id"] == "t1"
        assert result["node_name"] == "node1"
        assert result["reason"] == "reason1"
        assert result["has_state_snapshot"] is True


class TestResumeInfo:
    """Test ResumeInfo class."""

    def test_initialization(self):
        """Test ResumeInfo initialization."""
        from traceai_langchain._langgraph._interrupt_tracker import ResumeInfo

        info = ResumeInfo(
            thread_id="t1",
            resume_input={"approved": True},
        )

        assert info.thread_id == "t1"
        assert info.resume_input == {"approved": True}
        assert info.from_interrupt is False

    def test_to_dict(self):
        """Test to_dict conversion."""
        from traceai_langchain._langgraph._interrupt_tracker import ResumeInfo

        info = ResumeInfo("t1")
        info.wait_duration_seconds = 120.5
        info.from_interrupt = True

        result = info.to_dict()

        assert result["thread_id"] == "t1"
        assert result["wait_duration_seconds"] == 120.5
        assert result["from_interrupt"] is True


class TestHumanDecision:
    """Test HumanDecision class."""

    def test_initialization(self):
        """Test HumanDecision initialization."""
        from traceai_langchain._langgraph._interrupt_tracker import HumanDecision

        decision = HumanDecision(
            decision="approved",
            thread_id="t1",
            approver_id="user123",
        )

        assert decision.decision == "approved"
        assert decision.thread_id == "t1"
        assert decision.approver_id == "user123"

    def test_to_dict(self):
        """Test to_dict conversion."""
        from traceai_langchain._langgraph._interrupt_tracker import HumanDecision

        decision = HumanDecision("rejected", "t1", "admin")
        decision.feedback = "Not ready for production"

        result = decision.to_dict()

        assert result["decision"] == "rejected"
        assert result["feedback"] == "Not ready for production"


class TestInterruptResumeTracker:
    """Test InterruptResumeTracker class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.mock_tracer = MagicMock()

    def test_initialization(self):
        """Test InterruptResumeTracker initialization."""
        from traceai_langchain._langgraph._interrupt_tracker import InterruptResumeTracker

        tracker = InterruptResumeTracker(self.mock_tracer)

        assert tracker._tracer == self.mock_tracer
        assert tracker._interrupted_traces == {}
        assert tracker._interrupt_history == []

    def test_on_interrupt_basic(self):
        """Test recording a basic interrupt."""
        from traceai_langchain._langgraph._interrupt_tracker import InterruptResumeTracker

        tracker = InterruptResumeTracker(self.mock_tracer)

        result = tracker.on_interrupt(
            thread_id="thread_1",
            node_name="approval_node",
            reason="needs_approval",
        )

        assert result.thread_id == "thread_1"
        assert result.node_name == "approval_node"
        assert result.reason == "needs_approval"
        assert "thread_1" in tracker._interrupted_traces
        assert len(tracker._interrupt_history) == 1

    def test_on_interrupt_with_state(self):
        """Test recording interrupt with state snapshot."""
        from traceai_langchain._langgraph._interrupt_tracker import InterruptResumeTracker

        tracker = InterruptResumeTracker(self.mock_tracer)

        result = tracker.on_interrupt(
            thread_id="t1",
            node_name="node1",
            reason="test",
            state={"messages": ["hello"], "status": "pending"},
        )

        assert result.state_snapshot == {"messages": ["hello"], "status": "pending"}

    def test_on_interrupt_with_span(self):
        """Test recording interrupt with span attributes."""
        from traceai_langchain._langgraph._interrupt_tracker import InterruptResumeTracker

        tracker = InterruptResumeTracker(self.mock_tracer)

        mock_span = MagicMock()
        mock_span.is_recording.return_value = True
        mock_ctx = MagicMock()
        mock_ctx.trace_id = 12345
        mock_ctx.span_id = 67890
        mock_span.get_span_context.return_value = mock_ctx

        result = tracker.on_interrupt(
            thread_id="t1",
            node_name="node1",
            reason="test",
            span=mock_span,
            is_intentional=True,
        )

        assert result.trace_id == 12345
        assert result.span_id == 67890

        # Verify span attributes were set
        mock_span.set_attribute.assert_called()
        mock_span.add_event.assert_called()
        # Verify OK status was set (not ERROR!)
        mock_span.set_status.assert_called()

    def test_on_interrupt_intentional_vs_error(self):
        """Test that intentional interrupts are tracked differently."""
        from traceai_langchain._langgraph._interrupt_tracker import InterruptResumeTracker

        tracker = InterruptResumeTracker(self.mock_tracer)

        # Intentional interrupt
        intentional = tracker.on_interrupt(
            thread_id="t1",
            node_name="node1",
            reason="user_input_needed",
            is_intentional=True,
        )

        # Error interrupt
        error = tracker.on_interrupt(
            thread_id="t2",
            node_name="node2",
            reason="timeout",
            is_intentional=False,
        )

        assert intentional.is_intentional is True
        assert error.is_intentional is False

    def test_on_resume_basic(self):
        """Test recording a basic resume."""
        from traceai_langchain._langgraph._interrupt_tracker import InterruptResumeTracker

        tracker = InterruptResumeTracker(self.mock_tracer)

        result = tracker.on_resume(
            thread_id="t1",
            resume_input={"approved": True},
        )

        assert result.thread_id == "t1"
        assert result.resume_input == {"approved": True}
        assert result.from_interrupt is False  # No previous interrupt
        assert len(tracker._resume_history) == 1

    def test_on_resume_from_interrupt(self):
        """Test resume that follows an interrupt."""
        from traceai_langchain._langgraph._interrupt_tracker import InterruptResumeTracker

        tracker = InterruptResumeTracker(self.mock_tracer)

        # First, record an interrupt
        tracker.on_interrupt(
            thread_id="t1",
            node_name="approval_node",
            reason="needs_approval",
        )

        # Then resume
        result = tracker.on_resume(
            thread_id="t1",
            resume_input={"approved": True},
        )

        assert result.from_interrupt is True
        assert result.wait_duration_seconds is not None
        assert result.wait_duration_seconds >= 0

        # Interrupt should be cleared
        assert "t1" not in tracker._interrupted_traces

    def test_on_resume_with_span(self):
        """Test resume with span attributes."""
        from traceai_langchain._langgraph._interrupt_tracker import InterruptResumeTracker

        tracker = InterruptResumeTracker(self.mock_tracer)

        # Record interrupt with trace context
        mock_interrupt_span = MagicMock()
        mock_interrupt_span.is_recording.return_value = True
        mock_ctx = MagicMock()
        mock_ctx.trace_id = 11111
        mock_ctx.span_id = 22222
        mock_interrupt_span.get_span_context.return_value = mock_ctx

        tracker.on_interrupt(
            thread_id="t1",
            node_name="node1",
            reason="test",
            span=mock_interrupt_span,
        )

        # Resume with span
        mock_resume_span = MagicMock()
        mock_resume_span.is_recording.return_value = True

        result = tracker.on_resume(
            thread_id="t1",
            resume_input={"data": "test"},
            span=mock_resume_span,
        )

        # Verify resume attributes were set
        mock_resume_span.set_attribute.assert_called()
        mock_resume_span.add_event.assert_called()

    def test_record_human_decision(self):
        """Test recording a human decision."""
        from traceai_langchain._langgraph._interrupt_tracker import InterruptResumeTracker

        tracker = InterruptResumeTracker(self.mock_tracer)

        result = tracker.record_human_decision(
            decision="approved",
            thread_id="t1",
            approver_id="admin",
            feedback="Looks good",
        )

        assert result.decision == "approved"
        assert result.approver_id == "admin"
        assert result.feedback == "Looks good"
        assert len(tracker._human_decisions) == 1

    def test_record_human_decision_with_span(self):
        """Test recording human decision with span."""
        from traceai_langchain._langgraph._interrupt_tracker import InterruptResumeTracker

        tracker = InterruptResumeTracker(self.mock_tracer)

        mock_span = MagicMock()
        mock_span.is_recording.return_value = True

        tracker.record_human_decision(
            decision="rejected",
            thread_id="t1",
            approver_id="user123",
            metadata={"reason_code": "incomplete"},
            span=mock_span,
        )

        mock_span.set_attribute.assert_called()
        mock_span.add_event.assert_called()

    def test_get_pending_interrupts(self):
        """Test getting pending interrupts."""
        from traceai_langchain._langgraph._interrupt_tracker import InterruptResumeTracker

        tracker = InterruptResumeTracker(self.mock_tracer)

        tracker.on_interrupt("t1", "node1", "reason1")
        tracker.on_interrupt("t2", "node2", "reason2")

        pending = tracker.get_pending_interrupts()

        assert len(pending) == 2
        assert "t1" in pending
        assert "t2" in pending

    def test_get_interrupt_history(self):
        """Test getting interrupt history."""
        from traceai_langchain._langgraph._interrupt_tracker import InterruptResumeTracker

        tracker = InterruptResumeTracker(self.mock_tracer)

        tracker.on_interrupt("t1", "node1", "reason1")
        tracker.on_interrupt("t2", "node2", "reason2")

        history = tracker.get_interrupt_history()

        assert len(history) == 2
        assert history[0]["thread_id"] == "t1"
        assert history[1]["thread_id"] == "t2"

    def test_get_resume_history(self):
        """Test getting resume history."""
        from traceai_langchain._langgraph._interrupt_tracker import InterruptResumeTracker

        tracker = InterruptResumeTracker(self.mock_tracer)

        tracker.on_resume("t1")
        tracker.on_resume("t2", resume_input={"data": "x"})

        history = tracker.get_resume_history()

        assert len(history) == 2

    def test_get_human_decisions(self):
        """Test getting human decisions."""
        from traceai_langchain._langgraph._interrupt_tracker import InterruptResumeTracker

        tracker = InterruptResumeTracker(self.mock_tracer)

        tracker.record_human_decision("approved", "t1")
        tracker.record_human_decision("rejected", "t2")

        decisions = tracker.get_human_decisions()

        assert len(decisions) == 2
        assert decisions[0]["decision"] == "approved"
        assert decisions[1]["decision"] == "rejected"

    def test_get_stats(self):
        """Test getting statistics."""
        from traceai_langchain._langgraph._interrupt_tracker import InterruptResumeTracker

        tracker = InterruptResumeTracker(self.mock_tracer)

        # Record some activity
        tracker.on_interrupt("t1", "n1", "r1", is_intentional=True)
        tracker.on_interrupt("t2", "n2", "r2", is_intentional=False)
        tracker.on_resume("t1")
        tracker.record_human_decision("approved", "t1")
        tracker.record_human_decision("approved", "t2")
        tracker.record_human_decision("rejected", "t3")

        stats = tracker.get_stats()

        assert stats["total_interrupts"] == 2
        assert stats["intentional_interrupts"] == 1
        assert stats["error_interrupts"] == 1
        assert stats["pending_interrupts"] == 1  # t2 not resumed
        assert stats["total_resumes"] == 1
        assert stats["total_human_decisions"] == 3
        assert stats["decisions_by_type"]["approved"] == 2
        assert stats["decisions_by_type"]["rejected"] == 1

    def test_reset(self):
        """Test resetting history (keeps pending)."""
        from traceai_langchain._langgraph._interrupt_tracker import InterruptResumeTracker

        tracker = InterruptResumeTracker(self.mock_tracer)

        tracker.on_interrupt("t1", "n1", "r1")
        tracker.on_resume("t2")
        tracker.record_human_decision("approved", "t1")

        tracker.reset()

        assert len(tracker._interrupt_history) == 0
        assert len(tracker._resume_history) == 0
        assert len(tracker._human_decisions) == 0
        # Pending interrupts should remain
        assert len(tracker._interrupted_traces) == 1

    def test_reset_all(self):
        """Test resetting everything."""
        from traceai_langchain._langgraph._interrupt_tracker import InterruptResumeTracker

        tracker = InterruptResumeTracker(self.mock_tracer)

        tracker.on_interrupt("t1", "n1", "r1")

        tracker.reset_all()

        assert len(tracker._interrupted_traces) == 0
        assert len(tracker._interrupt_history) == 0
