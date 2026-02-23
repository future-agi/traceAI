"""Graph wrapping for LangGraph.

Wraps StateGraph and CompiledGraph to capture:
- Graph topology (nodes, edges, entry points)
- Graph compilation
- Graph execution (invoke, stream)
- Conditional edge decisions
"""

import functools
import time
from typing import Any, Callable, Dict, Generator, Iterator, List, Optional, Set, Tuple, Union

from opentelemetry import trace as trace_api
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from traceai_langchain._langgraph._attributes import LangGraphAttributes, LangGraphSpanKind
from traceai_langchain._langgraph._state_tracker import (
    StateTransitionTracker,
    safe_json_dumps,
)
from traceai_langchain._langgraph._node_wrapper import NodeWrapper, AsyncNodeWrapper


class GraphTopology:
    """Captures and stores graph topology information."""

    def __init__(self):
        self.nodes: List[str] = []
        self.edges: List[Tuple[str, str]] = []
        self.conditional_edges: List[Dict[str, Any]] = []
        self.entry_point: Optional[str] = None
        self.end_nodes: Set[str] = set()

    def to_dict(self) -> Dict[str, Any]:
        """Convert topology to dictionary."""
        return {
            "nodes": self.nodes,
            "edges": self.edges,
            "conditional_edges": self.conditional_edges,
            "entry_point": self.entry_point,
            "end_nodes": list(self.end_nodes),
        }

    def to_json(self) -> str:
        """Convert topology to JSON string."""
        return safe_json_dumps(self.to_dict())


class ConditionalEdgeTracker:
    """Tracks conditional edge decisions."""

    def __init__(self, tracer: trace_api.Tracer):
        self._tracer = tracer
        self._decisions: List[Dict[str, Any]] = []

    def wrap_condition(
        self,
        source_node: str,
        condition_func: Callable,
        branch_mapping: Dict[str, str],
    ) -> Callable:
        """Wrap a conditional edge function to track decisions.

        Args:
            source_node: The node that the conditional edge originates from
            condition_func: The condition function that determines the branch
            branch_mapping: Mapping from condition results to target nodes

        Returns:
            Wrapped condition function
        """
        @functools.wraps(condition_func)
        def wrapped(state: Dict[str, Any]) -> str:
            # Get current span
            current_span = trace_api.get_current_span()

            try:
                # Execute condition
                result = condition_func(state)

                # Record the decision
                decision = {
                    "source_node": source_node,
                    "result": result,
                    "target_node": branch_mapping.get(result, result),
                    "available_branches": list(branch_mapping.keys()),
                }
                self._decisions.append(decision)

                # Add attributes to current span if available
                if current_span and current_span.is_recording():
                    current_span.set_attribute(
                        LangGraphAttributes.CONDITIONAL_SOURCE,
                        source_node
                    )
                    current_span.set_attribute(
                        LangGraphAttributes.CONDITIONAL_RESULT,
                        str(result)
                    )
                    current_span.set_attribute(
                        LangGraphAttributes.CONDITIONAL_AVAILABLE_BRANCHES,
                        list(branch_mapping.keys())
                    )

                    current_span.add_event("conditional_edge_decision", {
                        "source": source_node,
                        "selected_branch": str(result),
                        "target_node": branch_mapping.get(result, str(result)),
                        "available_branches": list(branch_mapping.keys()),
                    })

                return result

            except Exception as e:
                if current_span and current_span.is_recording():
                    current_span.add_event("conditional_edge_error", {
                        "source": source_node,
                        "error": str(e),
                    })
                raise

        return wrapped

    def get_decisions(self) -> List[Dict[str, Any]]:
        """Get all recorded decisions."""
        return list(self._decisions)

    def reset(self) -> None:
        """Reset recorded decisions."""
        self._decisions.clear()


