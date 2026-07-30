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

LangGraph tracing is **automatic**: LangGraph runs each node, tool and LLM through
LangChain's callback system, so `LangChainInstrumentor` captures them with no
LangGraph-specific setup. You get:

- **Per-node spans** named after the graph node (`agent`, `tools`, `ask_human`, …)
- **Tool and LLM spans** nested under their node
- **Graph-node enrichment** — the node's own span carries `gen_ai.agent.graph.node_name` and `gen_ai.agent.graph.node_id`
- **Session grouping** — bind `session.id = thread_id` (see the quickstart)
- **HITL interrupts** traced correctly — an interrupted node/tool span stays `OK` (not `ERROR`) and is marked with `langgraph.interrupt`

> **`LangGraphInstrumentor` is a deprecated no-op** kept for backwards compatibility.
> You can remove any `LangGraphInstrumentor().instrument()` call — tracing happens
> through `LangChainInstrumentor`.

### LangGraph Quickstart

```python
from typing import Annotated, TypedDict
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.instrumentation.context_attributes import using_session
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from traceai_langchain import LangChainInstrumentor

class MyState(TypedDict):
    messages: Annotated[list, add_messages]

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="langgraph_app",
)

# Instrument LangChain — LangGraph is traced through it automatically.
LangChainInstrumentor().instrument(tracer_provider=trace_provider)

workflow = StateGraph(MyState)
workflow.add_node("agent", agent_node)   # sync or async nodes both work
workflow.add_edge(START, "agent")
workflow.add_edge("agent", END)
app = workflow.compile()

# Group every span of a conversation under one session:
with using_session("thread-123"):
    result = app.invoke(
        {"messages": []}, {"configurable": {"thread_id": "thread-123"}}
    )
```

### LangGraph Span Attributes

Node, tool and LLM runs are standard LangChain spans (`gen_ai.span.kind` =
`CHAIN` / `TOOL` / `LLM` / `AGENT`) with these LangGraph additions:

- `gen_ai.agent.graph.node_name`, `gen_ai.agent.graph.node_id` — the graph node (on the node's own span)
- `langgraph_node`, `langgraph_step`, `langgraph_triggers`, `langgraph_path`, `langgraph_checkpoint_ns` — LangGraph's raw callback metadata
- `session.id` — when set via `using_session(...)` or `config={"metadata": {"session_id": ...}}`
- `langgraph.interrupt` (attribute + event) on a HITL pause; a `langgraph.resume` event on resume

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
- `examples/langgraph_human_in_the_loop.py` - Human-in-the-loop interrupt workflow

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

# Deprecated no-op — LangGraph is traced automatically by LangChainInstrumentor.
# instrument() only logs a one-time deprecation notice; you can remove this call.
LangGraphInstrumentor().instrument(tracer_provider=trace_provider)
```

---

## Troubleshooting

### LangGraph not being traced
1. Make sure you instrumented **`LangChainInstrumentor`** — LangGraph is traced through it (`LangGraphInstrumentor` is a no-op).
2. Install the langgraph extra if you use LangGraph: `pip install traceAI-langchain[langgraph]`.
3. Check that `langgraph` is installed: `pip show langgraph`.
