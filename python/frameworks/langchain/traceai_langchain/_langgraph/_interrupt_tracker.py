"""Interrupt/Resume tracking for LangGraph Human-in-the-Loop workflows.

Provides unified tracing for interrupt-resume cycles, which is a critical gap
in competitor implementations (Langfuse has this broken, others unclear).

Key features:
- Unified trace linking between interrupt and resume
- Intentional interrupt vs error distinction
- Wait duration tracking
- Human decision recording
- State snapshot at interrupt point
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from opentelemetry import trace as trace_api
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, SpanContext, Link

from traceai_langchain._langgraph._attributes import LangGraphAttributes, LangGraphSpanKind
from traceai_langchain._langgraph._state_tracker import safe_json_dumps


class InterruptInfo:
    """Information about an interrupt event."""

    def __init__(
        self,
        thread_id: str,
        node_name: str,
        reason: str,
        is_intentional: bool = True,
    ):
        self.interrupt_id = str(uuid.uuid4())
        self.thread_id = thread_id
        self.node_name = node_name
        self.reason = reason
        self.is_intentional = is_intentional
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.state_snapshot: Optional[Dict[str, Any]] = None
        self.trace_id: Optional[int] = None
        self.span_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "interrupt_id": self.interrupt_id,
            "thread_id": self.thread_id,
            "node_name": self.node_name,
            "reason": self.reason,
            "is_intentional": self.is_intentional,
            "timestamp": self.timestamp,
            "has_state_snapshot": self.state_snapshot is not None,
        }


class ResumeInfo:
    """Information about a resume event."""

    def __init__(
        self,
        thread_id: str,
        resume_input: Optional[Dict[str, Any]] = None,
    ):
        self.resume_id = str(uuid.uuid4())
        self.thread_id = thread_id
        self.resume_input = resume_input
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.wait_duration_seconds: Optional[float] = None
        self.from_interrupt: bool = False
        self.interrupt_id: Optional[str] = None  # ID of the interrupt this resumes from
        self.previous_trace_id: Optional[int] = None
        self.previous_span_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "resume_id": self.resume_id,
            "thread_id": self.thread_id,
            "timestamp": self.timestamp,
            "wait_duration_seconds": self.wait_duration_seconds,
            "from_interrupt": self.from_interrupt,
            "interrupt_id": self.interrupt_id,
        }


class HumanDecision:
    """Information about a human decision in HITL workflow."""

    def __init__(
        self,
        decision: str,  # "approved", "rejected", "modified"
        thread_id: Optional[str] = None,
        approver_id: Optional[str] = None,
    ):
        self.decision = decision
        self.thread_id = thread_id
        self.approver_id = approver_id
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.metadata: Dict[str, Any] = {}
        self.feedback: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "decision": self.decision,
            "thread_id": self.thread_id,
            "approver_id": self.approver_id,
            "timestamp": self.timestamp,
            "feedback": self.feedback,
        }


class InterruptResumeTracker:
    """Track interrupt-resume cycles as unified traces.

    This addresses a critical gap in competitor implementations:
    - Langfuse creates separate traces instead of merging
    - Tools using interrupt() are marked as ERROR (wrong!)
    - Context is lost between interrupt and resume

    Our implementation:
    - Links resume spans to interrupt spans for unified trace view
    - Marks interrupts as intentional (StatusCode.OK, not ERROR)
    - Stores context for resumption
    - Tracks wait duration between interrupt and resume
    """

    def __init__(self, tracer: trace_api.Tracer):
        """Initialize the interrupt-resume tracker.

        Args:
            tracer: OpenTelemetry tracer
        """
        self._tracer = tracer
        self._interrupted_traces: Dict[str, InterruptInfo] = {}
        self._interrupt_history: List[InterruptInfo] = []
        self._resume_history: List[ResumeInfo] = []
        self._human_decisions: List[HumanDecision] = []

    def on_interrupt(
        self,
        thread_id: str,
        node_name: str,
        reason: str,
        state: Optional[Dict[str, Any]] = None,
        span: Optional[Span] = None,
        is_intentional: bool = True,
    ) -> InterruptInfo:
        """Record an interrupt event.

        This is called when interrupt() is invoked in a LangGraph workflow.
        Unlike competitor implementations, we:
        - Mark as intentional interrupt, NOT an error
        - Store trace context for proper resumption linking
        - Capture state snapshot for debugging

        Args:
            thread_id: Thread ID for the execution
            node_name: Name of the node where interrupt occurred
            reason: Reason for the interrupt
            state: State at the time of interrupt
            span: Current span to record attributes on
            is_intentional: Whether this is an intentional interrupt (default True)

        Returns:
            InterruptInfo with captured information
        """
        interrupt = InterruptInfo(
            thread_id=thread_id,
            node_name=node_name,
            reason=reason,
            is_intentional=is_intentional,
        )

        if state:
            interrupt.state_snapshot = dict(state)

        # Capture trace context for resumption linking
        if span and span.is_recording():
            ctx = span.get_span_context()
            interrupt.trace_id = ctx.trace_id
            interrupt.span_id = ctx.span_id

            # Set span attributes - CRITICAL: Use OK status, not ERROR!
            span.set_attribute(LangGraphAttributes.INTERRUPT_NODE, node_name)
            span.set_attribute(LangGraphAttributes.INTERRUPT_REASON, reason)
            span.set_attribute(LangGraphAttributes.INTERRUPT_IS_INTENTIONAL, is_intentional)
            span.set_attribute(LangGraphAttributes.INTERRUPT_TIMESTAMP, interrupt.timestamp)

            if state:
                span.set_attribute(
                    LangGraphAttributes.INTERRUPT_STATE_SNAPSHOT,
                    safe_json_dumps(state, max_length=5000)
                )

            # Add event for the interrupt
            span.add_event("interrupt", {
                "thread_id": thread_id,
                "node": node_name,
                "reason": reason,
                "is_intentional": is_intentional,
                "has_state": state is not None,
            })

            # CRITICAL: Set OK status for intentional interrupts
            if is_intentional:
                span.set_status(Status(StatusCode.OK, f"Intentional interrupt: {reason}"))
        else:
            # Get current span if not provided
            current_span = trace_api.get_current_span()
            if current_span and current_span.is_recording():
                ctx = current_span.get_span_context()
                interrupt.trace_id = ctx.trace_id
                interrupt.span_id = ctx.span_id

        # Store for resumption
        self._interrupted_traces[thread_id] = interrupt
        self._interrupt_history.append(interrupt)

        return interrupt

    def on_resume(
        self,
        thread_id: str,
        resume_input: Optional[Dict[str, Any]] = None,
        span: Optional[Span] = None,
    ) -> ResumeInfo:
        """Record a resume event.

        Called when execution resumes from an interrupt.
        Links the new span to the previous interrupt span for unified tracing.

        Args:
            thread_id: Thread ID for the execution
            resume_input: Input provided for resumption
            span: Current span to record attributes on

        Returns:
            ResumeInfo with captured information
        """
        resume = ResumeInfo(
            thread_id=thread_id,
            resume_input=resume_input,
        )

        # Check if we have a previous interrupt for this thread
        if thread_id in self._interrupted_traces:
            prev_interrupt = self._interrupted_traces[thread_id]
            resume.from_interrupt = True
            resume.interrupt_id = prev_interrupt.interrupt_id
            resume.previous_trace_id = prev_interrupt.trace_id
            resume.previous_span_id = prev_interrupt.span_id

            # Calculate wait duration
            interrupt_time = datetime.fromisoformat(prev_interrupt.timestamp)
            resume_time = datetime.fromisoformat(resume.timestamp)
            resume.wait_duration_seconds = (resume_time - interrupt_time).total_seconds()

            if span and span.is_recording():
                # Set resume attributes
                span.set_attribute(LangGraphAttributes.RESUME_FROM_INTERRUPT, True)
                span.set_attribute(
                    LangGraphAttributes.RESUME_WAIT_DURATION_SECONDS,
                    resume.wait_duration_seconds
                )

                if resume_input:
                    span.set_attribute(
                        LangGraphAttributes.RESUME_INPUT,
                        safe_json_dumps(resume_input, max_length=5000)
                    )

                # Link to previous trace for unified view
                if prev_interrupt.trace_id and prev_interrupt.span_id:
                    span.set_attribute(
                        LangGraphAttributes.RESUME_PREVIOUS_TRACE_ID,
                        format(prev_interrupt.trace_id, '032x')
                    )
                    span.set_attribute(
                        LangGraphAttributes.RESUME_PREVIOUS_SPAN_ID,
                        format(prev_interrupt.span_id, '016x')
                    )

                span.add_event("resume", {
                    "thread_id": thread_id,
                    "from_interrupt": True,
                    "wait_duration_seconds": resume.wait_duration_seconds,
                    "previous_node": prev_interrupt.node_name,
                    "interrupt_reason": prev_interrupt.reason,
                })

            # Clean up stored interrupt
            del self._interrupted_traces[thread_id]
        else:
            # Resume without previous interrupt (first start or timeout)
            if span and span.is_recording():
                span.set_attribute(LangGraphAttributes.RESUME_FROM_INTERRUPT, False)

                span.add_event("resume", {
                    "thread_id": thread_id,
                    "from_interrupt": False,
                })

        self._resume_history.append(resume)
        return resume

    def record_human_decision(
        self,
        decision: str,
        thread_id: Optional[str] = None,
        approver_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        feedback: Optional[str] = None,
        span: Optional[Span] = None,
    ) -> HumanDecision:
        """Record a human approval/rejection decision.

        Args:
            decision: The decision ("approved", "rejected", "modified")
            thread_id: Thread ID for the execution
            approver_id: ID of the human approver
            metadata: Additional metadata
            feedback: Human feedback text
            span: Current span to record attributes on

        Returns:
            HumanDecision with captured information
        """
        human_decision = HumanDecision(
            decision=decision,
            thread_id=thread_id,
            approver_id=approver_id,
        )
        human_decision.feedback = feedback
        if metadata:
            human_decision.metadata = dict(metadata)

        if span and span.is_recording():
            span.set_attribute(LangGraphAttributes.HUMAN_DECISION, decision)

            if approver_id:
                span.set_attribute(LangGraphAttributes.HUMAN_APPROVER_ID, approver_id)

            if metadata:
                span.set_attribute(
                    LangGraphAttributes.HUMAN_METADATA,
                    safe_json_dumps(metadata, max_length=2000)
                )

            if feedback:
                span.set_attribute(LangGraphAttributes.HUMAN_FEEDBACK, feedback)

            span.set_attribute(LangGraphAttributes.HUMAN_TIMESTAMP, human_decision.timestamp)

            span.add_event("human_decision", {
                "decision": decision,
                "approver_id": approver_id,
                "thread_id": thread_id,
            })

        self._human_decisions.append(human_decision)
        return human_decision

    def get_pending_interrupts(self) -> Dict[str, InterruptInfo]:
        """Get all pending (unresolved) interrupts.

        Returns:
            Dictionary mapping thread_id to InterruptInfo
        """
        return dict(self._interrupted_traces)

    def get_interrupt_history(self) -> List[Dict[str, Any]]:
        """Get the interrupt event history.

        Returns:
            List of interrupt dictionaries
        """
        return [i.to_dict() for i in self._interrupt_history]

    def get_resume_history(self) -> List[Dict[str, Any]]:
        """Get the resume event history.

        Returns:
            List of resume dictionaries
        """
        return [r.to_dict() for r in self._resume_history]

    def get_human_decisions(self) -> List[Dict[str, Any]]:
        """Get the human decision history.

        Returns:
            List of human decision dictionaries
        """
        return [d.to_dict() for d in self._human_decisions]

    def get_stats(self) -> Dict[str, Any]:
        """Get interrupt/resume statistics.

        Returns:
            Dictionary with statistics
        """
        wait_durations = [
            r.wait_duration_seconds
            for r in self._resume_history
            if r.wait_duration_seconds is not None
        ]

        intentional_count = sum(1 for i in self._interrupt_history if i.is_intentional)
        error_count = len(self._interrupt_history) - intentional_count

        decision_counts = {}
        for d in self._human_decisions:
            decision_counts[d.decision] = decision_counts.get(d.decision, 0) + 1

        return {
            "total_interrupts": len(self._interrupt_history),
            "intentional_interrupts": intentional_count,
            "error_interrupts": error_count,
            "pending_interrupts": len(self._interrupted_traces),
            "total_resumes": len(self._resume_history),
            "avg_wait_duration_seconds": sum(wait_durations) / len(wait_durations) if wait_durations else 0,
            "max_wait_duration_seconds": max(wait_durations) if wait_durations else 0,
            "total_human_decisions": len(self._human_decisions),
            "decisions_by_type": decision_counts,
        }

    def reset(self) -> None:
        """Reset the tracker (clears history but keeps pending interrupts)."""
        self._interrupt_history.clear()
        self._resume_history.clear()
        self._human_decisions.clear()

    def reset_all(self) -> None:
        """Reset everything including pending interrupts."""
        self.reset()
        self._interrupted_traces.clear()
