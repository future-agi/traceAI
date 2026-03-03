"""Superstep tracking for LangGraph.

Tracks superstep-based execution model of LangGraph's Pregel engine,
grouping node executions by their execution round.
"""

import time
from typing import Any, Dict, List, Optional, Set
from datetime import datetime, timezone

from opentelemetry import trace as trace_api
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from traceai_langchain._langgraph._attributes import LangGraphAttributes, LangGraphSpanKind
from traceai_langchain._langgraph._state_tracker import safe_json_dumps


class SuperstepInfo:
    """Information about a single superstep execution."""

    def __init__(self, superstep_number: int):
        self.superstep_number = superstep_number
        self.nodes_executed: List[str] = []
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.state_before: Optional[Dict[str, Any]] = None
        self.state_after: Optional[Dict[str, Any]] = None
        self.errors: List[Dict[str, Any]] = []

    @property
    def duration_ms(self) -> Optional[float]:
        """Calculate duration in milliseconds."""
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time) * 1000
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "superstep_number": self.superstep_number,
            "nodes_executed": self.nodes_executed,
            "duration_ms": self.duration_ms,
            "error_count": len(self.errors),
        }


class SuperstepTracker:
    """Track superstep execution in LangGraph.

    LangGraph uses a Pregel-like execution model where:
    - Nodes execute in parallel within a superstep
    - Supersteps execute sequentially
    - State is synchronized between supersteps
    """

    def __init__(self, tracer: trace_api.Tracer):
        """Initialize the superstep tracker.

        Args:
            tracer: OpenTelemetry tracer
        """
        self._tracer = tracer
        self._superstep_count = 0
        self._current_superstep: Optional[SuperstepInfo] = None
        self._superstep_history: List[SuperstepInfo] = []
        self._active_span: Optional[Span] = None

    def start_superstep(self, state: Optional[Dict[str, Any]] = None) -> SuperstepInfo:
        """Start a new superstep.

        Args:
            state: The state at the beginning of the superstep

        Returns:
            SuperstepInfo for the new superstep
        """
        self._superstep_count += 1

        superstep = SuperstepInfo(self._superstep_count)
        superstep.start_time = time.perf_counter()
        superstep.state_before = dict(state) if state else {}

        self._current_superstep = superstep

        # Start span for superstep
        self._active_span = self._tracer.start_span(
            f"langgraph.superstep.{self._superstep_count}",
            kind=SpanKind.INTERNAL,
        )
        self._active_span.set_attribute("gen_ai.span.kind", LangGraphSpanKind.SUPERSTEP)
        self._active_span.set_attribute(
            LangGraphAttributes.EXECUTION_SUPERSTEP,
            self._superstep_count
        )

        if state:
            self._active_span.set_attribute(
                LangGraphAttributes.STATE_INPUT,
                safe_json_dumps(state, max_length=5000)
            )

        return superstep

    def record_node_execution(self, node_name: str) -> None:
        """Record that a node executed in the current superstep.

        Args:
            node_name: Name of the node that executed
        """
        if self._current_superstep:
            self._current_superstep.nodes_executed.append(node_name)

            if self._active_span:
                self._active_span.add_event("node_executed", {
                    "node_name": node_name,
                    "order": len(self._current_superstep.nodes_executed),
                })

    def record_error(self, node_name: str, error: Exception) -> None:
        """Record an error that occurred during the superstep.

        Args:
            node_name: Name of the node where error occurred
            error: The exception that was raised
        """
        if self._current_superstep:
            error_info = {
                "node": node_name,
                "error_type": type(error).__name__,
                "error_message": str(error),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self._current_superstep.errors.append(error_info)

            if self._active_span:
                self._active_span.add_event("node_error", error_info)

    def end_superstep(self, state: Optional[Dict[str, Any]] = None) -> Optional[SuperstepInfo]:
        """End the current superstep.

        Args:
            state: The state at the end of the superstep

        Returns:
            The completed SuperstepInfo, or None if no superstep was active
        """
        if not self._current_superstep:
            return None

        superstep = self._current_superstep
        superstep.end_time = time.perf_counter()
        superstep.state_after = dict(state) if state else {}

        # Update span
        if self._active_span:
            self._active_span.set_attribute(
                "langgraph.superstep.nodes_executed",
                superstep.nodes_executed
            )
            self._active_span.set_attribute(
                "langgraph.superstep.node_count",
                len(superstep.nodes_executed)
            )

            if superstep.duration_ms:
                self._active_span.set_attribute(
                    LangGraphAttributes.PERF_DURATION_MS,
                    superstep.duration_ms
                )

            if state:
                self._active_span.set_attribute(
                    LangGraphAttributes.STATE_OUTPUT,
                    safe_json_dumps(state, max_length=5000)
                )

            # Set status based on errors
            if superstep.errors:
                self._active_span.set_status(
                    Status(StatusCode.ERROR, f"{len(superstep.errors)} errors in superstep")
                )
            else:
                self._active_span.set_status(Status(StatusCode.OK))

            self._active_span.end()
            self._active_span = None

        # Store in history
        self._superstep_history.append(superstep)
        self._current_superstep = None

        return superstep

    def get_current_superstep(self) -> Optional[SuperstepInfo]:
        """Get the current active superstep.

        Returns:
            Current SuperstepInfo or None
        """
        return self._current_superstep

    def get_superstep_count(self) -> int:
        """Get the total number of supersteps executed.

        Returns:
            Total superstep count
        """
        return self._superstep_count

    def get_history(self) -> List[Dict[str, Any]]:
        """Get the superstep execution history.

        Returns:
            List of superstep information dictionaries
        """
        return [s.to_dict() for s in self._superstep_history]

    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about superstep execution.

        Returns:
            Dictionary with execution statistics
        """
        if not self._superstep_history:
            return {"error": "No superstep data available"}

        durations = [s.duration_ms for s in self._superstep_history if s.duration_ms]
        node_counts = [len(s.nodes_executed) for s in self._superstep_history]
        error_counts = [len(s.errors) for s in self._superstep_history]

        return {
            "total_supersteps": len(self._superstep_history),
            "total_duration_ms": sum(durations) if durations else 0,
            "avg_duration_ms": sum(durations) / len(durations) if durations else 0,
            "total_nodes_executed": sum(node_counts),
            "avg_nodes_per_superstep": sum(node_counts) / len(node_counts) if node_counts else 0,
            "total_errors": sum(error_counts),
            "supersteps_with_errors": sum(1 for c in error_counts if c > 0),
        }

    def reset(self) -> None:
        """Reset the tracker state."""
        if self._active_span:
            self._active_span.end()
            self._active_span = None

        self._superstep_count = 0
        self._current_superstep = None
        self._superstep_history.clear()


class ReducerTracker:
    """Track state reducer function attribution.

    Identifies which reducer function modified which state field,
    providing attribution for state changes.
    """

    def __init__(self):
        """Initialize the reducer tracker."""
        self._reducer_map: Dict[str, str] = {}  # field -> reducer_name
        self._attribution_history: List[Dict[str, Any]] = []

    def register_reducer(self, field_name: str, reducer_name: str) -> None:
        """Register a reducer for a state field.

        Args:
            field_name: Name of the state field
            reducer_name: Name of the reducer function
        """
        self._reducer_map[field_name] = reducer_name

    def attribute_change(
        self,
        field_name: str,
        old_value: Any,
        new_value: Any,
        node_name: str,
        span: Optional[Span] = None,
    ) -> Optional[str]:
        """Attribute a state change to a reducer.

        Args:
            field_name: Name of the changed field
            old_value: Previous value
            new_value: New value
            node_name: Node that caused the change
            span: Optional span to record attribution on

        Returns:
            Name of the attributed reducer, or None
        """
        reducer_name = self._reducer_map.get(field_name)

        attribution = {
            "field": field_name,
            "reducer": reducer_name,
            "node": node_name,
            "old_value_type": type(old_value).__name__,
            "new_value_type": type(new_value).__name__,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._attribution_history.append(attribution)

        if span and span.is_recording():
            if reducer_name:
                span.set_attribute(LangGraphAttributes.STATE_REDUCER, reducer_name)

            span.add_event("reducer_attribution", {
                "field": field_name,
                "reducer": reducer_name or "unknown",
                "node": node_name,
            })

        return reducer_name

    def get_field_reducer(self, field_name: str) -> Optional[str]:
        """Get the reducer registered for a field.

        Args:
            field_name: Name of the state field

        Returns:
            Reducer name or None
        """
        return self._reducer_map.get(field_name)

    def get_attribution_history(self) -> List[Dict[str, Any]]:
        """Get the attribution history.

        Returns:
            List of attribution records
        """
        return list(self._attribution_history)

    def reset(self) -> None:
        """Reset the tracker (keeps reducer map)."""
        self._attribution_history.clear()
