"""LangGraph Instrumentor.

Main instrumentor class that provides comprehensive tracing for LangGraph workflows.
"""

import logging
from typing import Any, Collection, Dict, Optional

from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor

from traceai_langchain._langgraph._attributes import LangGraphAttributes
from traceai_langchain._langgraph._graph_wrapper import GraphWrapper, GraphTopology
from traceai_langchain._langgraph._node_wrapper import NodeWrapper, AsyncNodeWrapper
from traceai_langchain._langgraph._state_tracker import StateTransitionTracker
from traceai_langchain._langgraph._superstep_tracker import SuperstepTracker, ReducerTracker
from traceai_langchain._langgraph._checkpoint_tracer import CheckpointTracer
from traceai_langchain._langgraph._interrupt_tracker import InterruptResumeTracker
from traceai_langchain._langgraph._multiagent_tracker import MultiAgentTracker
from traceai_langchain._langgraph._cost_tracker import CostTracker


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_instruments = ("langgraph >= 0.0.1",)


class LangGraphInstrumentor(BaseInstrumentor):
    """OpenTelemetry Instrumentor for LangGraph.

    This instrumentor provides comprehensive tracing for LangGraph workflows including:
    - Graph topology capture
    - Node execution tracing with state transitions
    - Conditional edge decision tracking
    - Invoke and stream method tracing
    - Performance metrics
    - Memory tracking

    Usage:
        from traceai_langchain import LangGraphInstrumentor

        # Initialize with tracer provider
        LangGraphInstrumentor().instrument(tracer_provider=provider)

        # Or use default provider
        LangGraphInstrumentor().instrument()

        # Use LangGraph as normal - traces are automatically captured
        from langgraph.graph import StateGraph

        graph = StateGraph(MyState)
        graph.add_node("node1", my_function)
        # ... build graph ...
        compiled = graph.compile()
        result = compiled.invoke({"input": "data"})
    """

    _instance: Optional["LangGraphInstrumentor"] = None
    _is_instrumented: bool = False

    def __new__(cls) -> "LangGraphInstrumentor":
        """Singleton pattern to ensure single instrumentation."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize the instrumentor."""
        super().__init__()
        self._tracer: Optional[trace_api.Tracer] = None
        self._graph_wrapper: Optional[GraphWrapper] = None
        self._state_tracker: Optional[StateTransitionTracker] = None
        self._superstep_tracker: Optional[SuperstepTracker] = None
        self._reducer_tracker: Optional[ReducerTracker] = None
        self._checkpoint_tracer: Optional[CheckpointTracer] = None
        self._interrupt_tracker: Optional[InterruptResumeTracker] = None
        self._multiagent_tracker: Optional[MultiAgentTracker] = None
        self._cost_tracker: Optional[CostTracker] = None
        self._original_compile = None
        self._original_add_node = None

    def instrumentation_dependencies(self) -> Collection[str]:
        """Return the instrumentation dependencies."""
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        """Instrument LangGraph.

        Args:
            **kwargs: Configuration options
                - tracer_provider: OpenTelemetry tracer provider
                - enable_memory_tracking: Enable memory usage tracking (default: True)
                - max_state_history: Maximum state transitions to keep (default: 100)
        """
        if self._is_instrumented:
            logger.warning("LangGraph is already instrumented")
            return

        # Get tracer provider
        tracer_provider = kwargs.get("tracer_provider")
        if not tracer_provider:
            tracer_provider = trace_api.get_tracer_provider()

        # Create tracer
        self._tracer = trace_api.get_tracer(
            __name__,
            "0.1.0",
            tracer_provider,
        )

        # Configuration options
        enable_memory_tracking = kwargs.get("enable_memory_tracking", True)
        max_state_history = kwargs.get("max_state_history", 100)

        # Create state tracker
        self._state_tracker = StateTransitionTracker(
            max_history=max_state_history,
            enable_memory_tracking=enable_memory_tracking,
        )

        # Create superstep tracker
        self._superstep_tracker = SuperstepTracker(tracer=self._tracer)

        # Create reducer tracker
        self._reducer_tracker = ReducerTracker()

        # Create checkpoint tracer
        self._checkpoint_tracer = CheckpointTracer(tracer=self._tracer)

        # Create interrupt/resume tracker (CRITICAL for HITL workflows)
        self._interrupt_tracker = InterruptResumeTracker(tracer=self._tracer)

        # Create multi-agent tracker
        self._multiagent_tracker = MultiAgentTracker(tracer=self._tracer)

        # Create cost tracker
        self._cost_tracker = CostTracker()

        # Create graph wrapper
        self._graph_wrapper = GraphWrapper(
            tracer=self._tracer,
            state_tracker=self._state_tracker,
        )

        # Try to import and patch LangGraph
        try:
            self._patch_langgraph()
            self._is_instrumented = True
            logger.info("LangGraph instrumentation enabled")
        except ImportError as e:
            logger.warning(f"LangGraph not installed, instrumentation skipped: {e}")
        except Exception as e:
            logger.error(f"Failed to instrument LangGraph: {e}")
            raise

    def _patch_langgraph(self) -> None:
        """Patch LangGraph classes and methods."""
        try:
            from langgraph.graph import StateGraph
            from langgraph.graph.state import CompiledStateGraph
        except ImportError:
            # Try alternative import paths
            try:
                from langgraph.graph.state import StateGraph, CompiledStateGraph
            except ImportError:
                raise ImportError("Could not import LangGraph StateGraph")

        # Store original methods
        self._original_compile = StateGraph.compile

        # Patch compile method
        wrapper = self._graph_wrapper

        def patched_compile(self_graph, *args, **kwargs):
            return wrapper.wrap_compile(self._original_compile)(self_graph, *args, **kwargs)

        StateGraph.compile = patched_compile

        logger.debug("Patched StateGraph.compile")

        # Also try to patch add_node to wrap node functions
        if hasattr(StateGraph, 'add_node'):
            self._original_add_node = StateGraph.add_node

            def patched_add_node(self_graph, node_name, action, *args, **kwargs):
                # Wrap the action function if it's callable
                if callable(action) and not getattr(action, '_langgraph_wrapped', False):
                    node_wrapper = NodeWrapper(
                        tracer=wrapper._tracer,
                        state_tracker=wrapper._state_tracker,
                    )

                    # Determine node type
                    is_entry = (hasattr(self_graph, 'entry_point') and
                               self_graph.entry_point == node_name)

                    wrapped_action = node_wrapper.wrap_node(
                        node_name=node_name,
                        node_func=action,
                        is_entry=is_entry,
                        node_type="intermediate",
                    )
                    return self._original_add_node(self_graph, node_name, wrapped_action, *args, **kwargs)

                return self._original_add_node(self_graph, node_name, action, *args, **kwargs)

            StateGraph.add_node = patched_add_node
            logger.debug("Patched StateGraph.add_node")

    def _uninstrument(self, **kwargs: Any) -> None:
        """Remove LangGraph instrumentation."""
        if not self._is_instrumented:
            return

        try:
            from langgraph.graph import StateGraph
        except ImportError:
            try:
                from langgraph.graph.state import StateGraph
            except ImportError:
                return

        # Restore original methods
        if self._original_compile:
            StateGraph.compile = self._original_compile
            self._original_compile = None

        if self._original_add_node:
            StateGraph.add_node = self._original_add_node
            self._original_add_node = None

        self._is_instrumented = False
        self._tracer = None
        self._graph_wrapper = None
        self._state_tracker = None
        self._superstep_tracker = None
        self._reducer_tracker = None
        self._checkpoint_tracer = None
        self._interrupt_tracker = None
        self._multiagent_tracker = None
        self._cost_tracker = None

        logger.info("LangGraph instrumentation disabled")

    @property
    def is_instrumented(self) -> bool:
        """Check if LangGraph is instrumented."""
        return self._is_instrumented

    @property
    def graph_wrapper(self) -> Optional[GraphWrapper]:
        """Get the graph wrapper instance."""
        return self._graph_wrapper

    @property
    def state_tracker(self) -> Optional[StateTransitionTracker]:
        """Get the state tracker instance."""
        return self._state_tracker

    @property
    def superstep_tracker(self) -> Optional[SuperstepTracker]:
        """Get the superstep tracker instance."""
        return self._superstep_tracker

    @property
    def reducer_tracker(self) -> Optional[ReducerTracker]:
        """Get the reducer tracker instance."""
        return self._reducer_tracker

    @property
    def checkpoint_tracer(self) -> Optional[CheckpointTracer]:
        """Get the checkpoint tracer instance."""
        return self._checkpoint_tracer

    @property
    def interrupt_tracker(self) -> Optional[InterruptResumeTracker]:
        """Get the interrupt/resume tracker instance."""
        return self._interrupt_tracker

    @property
    def multiagent_tracker(self) -> Optional[MultiAgentTracker]:
        """Get the multi-agent tracker instance."""
        return self._multiagent_tracker

    @property
    def cost_tracker(self) -> Optional[CostTracker]:
        """Get the cost tracker instance."""
        return self._cost_tracker

    def get_topology(self) -> Optional[GraphTopology]:
        """Get the captured graph topology."""
        if self._graph_wrapper:
            return self._graph_wrapper.topology
        return None

    def get_state_history(self) -> list:
        """Get the state transition history."""
        if self._state_tracker:
            return self._state_tracker.get_history()
        return []

    def get_memory_stats(self) -> Dict[str, Any]:
        """Get memory usage statistics."""
        if self._state_tracker:
            return self._state_tracker.get_memory_stats()
        return {}

    def get_superstep_history(self) -> list:
        """Get the superstep execution history."""
        if self._superstep_tracker:
            return self._superstep_tracker.get_history()
        return []

    def get_superstep_stats(self) -> Dict[str, Any]:
        """Get superstep execution statistics."""
        if self._superstep_tracker:
            return self._superstep_tracker.get_stats()
        return {}

    def register_reducer(self, field_name: str, reducer_name: str) -> None:
        """Register a reducer function for a state field.

        Args:
            field_name: Name of the state field
            reducer_name: Name of the reducer function
        """
        if self._reducer_tracker:
            self._reducer_tracker.register_reducer(field_name, reducer_name)

    def wrap_checkpoint_saver(self, checkpoint_saver: Any) -> Any:
        """Wrap a checkpoint saver with tracing.

        Args:
            checkpoint_saver: The checkpoint saver instance

        Returns:
            The wrapped checkpoint saver
        """
        if self._checkpoint_tracer:
            return self._checkpoint_tracer.wrap_checkpoint_saver(checkpoint_saver)
        return checkpoint_saver

    def get_checkpoint_history(self) -> list:
        """Get the checkpoint operation history."""
        if self._checkpoint_tracer:
            return self._checkpoint_tracer.get_operation_history()
        return []

    def get_checkpoint_stats(self) -> Dict[str, Any]:
        """Get checkpoint operation statistics."""
        if self._checkpoint_tracer:
            return self._checkpoint_tracer.get_stats()
        return {}

    def on_interrupt(
        self,
        thread_id: str,
        node_name: str,
        reason: str,
        state: Optional[Dict[str, Any]] = None,
        is_intentional: bool = True,
    ) -> Any:
        """Record an interrupt event.

        Use this when your workflow calls interrupt() for human-in-the-loop.
        Unlike competitor implementations, this marks the interrupt as intentional
        (not an error) and stores context for unified resume tracing.

        Args:
            thread_id: Thread ID for the execution
            node_name: Name of the node where interrupt occurred
            reason: Reason for the interrupt
            state: State at the time of interrupt
            is_intentional: Whether this is an intentional interrupt

        Returns:
            InterruptInfo with captured information
        """
        if self._interrupt_tracker:
            return self._interrupt_tracker.on_interrupt(
                thread_id=thread_id,
                node_name=node_name,
                reason=reason,
                state=state,
                is_intentional=is_intentional,
            )
        return None

    def on_resume(
        self,
        thread_id: str,
        resume_input: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Record a resume event.

        Use this when execution resumes from an interrupt.
        Automatically links to the previous interrupt for unified tracing.

        Args:
            thread_id: Thread ID for the execution
            resume_input: Input provided for resumption

        Returns:
            ResumeInfo with captured information
        """
        if self._interrupt_tracker:
            return self._interrupt_tracker.on_resume(
                thread_id=thread_id,
                resume_input=resume_input,
            )
        return None

    def record_human_decision(
        self,
        decision: str,
        thread_id: Optional[str] = None,
        approver_id: Optional[str] = None,
        feedback: Optional[str] = None,
    ) -> Any:
        """Record a human approval/rejection decision.

        Args:
            decision: The decision ("approved", "rejected", "modified")
            thread_id: Thread ID for the execution
            approver_id: ID of the human approver
            feedback: Human feedback text

        Returns:
            HumanDecision with captured information
        """
        if self._interrupt_tracker:
            return self._interrupt_tracker.record_human_decision(
                decision=decision,
                thread_id=thread_id,
                approver_id=approver_id,
                feedback=feedback,
            )
        return None

    def get_interrupt_stats(self) -> Dict[str, Any]:
        """Get interrupt/resume statistics."""
        if self._interrupt_tracker:
            return self._interrupt_tracker.get_stats()
        return {}

    def track_agent_message(
        self,
        from_agent: str,
        to_agent: str,
        message: Any,
        message_type: str = "task",
        correlation_id: Optional[str] = None,
    ) -> Optional[str]:
        """Track a message passed between agents.

        Args:
            from_agent: Name of the sending agent
            to_agent: Name of the receiving agent
            message: The message content
            message_type: Type of message (task, result, feedback, etc.)
            correlation_id: Optional correlation ID for threading

        Returns:
            The correlation ID for this message
        """
        if self._multiagent_tracker:
            return self._multiagent_tracker.track_agent_message(
                from_agent=from_agent,
                to_agent=to_agent,
                message=message,
                message_type=message_type,
                correlation_id=correlation_id,
            )
        return None

    def track_supervisor_routing(
        self,
        supervisor_name: str,
        selected_agent: str,
        available_agents: list,
        reason: Optional[str] = None,
    ) -> Any:
        """Track a supervisor routing decision.

        Args:
            supervisor_name: Name of the supervisor agent
            selected_agent: Agent selected by the supervisor
            available_agents: All available agents
            reason: Reason for the selection

        Returns:
            SupervisorDecision with captured information
        """
        if self._multiagent_tracker:
            return self._multiagent_tracker.track_supervisor_routing(
                supervisor_name=supervisor_name,
                selected_agent=selected_agent,
                available_agents=available_agents,
                reason=reason,
            )
        return None

    def get_multiagent_stats(self) -> Dict[str, Any]:
        """Get multi-agent coordination statistics."""
        if self._multiagent_tracker:
            return self._multiagent_tracker.get_stats()
        return {}

    def track_llm_cost(
        self,
        node_name: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
    ) -> Any:
        """Track LLM usage cost for a node.

        Args:
            node_name: Name of the node
            model: Model name/ID
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            cached_tokens: Number of cached tokens

        Returns:
            NodeCost with calculated costs
        """
        if self._cost_tracker:
            return self._cost_tracker.track_llm_usage(
                node_name=node_name,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
            )
        return None

    def get_cost_stats(self) -> Dict[str, Any]:
        """Get cost statistics."""
        if self._cost_tracker:
            return self._cost_tracker.get_stats()
        return {}

    def get_cost_by_node(self) -> Dict[str, Any]:
        """Get costs grouped by node."""
        if self._cost_tracker:
            return self._cost_tracker.get_cost_by_node()
        return {}

    def get_cost_by_model(self) -> Dict[str, Any]:
        """Get costs grouped by model."""
        if self._cost_tracker:
            return self._cost_tracker.get_cost_by_model()
        return {}
