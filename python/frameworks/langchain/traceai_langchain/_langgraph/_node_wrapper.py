"""Node function wrapping for LangGraph.

Wraps node functions to trace execution, state changes, and performance.
"""

import functools
import time
from typing import Any, Callable, Dict, Optional, TypeVar, Union

from opentelemetry import trace as trace_api
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from traceai_langchain._langgraph._attributes import LangGraphAttributes, LangGraphSpanKind
from traceai_langchain._langgraph._state_tracker import (
    StateTransitionTracker,
    safe_json_dumps,
    get_object_size,
)


T = TypeVar("T")


class NodeWrapper:
    """Wraps LangGraph node functions for tracing."""

    def __init__(
        self,
        tracer: trace_api.Tracer,
        state_tracker: StateTransitionTracker,
        graph_name: Optional[str] = None,
    ):
        """Initialize the node wrapper.

        Args:
            tracer: OpenTelemetry tracer
            state_tracker: State transition tracker instance
            graph_name: Optional name of the parent graph
        """
        self._tracer = tracer
        self._state_tracker = state_tracker
        self._graph_name = graph_name
        self._node_execution_counts: Dict[str, int] = {}

    def wrap_node(
        self,
        node_name: str,
        node_func: Callable,
        is_entry: bool = False,
        is_end: bool = False,
        node_type: str = "intermediate",
    ) -> Callable:
        """Wrap a node function with tracing.

        Args:
            node_name: Name of the node
            node_func: The node function to wrap
            is_entry: Whether this is the entry point node
            is_end: Whether this is an end node
            node_type: Type of node (start/end/intermediate/subgraph)

        Returns:
            Wrapped function with tracing
        """
        @functools.wraps(node_func)
        def wrapped(state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Any:
            return self._execute_node(
                node_name=node_name,
                node_func=node_func,
                state=state,
                config=config,
                is_entry=is_entry,
                is_end=is_end,
                node_type=node_type,
            )

        # Mark as wrapped to avoid double-wrapping
        wrapped._langgraph_wrapped = True
        wrapped._original_func = node_func

        return wrapped

    def _execute_node(
        self,
        node_name: str,
        node_func: Callable,
        state: Dict[str, Any],
        config: Optional[Dict[str, Any]],
        is_entry: bool,
        is_end: bool,
        node_type: str,
    ) -> Any:
        """Execute a node with full tracing.

        Args:
            node_name: Name of the node
            node_func: The node function
            state: Current graph state
            config: Optional config dict
            is_entry: Whether this is entry node
            is_end: Whether this is end node
            node_type: Type of node

        Returns:
            Node function result (state updates)
        """
        # Track execution count
        if node_name not in self._node_execution_counts:
            self._node_execution_counts[node_name] = 0
        self._node_execution_counts[node_name] += 1
        execution_num = self._node_execution_counts[node_name]

        # Create span name
        span_name = f"langgraph.node.{node_name}"
        if execution_num > 1:
            span_name = f"{span_name}[{execution_num}]"

        # Capture state before execution
        state_before = dict(state) if state else {}

        with self._tracer.start_as_current_span(
            span_name,
            kind=SpanKind.INTERNAL,
        ) as span:
            start_time = time.perf_counter()

            try:
                # Set node attributes
                self._set_node_attributes(
                    span=span,
                    node_name=node_name,
                    node_type=node_type,
                    is_entry=is_entry,
                    is_end=is_end,
                    execution_num=execution_num,
                )

                # Set input state
                span.set_attribute(
                    LangGraphAttributes.STATE_INPUT,
                    safe_json_dumps(state_before, max_length=5000)
                )

                # Execute the node
                if config is not None:
                    result = node_func(state, config)
                else:
                    # Try with just state first
                    try:
                        result = node_func(state)
                    except TypeError:
                        # Some nodes might require config
                        result = node_func(state, {})

                # Calculate state after
                if result is not None:
                    if isinstance(result, dict):
                        state_after = {**state_before, **result}
                    else:
                        state_after = state_before
                else:
                    state_after = state_before

                # Track state transition
                diff = self._state_tracker.record_transition(
                    node_name=node_name,
                    before_state=state_before,
                    after_state=state_after,
                    span=span,
                )

                # Set output
                if result is not None:
                    span.set_attribute(
                        LangGraphAttributes.STATE_UPDATES,
                        safe_json_dumps(result, max_length=5000)
                    )

                # Set performance metrics
                end_time = time.perf_counter()
                duration_ms = (end_time - start_time) * 1000

                span.set_attribute(LangGraphAttributes.PERF_DURATION_MS, duration_ms)
                span.set_attribute(LangGraphAttributes.PERF_NODE, node_name)

                # Set success status
                span.set_status(Status(StatusCode.OK))

                return result

            except Exception as e:
                # Record error
                end_time = time.perf_counter()
                duration_ms = (end_time - start_time) * 1000

                span.set_attribute(LangGraphAttributes.PERF_DURATION_MS, duration_ms)
                span.set_attribute(LangGraphAttributes.ERROR_NODE, node_name)

                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)

                raise

    def _set_node_attributes(
        self,
        span: Span,
        node_name: str,
        node_type: str,
        is_entry: bool,
        is_end: bool,
        execution_num: int,
    ) -> None:
        """Set standard node attributes on span.

        Args:
            span: The span to set attributes on
            node_name: Name of the node
            node_type: Type of node
            is_entry: Whether this is entry node
            is_end: Whether this is end node
            execution_num: Execution count for this node
        """
        span.set_attribute("gen_ai.span.kind", LangGraphSpanKind.NODE)
        span.set_attribute(LangGraphAttributes.NODE_NAME, node_name)
        span.set_attribute(LangGraphAttributes.NODE_TYPE, node_type)
        span.set_attribute(LangGraphAttributes.NODE_IS_ENTRY, is_entry)
        span.set_attribute(LangGraphAttributes.NODE_IS_END, is_end)

        if self._graph_name:
            span.set_attribute(LangGraphAttributes.GRAPH_NAME, self._graph_name)

    def get_execution_counts(self) -> Dict[str, int]:
        """Get execution counts for all nodes.

        Returns:
            Dictionary mapping node names to execution counts
        """
        return dict(self._node_execution_counts)

    def reset_counts(self) -> None:
        """Reset execution counts."""
        self._node_execution_counts.clear()