class GraphWrapper:
    """Wraps LangGraph StateGraph and CompiledGraph for tracing."""

    def __init__(
        self,
        tracer: trace_api.Tracer,
        state_tracker: StateTransitionTracker,
    ):
        """Initialize the graph wrapper.

        Args:
            tracer: OpenTelemetry tracer
            state_tracker: State transition tracker
        """
        self._tracer = tracer
        self._state_tracker = state_tracker
        self._node_wrapper = NodeWrapper(tracer, state_tracker)
        self._async_node_wrapper = AsyncNodeWrapper(tracer, state_tracker)
        self._conditional_tracker = ConditionalEdgeTracker(tracer)
        self._topology: Optional[GraphTopology] = None
        self._superstep_count = 0

    def capture_topology(self, graph: Any) -> GraphTopology:
        """Capture the topology of a StateGraph.

        Args:
            graph: The StateGraph instance

        Returns:
            GraphTopology with captured information
        """
        topology = GraphTopology()

        # Capture nodes
        if hasattr(graph, 'nodes'):
            if isinstance(graph.nodes, dict):
                topology.nodes = list(graph.nodes.keys())
            elif hasattr(graph.nodes, '__iter__'):
                topology.nodes = list(graph.nodes)

        # Capture edges
        if hasattr(graph, 'edges'):
            if isinstance(graph.edges, dict):
                for source, targets in graph.edges.items():
                    if isinstance(targets, (list, set)):
                        for target in targets:
                            topology.edges.append((source, target))
                    else:
                        topology.edges.append((source, targets))
            elif hasattr(graph.edges, '__iter__'):
                topology.edges = list(graph.edges)

        # Capture conditional edges
        if hasattr(graph, '_conditional_edges'):
            for source, edge_info in graph._conditional_edges.items():
                if isinstance(edge_info, tuple) and len(edge_info) >= 2:
                    path_func, mapping = edge_info[0], edge_info[1]
                    topology.conditional_edges.append({
                        "source": source,
                        "branches": list(mapping.keys()) if isinstance(mapping, dict) else [],
                    })

        # Capture entry point
        if hasattr(graph, 'entry_point'):
            topology.entry_point = graph.entry_point
        elif hasattr(graph, '_entry_point'):
            topology.entry_point = graph._entry_point

        # Try to identify end nodes
        if hasattr(graph, '__end__'):
            topology.end_nodes.add('__end__')

        self._topology = topology
        return topology

    def wrap_compile(self, original_compile: Callable) -> Callable:
        """Wrap the StateGraph.compile() method.

        Args:
            original_compile: Original compile method

        Returns:
            Wrapped compile method
        """
        @functools.wraps(original_compile)
        def wrapped(graph_self, *args, **kwargs):
            # Capture topology before compilation
            topology = self.capture_topology(graph_self)

            with self._tracer.start_as_current_span(
                "langgraph.compile",
                kind=SpanKind.INTERNAL,
            ) as span:
                span.set_attribute("gen_ai.span.kind", "LANGGRAPH_COMPILE")
                span.set_attribute(
                    LangGraphAttributes.GRAPH_NODE_COUNT,
                    len(topology.nodes)
                )
                span.set_attribute(
                    LangGraphAttributes.GRAPH_EDGE_COUNT,
                    len(topology.edges)
                )
                span.set_attribute(
                    LangGraphAttributes.GRAPH_CONDITIONAL_EDGE_COUNT,
                    len(topology.conditional_edges)
                )
                span.set_attribute(
                    LangGraphAttributes.GRAPH_TOPOLOGY,
                    topology.to_json()
                )

                if topology.entry_point:
                    span.set_attribute(
                        LangGraphAttributes.GRAPH_ENTRY_POINT,
                        topology.entry_point
                    )

                span.add_event("graph_compiled", {
                    "nodes": topology.nodes,
                    "edge_count": len(topology.edges),
                    "conditional_edge_count": len(topology.conditional_edges),
                })

                try:
                    # Compile the graph
                    compiled = original_compile(graph_self, *args, **kwargs)

                    # Wrap the compiled graph's invoke and stream methods
                    compiled = self._wrap_compiled_graph(compiled, topology)

                    span.set_status(Status(StatusCode.OK))
                    return compiled

                except Exception as e:
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
                    raise

        return wrapped

    def _wrap_compiled_graph(self, compiled_graph: Any, topology: GraphTopology) -> Any:
        """Wrap the compiled graph's execution methods.

        Args:
            compiled_graph: The compiled graph instance
            topology: Captured graph topology

        Returns:
            Compiled graph with wrapped methods
        """
        # Store original methods
        original_invoke = compiled_graph.invoke
        original_stream = getattr(compiled_graph, 'stream', None)
        original_ainvoke = getattr(compiled_graph, 'ainvoke', None)
        original_astream = getattr(compiled_graph, 'astream', None)

        # Wrap invoke
        compiled_graph.invoke = self._create_invoke_wrapper(
            original_invoke, topology, "invoke"
        )

        # Wrap stream if available
        if original_stream:
            compiled_graph.stream = self._create_stream_wrapper(
                original_stream, topology
            )

        # Wrap async methods if available
        if original_ainvoke:
            compiled_graph.ainvoke = self._create_async_invoke_wrapper(
                original_ainvoke, topology
            )

        if original_astream:
            compiled_graph.astream = self._create_async_stream_wrapper(
                original_astream, topology
            )

        return compiled_graph

    def _create_invoke_wrapper(
        self,
        original_invoke: Callable,
        topology: GraphTopology,
        mode: str = "invoke",
    ) -> Callable:
        """Create a wrapper for invoke method.

        Args:
            original_invoke: Original invoke method
            topology: Graph topology
            mode: Execution mode

        Returns:
            Wrapped invoke method
        """
        tracer = self._tracer
        state_tracker = self._state_tracker

        @functools.wraps(original_invoke)
        def wrapped(input_data: Dict[str, Any], config: Optional[Dict[str, Any]] = None, **kwargs):
            # Reset superstep count
            self._superstep_count = 0
            state_tracker.reset()

            with tracer.start_as_current_span(
                "langgraph.invoke",
                kind=SpanKind.INTERNAL,
            ) as span:
                start_time = time.perf_counter()

                try:
                    # Set execution attributes
                    span.set_attribute("gen_ai.span.kind", LangGraphSpanKind.GRAPH)
                    span.set_attribute(LangGraphAttributes.EXECUTION_MODE, mode)
                    span.set_attribute(
                        LangGraphAttributes.GRAPH_NODE_COUNT,
                        len(topology.nodes)
                    )
                    span.set_attribute(
                        "input.value",
                        safe_json_dumps(input_data, max_length=5000)
                    )

                    # Extract thread_id if present
                    if config and "configurable" in config:
                        thread_id = config["configurable"].get("thread_id")
                        if thread_id:
                            span.set_attribute(
                                LangGraphAttributes.EXECUTION_THREAD_ID,
                                str(thread_id)
                            )

                    # Execute the graph
                    result = original_invoke(input_data, config, **kwargs)

                    # Set output and performance
                    end_time = time.perf_counter()
                    duration_ms = (end_time - start_time) * 1000

                    span.set_attribute(
                        "output.value",
                        safe_json_dumps(result, max_length=5000)
                    )
                    span.set_attribute(LangGraphAttributes.PERF_DURATION_MS, duration_ms)

                    # Get memory stats
                    memory_stats = state_tracker.get_memory_stats()
                    if "peak_bytes" in memory_stats:
                        span.set_attribute(
                            LangGraphAttributes.MEMORY_PEAK_BYTES,
                            memory_stats["peak_bytes"]
                        )

                    span.set_status(Status(StatusCode.OK))
                    return result

                except Exception as e:
                    end_time = time.perf_counter()
                    duration_ms = (end_time - start_time) * 1000

                    span.set_attribute(LangGraphAttributes.PERF_DURATION_MS, duration_ms)
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
                    raise

        return wrapped

    def _create_stream_wrapper(
        self,
        original_stream: Callable,
        topology: GraphTopology,
    ) -> Callable:
        """Create a wrapper for stream method.

        Args:
            original_stream: Original stream method
            topology: Graph topology

        Returns:
            Wrapped stream method
        """
        tracer = self._tracer
        state_tracker = self._state_tracker

        @functools.wraps(original_stream)
        def wrapped(
            input_data: Dict[str, Any],
            config: Optional[Dict[str, Any]] = None,
            stream_mode: str = "values",
            **kwargs
        ) -> Iterator:
            self._superstep_count = 0
            state_tracker.reset()

            with tracer.start_as_current_span(
                "langgraph.stream",
                kind=SpanKind.INTERNAL,
            ) as span:
                start_time = time.perf_counter()

                try:
                    span.set_attribute("gen_ai.span.kind", LangGraphSpanKind.GRAPH)
                    span.set_attribute(LangGraphAttributes.EXECUTION_MODE, "stream")
                    span.set_attribute(LangGraphAttributes.STREAM_MODE, stream_mode)
                    span.set_attribute(
                        LangGraphAttributes.GRAPH_NODE_COUNT,
                        len(topology.nodes)
                    )
                    span.set_attribute(
                        "input.value",
                        safe_json_dumps(input_data, max_length=5000)
                    )

                    if config and "configurable" in config:
                        thread_id = config["configurable"].get("thread_id")
                        if thread_id:
                            span.set_attribute(
                                LangGraphAttributes.EXECUTION_THREAD_ID,
                                str(thread_id)
                            )

                    # Stream results
                    chunk_count = 0
                    last_value = None

                    for chunk in original_stream(input_data, config, stream_mode=stream_mode, **kwargs):
                        chunk_count += 1
                        last_value = chunk

                        # Add event for each chunk
                        span.add_event(f"stream_chunk_{chunk_count}", {
                            "chunk_number": chunk_count,
                            "chunk_preview": safe_json_dumps(chunk, max_length=500),
                        })

                        yield chunk

                    # Final metrics
                    end_time = time.perf_counter()
                    duration_ms = (end_time - start_time) * 1000

                    span.set_attribute(LangGraphAttributes.PERF_DURATION_MS, duration_ms)
                    span.set_attribute("langgraph.stream.chunk_count", chunk_count)

                    if last_value is not None:
                        span.set_attribute(
                            "output.value",
                            safe_json_dumps(last_value, max_length=5000)
                        )

                    span.set_status(Status(StatusCode.OK))

                except Exception as e:
                    end_time = time.perf_counter()
                    duration_ms = (end_time - start_time) * 1000

                    span.set_attribute(LangGraphAttributes.PERF_DURATION_MS, duration_ms)
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
                    raise

        return wrapped

    def _create_async_invoke_wrapper(
        self,
        original_ainvoke: Callable,
        topology: GraphTopology,
    ) -> Callable:
        """Create a wrapper for async invoke method."""
        tracer = self._tracer
        state_tracker = self._state_tracker

        @functools.wraps(original_ainvoke)
        async def wrapped(input_data: Dict[str, Any], config: Optional[Dict[str, Any]] = None, **kwargs):
            self._superstep_count = 0
            state_tracker.reset()

            with tracer.start_as_current_span(
                "langgraph.ainvoke",
                kind=SpanKind.INTERNAL,
            ) as span:
                start_time = time.perf_counter()

                try:
                    span.set_attribute("gen_ai.span.kind", LangGraphSpanKind.GRAPH)
                    span.set_attribute(LangGraphAttributes.EXECUTION_MODE, "ainvoke")
                    span.set_attribute(
                        LangGraphAttributes.GRAPH_NODE_COUNT,
                        len(topology.nodes)
                    )
                    span.set_attribute(
                        "input.value",
                        safe_json_dumps(input_data, max_length=5000)
                    )

                    if config and "configurable" in config:
                        thread_id = config["configurable"].get("thread_id")
                        if thread_id:
                            span.set_attribute(
                                LangGraphAttributes.EXECUTION_THREAD_ID,
                                str(thread_id)
                            )

                    result = await original_ainvoke(input_data, config, **kwargs)

                    end_time = time.perf_counter()
                    duration_ms = (end_time - start_time) * 1000

                    span.set_attribute(
                        "output.value",
                        safe_json_dumps(result, max_length=5000)
                    )
                    span.set_attribute(LangGraphAttributes.PERF_DURATION_MS, duration_ms)
                    span.set_status(Status(StatusCode.OK))

                    return result

                except Exception as e:
                    end_time = time.perf_counter()
                    duration_ms = (end_time - start_time) * 1000

                    span.set_attribute(LangGraphAttributes.PERF_DURATION_MS, duration_ms)
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
                    raise

        return wrapped

    def _create_async_stream_wrapper(
        self,
        original_astream: Callable,
        topology: GraphTopology,
    ) -> Callable:
        """Create a wrapper for async stream method."""
        tracer = self._tracer
        state_tracker = self._state_tracker

        @functools.wraps(original_astream)
        async def wrapped(
            input_data: Dict[str, Any],
            config: Optional[Dict[str, Any]] = None,
            stream_mode: str = "values",
            **kwargs
        ):
            self._superstep_count = 0
            state_tracker.reset()

            with tracer.start_as_current_span(
                "langgraph.astream",
                kind=SpanKind.INTERNAL,
            ) as span:
                start_time = time.perf_counter()

                try:
                    span.set_attribute("gen_ai.span.kind", LangGraphSpanKind.GRAPH)
                    span.set_attribute(LangGraphAttributes.EXECUTION_MODE, "astream")
                    span.set_attribute(LangGraphAttributes.STREAM_MODE, stream_mode)
                    span.set_attribute(
                        LangGraphAttributes.GRAPH_NODE_COUNT,
                        len(topology.nodes)
                    )
                    span.set_attribute(
                        "input.value",
                        safe_json_dumps(input_data, max_length=5000)
                    )

                    if config and "configurable" in config:
                        thread_id = config["configurable"].get("thread_id")
                        if thread_id:
                            span.set_attribute(
                                LangGraphAttributes.EXECUTION_THREAD_ID,
                                str(thread_id)
                            )

                    chunk_count = 0
                    last_value = None

                    async for chunk in original_astream(input_data, config, stream_mode=stream_mode, **kwargs):
                        chunk_count += 1
                        last_value = chunk

                        span.add_event(f"stream_chunk_{chunk_count}", {
                            "chunk_number": chunk_count,
                            "chunk_preview": safe_json_dumps(chunk, max_length=500),
                        })

                        yield chunk

                    end_time = time.perf_counter()
                    duration_ms = (end_time - start_time) * 1000

                    span.set_attribute(LangGraphAttributes.PERF_DURATION_MS, duration_ms)
                    span.set_attribute("langgraph.stream.chunk_count", chunk_count)

                    if last_value is not None:
                        span.set_attribute(
                            "output.value",
                            safe_json_dumps(last_value, max_length=5000)
                        )

                    span.set_status(Status(StatusCode.OK))

                except Exception as e:
                    end_time = time.perf_counter()
                    duration_ms = (end_time - start_time) * 1000

                    span.set_attribute(LangGraphAttributes.PERF_DURATION_MS, duration_ms)
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
                    raise

        return wrapped

    @property
    def topology(self) -> Optional[GraphTopology]:
        """Get the captured graph topology."""
        return self._topology

    @property
    def conditional_tracker(self) -> ConditionalEdgeTracker:
        """Get the conditional edge tracker."""
        return self._conditional_tracker
