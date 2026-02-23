"""State transition tracking for LangGraph.

Tracks state changes through graph execution, including:
- Before/after state snapshots
- Field-level diff tracking
- Reducer attribution
- Memory growth detection
"""

import json
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from opentelemetry.trace import Span

from traceai_langchain._langgraph._attributes import LangGraphAttributes


def safe_json_dumps(obj: Any, max_length: int = 10000) -> str:
    """Safely serialize object to JSON string with truncation."""
    try:
        result = json.dumps(obj, default=str, ensure_ascii=False)
        if len(result) > max_length:
            return result[:max_length] + "...[truncated]"
        return result
    except Exception:
        return str(obj)[:max_length]


def get_object_size(obj: Any) -> int:
    """Get approximate size of an object in bytes."""
    try:
        return sys.getsizeof(obj)
    except Exception:
        return 0


def deep_diff(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """Calculate deep differences between two state dictionaries.

    Returns a dict with:
    - 'added': fields that were added
    - 'removed': fields that were removed
    - 'changed': fields that changed (with before/after values)
    """
    diff = {
        "added": {},
        "removed": {},
        "changed": {},
    }

    before_keys = set(before.keys()) if before else set()
    after_keys = set(after.keys()) if after else set()

    # Added fields
    for key in after_keys - before_keys:
        diff["added"][key] = after[key]

    # Removed fields
    for key in before_keys - after_keys:
        diff["removed"][key] = before[key]

    # Changed fields
    for key in before_keys & after_keys:
        before_val = before.get(key)
        after_val = after.get(key)

        # Deep comparison for nested structures
        if before_val != after_val:
            diff["changed"][key] = {
                "before": before_val,
                "after": after_val,
            }

    # Remove empty sections
    return {k: v for k, v in diff.items() if v}


class StateTransitionTracker:
    """Track state changes through graph execution."""

    def __init__(self, max_history: int = 100, enable_memory_tracking: bool = True):
        """Initialize the state tracker.

        Args:
            max_history: Maximum number of state transitions to keep in history
            enable_memory_tracking: Whether to track memory usage
        """
        self._state_history: List[Dict[str, Any]] = []
        self._max_history = max_history
        self._enable_memory_tracking = enable_memory_tracking
        self._memory_snapshots: List[int] = []

    def record_transition(
        self,
        node_name: str,
        before_state: Optional[Dict[str, Any]],
        after_state: Optional[Dict[str, Any]],
        span: Span,
        reducer_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record a state transition.

        Args:
            node_name: Name of the node that caused the transition
            before_state: State before node execution
            after_state: State after node execution
            span: OpenTelemetry span to record attributes on
            reducer_name: Optional name of the reducer function

        Returns:
            Dictionary containing the diff information
        """
        before = before_state or {}
        after = after_state or {}

        # Calculate diff
        diff = deep_diff(before, after)

        # Get changed field names
        changed_fields = list(diff.get("changed", {}).keys())
        added_fields = list(diff.get("added", {}).keys())
        removed_fields = list(diff.get("removed", {}).keys())
        all_changed = changed_fields + added_fields + removed_fields

        # Set span attributes
        if all_changed:
            span.set_attribute(
                LangGraphAttributes.STATE_CHANGED_FIELDS,
                all_changed
            )

        if diff:
            span.set_attribute(
                LangGraphAttributes.STATE_DIFF,
                safe_json_dumps(diff)
            )

        if reducer_name:
            span.set_attribute(
                LangGraphAttributes.STATE_REDUCER,
                reducer_name
            )

        # Add event for significant changes
        if diff:
            span.add_event("state_transition", {
                "node": node_name,
                "changed_fields": all_changed,
                "added_count": len(added_fields),
                "removed_count": len(removed_fields),
                "changed_count": len(changed_fields),
            })

        # Track memory usage
        if self._enable_memory_tracking:
            state_size = get_object_size(after)
            self._memory_snapshots.append(state_size)

            span.set_attribute(
                LangGraphAttributes.MEMORY_STATE_SIZE_BYTES,
                state_size
            )

            # Check for memory growth
            self._check_memory_growth(span)

        # Store in history
        transition_record = {
            "node": node_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "before": before,
            "after": after,
            "diff": diff,
            "reducer": reducer_name,
        }

        self._state_history.append(transition_record)

        # Trim history if needed
        if len(self._state_history) > self._max_history:
            self._state_history = self._state_history[-self._max_history:]

        return diff

    def _check_memory_growth(self, span: Span) -> None:
        """Check for consistent memory growth (potential leak).

        Emits a warning if state size has grown consistently over
        the last 5 transitions.
        """
        if len(self._memory_snapshots) < 5:
            return

        recent = self._memory_snapshots[-5:]

        # Check if all values are increasing
        is_growing = all(
            recent[i] < recent[i + 1]
            for i in range(len(recent) - 1)
        )

        if is_growing:
            growth_rate = (recent[-1] - recent[0]) / recent[0] * 100 if recent[0] > 0 else 0

            span.set_attribute(
                LangGraphAttributes.MEMORY_GROWTH_WARNING,
                True
            )

            span.add_event("memory_growth_warning", {
                "message": "State size growing consistently - potential memory leak",
                "recent_sizes_bytes": recent,
                "growth_rate_percent": round(growth_rate, 2),
            })

    def get_state_at_node(self, node_name: str) -> Optional[Dict[str, Any]]:
        """Get the state after a specific node executed.

        Args:
            node_name: Name of the node

        Returns:
            State after the node executed, or None if not found
        """
        for record in reversed(self._state_history):
            if record["node"] == node_name:
                return record["after"]
        return None

    def get_history(self) -> List[Dict[str, Any]]:
        """Get the full state transition history."""
        return list(self._state_history)

    def get_field_history(self, field_name: str) -> List[Dict[str, Any]]:
        """Get history of changes to a specific field.

        Args:
            field_name: Name of the state field to track

        Returns:
            List of transitions that affected this field
        """
        field_changes = []

        for record in self._state_history:
            diff = record.get("diff", {})

            # Check if field was changed, added, or removed
            if (field_name in diff.get("changed", {}) or
                field_name in diff.get("added", {}) or
                field_name in diff.get("removed", {})):
                field_changes.append({
                    "node": record["node"],
                    "timestamp": record["timestamp"],
                    "before": record["before"].get(field_name),
                    "after": record["after"].get(field_name),
                })

        return field_changes

    def get_memory_stats(self) -> Dict[str, Any]:
        """Get memory usage statistics.

        Returns:
            Dictionary with memory statistics
        """
        if not self._memory_snapshots:
            return {"error": "No memory data available"}

        return {
            "current_bytes": self._memory_snapshots[-1],
            "peak_bytes": max(self._memory_snapshots),
            "min_bytes": min(self._memory_snapshots),
            "avg_bytes": sum(self._memory_snapshots) / len(self._memory_snapshots),
            "sample_count": len(self._memory_snapshots),
        }

    def reset(self) -> None:
        """Reset the tracker state."""
        self._state_history.clear()
        self._memory_snapshots.clear()