class AsyncNodeWrapper(NodeWrapper):
    """Wraps async LangGraph node functions for tracing."""

    def wrap_node(
        self,
        node_name: str,
        node_func: Callable,
        is_entry: bool = False,
        is_end: bool = False,
        node_type: str = "intermediate",
    ) -> Callable:
        """Wrap an async node function with tracing.

        Args:
            node_name: Name of the node
            node_func: The async node function to wrap
            is_entry: Whether this is the entry point node
            is_end: Whether this is an end node
            node_type: Type of node

        Returns:
            Wrapped async function with tracing
        """
        @functools.wraps(node_func)
        async def wrapped(state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Any:
            return await self._execute_node_async(
                node_name=node_name,
                node_func=node_func,
                state=state,
                config=config,
                is_entry=is_entry,
                is_end=is_end,
                node_type=node_type,
            )

        wrapped._langgraph_wrapped = True
        wrapped._original_func = node_func

        return wrapped

    async def _execute_node_async(
        self,
        node_name: str,
        node_func: Callable,
        state: Dict[str, Any],
        config: Optional[Dict[str, Any]],
        is_entry: bool,
        is_end: bool,
        node_type: str,
    ) -> Any:
        """Execute an async node with full tracing."""
        if node_name not in self._node_execution_counts:
            self._node_execution_counts[node_name] = 0
        self._node_execution_counts[node_name] += 1
        execution_num = self._node_execution_counts[node_name]

        span_name = f"langgraph.node.{node_name}"
        if execution_num > 1:
            span_name = f"{span_name}[{execution_num}]"

        state_before = dict(state) if state else {}

        with self._tracer.start_as_current_span(
            span_name,
            kind=SpanKind.INTERNAL,
        ) as span:
            start_time = time.perf_counter()

            try:
                self._set_node_attributes(
                    span=span,
                    node_name=node_name,
                    node_type=node_type,
                    is_entry=is_entry,
                    is_end=is_end,
                    execution_num=execution_num,
                )

                span.set_attribute(
                    LangGraphAttributes.STATE_INPUT,
                    safe_json_dumps(state_before, max_length=5000)
                )

                # Execute async node
                if config is not None:
                    result = await node_func(state, config)
                else:
                    try:
                        result = await node_func(state)
                    except TypeError:
                        result = await node_func(state, {})

                # Calculate state after
                if result is not None:
                    if isinstance(result, dict):
                        state_after = {**state_before, **result}
                    else:
                        state_after = state_before
                else:
                    state_after = state_before

                # Track state transition
                self._state_tracker.record_transition(
                    node_name=node_name,
                    before_state=state_before,
                    after_state=state_after,
                    span=span,
                )

                if result is not None:
                    span.set_attribute(
                        LangGraphAttributes.STATE_UPDATES,
                        safe_json_dumps(result, max_length=5000)
                    )

                end_time = time.perf_counter()
                duration_ms = (end_time - start_time) * 1000

                span.set_attribute(LangGraphAttributes.PERF_DURATION_MS, duration_ms)
                span.set_attribute(LangGraphAttributes.PERF_NODE, node_name)
                span.set_status(Status(StatusCode.OK))

                return result

            except Exception as e:
                end_time = time.perf_counter()
                duration_ms = (end_time - start_time) * 1000

                span.set_attribute(LangGraphAttributes.PERF_DURATION_MS, duration_ms)
                span.set_attribute(LangGraphAttributes.ERROR_NODE, node_name)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)

                raise
