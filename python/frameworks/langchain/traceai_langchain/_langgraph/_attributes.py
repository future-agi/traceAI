"""LangGraph-specific span attributes.

These attributes follow OpenTelemetry semantic conventions and extend
the base fi_instrumentation attributes for LangGraph-specific concepts.
"""


class LangGraphAttributes:
    """Span attributes specific to LangGraph instrumentation."""

    # ==========================================================================
    # Graph Structure
    # ==========================================================================
    GRAPH_NAME = "langgraph.graph.name"
    GRAPH_NODE_COUNT = "langgraph.graph.node_count"
    GRAPH_EDGE_COUNT = "langgraph.graph.edge_count"
    GRAPH_TOPOLOGY = "langgraph.graph.topology"
    GRAPH_ENTRY_POINT = "langgraph.graph.entry_point"
    GRAPH_CONDITIONAL_EDGE_COUNT = "langgraph.graph.conditional_edge_count"

    # ==========================================================================
    # Node Execution
    # ==========================================================================
    NODE_NAME = "langgraph.node.name"
    NODE_TYPE = "langgraph.node.type"  # start/end/intermediate/subgraph
    NODE_IS_ENTRY = "langgraph.node.is_entry"
    NODE_IS_END = "langgraph.node.is_end"

    # ==========================================================================
    # Execution Mode
    # ==========================================================================
    EXECUTION_MODE = "langgraph.execution.mode"  # invoke/stream/astream/ainvoke
    EXECUTION_SUPERSTEP = "langgraph.execution.superstep"
    EXECUTION_THREAD_ID = "langgraph.execution.thread_id"
    STREAM_MODE = "langgraph.stream_mode"  # values/updates

    # ==========================================================================
    # State Management
    # ==========================================================================
    STATE_INPUT = "langgraph.state.input"
    STATE_OUTPUT = "langgraph.state.output"
    STATE_UPDATES = "langgraph.state.updates"
    STATE_CHANGED_FIELDS = "langgraph.state.changed_fields"
    STATE_REDUCER = "langgraph.state.reducer"
    STATE_BEFORE = "langgraph.state.before"
    STATE_AFTER = "langgraph.state.after"
    STATE_DIFF = "langgraph.state.diff"

    # ==========================================================================
    # Checkpointing
    # ==========================================================================
    CHECKPOINT_THREAD_ID = "langgraph.checkpoint.thread_id"
    CHECKPOINT_ID = "langgraph.checkpoint.id"
    CHECKPOINT_BACKEND = "langgraph.checkpoint.backend"
    CHECKPOINT_SIZE_BYTES = "langgraph.checkpoint.size_bytes"
    CHECKPOINT_FOUND = "langgraph.checkpoint.found"
    CHECKPOINT_OPERATION = "langgraph.checkpoint.operation"  # save/load/list

    # ==========================================================================
    # Subgraphs
    # ==========================================================================
    SUBGRAPH_PARENT = "langgraph.subgraph.parent"
    SUBGRAPH_NAME = "langgraph.subgraph.name"
    SUBGRAPH_STATE_KEYS = "langgraph.subgraph.state_keys"
    SUBGRAPH_DEPTH = "langgraph.subgraph.depth"

    # ==========================================================================
    # Conditional Edges
    # ==========================================================================
    CONDITIONAL_SOURCE = "langgraph.conditional.source"
    CONDITIONAL_RESULT = "langgraph.conditional.result"
    CONDITIONAL_AVAILABLE_BRANCHES = "langgraph.conditional.available_branches"
    CONDITIONAL_REASON = "langgraph.conditional.reason"
    CONDITIONAL_CONDITION_VALUE = "langgraph.conditional.condition_value"

    # ==========================================================================
    # Interrupt/Resume (CRITICAL - competitors failing here)
    # ==========================================================================
    INTERRUPT_REASON = "langgraph.interrupt.reason"
    INTERRUPT_IS_INTENTIONAL = "langgraph.interrupt.is_intentional"
    INTERRUPT_STATE_SNAPSHOT = "langgraph.interrupt.state_snapshot"
    INTERRUPT_NODE = "langgraph.interrupt.node"
    INTERRUPT_TIMESTAMP = "langgraph.interrupt.timestamp"

    RESUME_FROM_INTERRUPT = "langgraph.resume.from_interrupt"
    RESUME_WAIT_DURATION_SECONDS = "langgraph.resume.wait_duration_seconds"
    RESUME_INPUT = "langgraph.resume.input"
    RESUME_PREVIOUS_TRACE_ID = "langgraph.resume.previous_trace_id"
    RESUME_PREVIOUS_SPAN_ID = "langgraph.resume.previous_span_id"

    # ==========================================================================
    # Human-in-the-Loop
    # ==========================================================================
    HUMAN_DECISION = "langgraph.human.decision"  # approved/rejected/modified
    HUMAN_APPROVER_ID = "langgraph.human.approver_id"
    HUMAN_METADATA = "langgraph.human.metadata"
    HUMAN_TIMESTAMP = "langgraph.human.timestamp"
    HUMAN_FEEDBACK = "langgraph.human.feedback"

    # ==========================================================================
    # Multi-Agent Coordination
    # ==========================================================================
    MULTIAGENT_FROM = "langgraph.multiagent.from"
    MULTIAGENT_TO = "langgraph.multiagent.to"
    MULTIAGENT_CORRELATION_ID = "langgraph.multiagent.correlation_id"
    MULTIAGENT_MESSAGE_TYPE = "langgraph.multiagent.message_type"

    SUPERVISOR_NAME = "langgraph.supervisor.name"
    SUPERVISOR_SELECTED_AGENT = "langgraph.supervisor.selected_agent"
    SUPERVISOR_AVAILABLE_AGENTS = "langgraph.supervisor.available_agents"
    SUPERVISOR_ROUTING_REASON = "langgraph.supervisor.routing_reason"

    # ==========================================================================
    # Cost Tracking
    # ==========================================================================
    COST_NODE = "langgraph.cost.node"
    COST_MODEL = "langgraph.cost.model"
    COST_INPUT_TOKENS = "langgraph.cost.input_tokens"
    COST_OUTPUT_TOKENS = "langgraph.cost.output_tokens"
    COST_TOTAL_USD = "langgraph.cost.total_usd"
    COST_CACHED_TOKENS = "langgraph.cost.cached_tokens"

    # ==========================================================================
    # Performance Metrics
    # ==========================================================================
    PERF_NODE = "langgraph.perf.node"
    PERF_DURATION_MS = "langgraph.perf.duration_ms"
    PERF_START_TIME = "langgraph.perf.start_time"
    PERF_END_TIME = "langgraph.perf.end_time"
    PERF_QUEUE_TIME_MS = "langgraph.perf.queue_time_ms"

    # ==========================================================================
    # Memory Tracking (UNIQUE - no competitor has this)
    # ==========================================================================
    MEMORY_STATE_SIZE_BYTES = "langgraph.memory.state_size_bytes"
    MEMORY_STATE_SIZE_JSON_BYTES = "langgraph.memory.state_size_json_bytes"
    MEMORY_TOP_ALLOCATIONS = "langgraph.memory.top_allocations"
    MEMORY_GROWTH_WARNING = "langgraph.memory.growth_warning"
    MEMORY_PEAK_BYTES = "langgraph.memory.peak_bytes"

    # ==========================================================================
    # Agent Health
    # ==========================================================================
    HEALTH_ELAPSED_SECONDS = "langgraph.health.elapsed_seconds"
    HEALTH_SUPERSTEP_COUNT = "langgraph.health.superstep_count"
    HEALTH_WARNINGS = "langgraph.health.warnings"
    HEALTH_STATUS = "langgraph.health.status"  # healthy/warning/critical

    # ==========================================================================
    # Error & Recovery
    # ==========================================================================
    ERROR_NODE = "langgraph.error.node"
    ERROR_SUPERSTEP = "langgraph.error.superstep"
    ERROR_RECOVERABLE = "langgraph.error.recoverable"
    RECOVERY_CHECKPOINT_ID = "langgraph.recovery.checkpoint_id"
    RECOVERY_RETRY_COUNT = "langgraph.recovery.retry_count"


# Span kind values for LangGraph
class LangGraphSpanKind:
    """Span kind values specific to LangGraph."""
    GRAPH = "LANGGRAPH"
    NODE = "LANGGRAPH_NODE"
    SUPERSTEP = "LANGGRAPH_SUPERSTEP"
    CHECKPOINT = "LANGGRAPH_CHECKPOINT"
    INTERRUPT = "LANGGRAPH_INTERRUPT"
    SUBGRAPH = "LANGGRAPH_SUBGRAPH"
