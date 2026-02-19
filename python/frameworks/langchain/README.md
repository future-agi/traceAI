# LangChain & LangGraph OpenTelemetry Integration

## Overview
This integration provides comprehensive OpenTelemetry instrumentation for both LangChain and LangGraph frameworks. It enables detailed tracing and monitoring of applications built with these frameworks.

## Installation

### Install traceAI LangChain
```bash
pip install traceAI-langchain
```

### For LangGraph support (optional)
```bash
pip install traceAI-langchain[langgraph]
```

### Install LangChain OpenAI
```bash
pip install langchain-openai
```

## Environment Variables
Set up your environment variables to authenticate with FutureAGI.

```python
import os

os.environ["FI_API_KEY"] = FI_API_KEY
os.environ["FI_SECRET_KEY"] = FI_SECRET_KEY
os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
```

## LangChain Quickstart

### Register Tracer Provider
Set up the trace provider to establish the observability pipeline:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="langchain_app",
    session_name="chat-bot"
)
```

### Configure LangChain Instrumentation
Instrument the LangChain client to enable telemetry collection:

```python
from traceai_langchain import LangChainInstrumentor

LangChainInstrumentor().instrument(tracer_provider=trace_provider)
```

### Create LangChain Components
Set up your LangChain client with built-in observability:

```python
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

prompt = ChatPromptTemplate.from_template("{x} {y} {z}?").partial(x="why is", z="blue")
chain = prompt | ChatOpenAI(model_name="gpt-3.5-turbo")

def run_chain():
    try:
        result = chain.invoke({"y": "sky"})
        print(f"Response: {result}")
    except Exception as e:
        print(f"Error executing chain: {e}")

if __name__ == "__main__":
    run_chain()
```

---

## LangGraph Instrumentation

LangGraph instrumentation provides comprehensive tracing for graph-based workflows, including:

- **Graph Topology Capture**: Automatically captures graph structure (nodes, edges, entry points)
- **Node Execution Tracing**: Tracks each node execution with state transitions
- **Conditional Edge Decisions**: Records branch decisions at routing nodes
- **State Transition Tracking**: Tracks state changes through graph execution with diffs
- **Performance Metrics**: Duration, memory usage, and execution counts
- **Memory Tracking**: Detects potential memory leaks in state growth

### LangGraph Quickstart

```python
from typing import TypedDict
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from langgraph.graph import StateGraph, END
from traceai_langchain import LangChainInstrumentor, LangGraphInstrumentor

# Define state schema
class MyState(TypedDict):
    messages: list
    current_step: str

# Register trace provider
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="langgraph_app",
    session_name="workflow"
)

# Instrument both LangChain and LangGraph
LangChainInstrumentor().instrument(tracer_provider=trace_provider)
LangGraphInstrumentor().instrument(tracer_provider=trace_provider)

# Build your graph
workflow = StateGraph(MyState)
workflow.add_node("process", process_node)
workflow.add_node("analyze", analyze_node)
workflow.add_edge("process", "analyze")
workflow.add_edge("analyze", END)
workflow.set_entry_point("process")

# Compile and run - traces are automatically captured
app = workflow.compile()
result = app.invoke({"messages": [], "current_step": "start"})
```

### LangGraph Span Attributes

The instrumentation captures rich attributes for debugging and monitoring:

#### Graph Structure
- `langgraph.graph.name` - Graph name
- `langgraph.graph.node_count` - Number of nodes
- `langgraph.graph.edge_count` - Number of edges
- `langgraph.graph.topology` - Full topology as JSON
- `langgraph.graph.entry_point` - Entry point node

#### Node Execution
- `langgraph.node.name` - Node name
- `langgraph.node.type` - Node type (start/end/intermediate)
- `langgraph.node.is_entry` - Whether this is the entry node
- `langgraph.node.is_end` - Whether this is an end node

#### State Management
- `langgraph.state.input` - Input state (JSON)
- `langgraph.state.output` - Output state (JSON)
- `langgraph.state.updates` - State updates from node
- `langgraph.state.changed_fields` - List of changed fields
- `langgraph.state.diff` - State diff (JSON)

#### Conditional Edges
- `langgraph.conditional.source` - Source node
- `langgraph.conditional.result` - Selected branch
- `langgraph.conditional.available_branches` - All available branches

#### Performance
- `langgraph.perf.duration_ms` - Execution duration
- `langgraph.perf.node` - Node name for performance metric

#### Memory Tracking
- `langgraph.memory.state_size_bytes` - Current state size
- `langgraph.memory.peak_bytes` - Peak memory usage
- `langgraph.memory.growth_warning` - Memory growth alert

### Advanced: Accessing State and Topology

```python
# Get the instrumentor instance
instrumentor = LangGraphInstrumentor()

# After graph execution, access captured data
topology = instrumentor.get_topology()
if topology:
    print(f"Nodes: {topology.nodes}")
    print(f"Edges: {topology.edges}")
    print(f"Entry point: {topology.entry_point}")

# Get state transition history
history = instrumentor.get_state_history()
for transition in history:
    print(f"Node: {transition['node']}")
    print(f"Diff: {transition['diff']}")

# Get memory statistics
memory_stats = instrumentor.get_memory_stats()
print(f"Peak memory: {memory_stats.get('peak_bytes', 0)} bytes")
```

---

## Examples

### LangChain Examples
- `examples/chat_prompt_template.py` - Basic chat prompt usage
- `examples/rag.py` - Retrieval-augmented generation
- `examples/tool_calling_agent.py` - Agent with tools
- `examples/openai_chat_stream.py` - Streaming responses

### LangGraph Examples
- `examples/langgraph_simple_workflow.py` - Simple state machine workflow
- `examples/langgraph_agent_supervisor.py` - Multi-agent supervisor pattern

---

## API Reference

### LangChainInstrumentor

```python
from traceai_langchain import LangChainInstrumentor

# Initialize and instrument
instrumentor = LangChainInstrumentor()
instrumentor.instrument(tracer_provider=trace_provider)

# Get current span
span = instrumentor.get_span(run_id)

# Get ancestor spans
ancestors = instrumentor.get_ancestors(run_id)
```

### LangGraphInstrumentor

```python
from traceai_langchain import LangGraphInstrumentor

# Initialize and instrument
instrumentor = LangGraphInstrumentor()
instrumentor.instrument(
    tracer_provider=trace_provider,
    enable_memory_tracking=True,  # default: True
    max_state_history=100,        # default: 100
)

# Check instrumentation status
is_active = instrumentor.is_instrumented

# Get captured topology
topology = instrumentor.get_topology()

# Get state history
history = instrumentor.get_state_history()

# Get memory statistics
stats = instrumentor.get_memory_stats()

# Uninstrument when done
instrumentor.uninstrument()
```

### LangGraphAttributes

All span attributes are defined as constants:

```python
from traceai_langchain import LangGraphAttributes

# Use in custom spans or queries
attrs = {
    LangGraphAttributes.NODE_NAME: "my_node",
    LangGraphAttributes.EXECUTION_MODE: "invoke",
}
```

---

## Troubleshooting

### LangGraph not being traced
1. Ensure you've installed the langgraph extra: `pip install traceAI-langchain[langgraph]`
2. Initialize `LangGraphInstrumentor` before creating any graphs
3. Check that `langgraph` is installed: `pip show langgraph`

### State diffs not appearing
State diffs only appear when there are actual changes between before and after states. If a node returns the same state, no diff will be recorded.

### Memory tracking overhead
If memory tracking causes performance issues, disable it:
```python
LangGraphInstrumentor().instrument(enable_memory_tracking=False)
```
