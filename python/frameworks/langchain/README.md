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
- **Session grouping** — LangGraph's `configurable.thread_id` becomes `session.id` on every span (an explicit `using_session(...)` or `metadata={"session_id": ...}` wins)
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
- `session.id` — from `using_session(...)`, else `config={"metadata": {"session_id": ...}}`, else LangGraph's `configurable.thread_id`
- `langgraph.interrupt` (attribute + event) on a HITL pause; a `langgraph.resume` event on resume

---

## Deep Agents

[Deep Agents](https://github.com/langchain-ai/deepagents) builds a LangGraph graph
with `create_deep_agent`. There is no Deep Agents instrumentor and no separate
package: `LangChainInstrumentor` traces the graph, its model calls, its built-in
tools and its `task` subagents. The cookbook is
[`examples/deep_agents.py`](examples/deep_agents.py); the compatibility test
(`tests/test_deepagents_compat.py`) runs that same script offline against a
scripted fake model and a loopback collector.

Tested versions, as printed by the compatibility test:

| Package | Version |
|---|---|
| `deepagents` | 0.7.21 (PyPI wheel) |
| `langchain` | 1.4.3 |
| `langchain-core` | 1.6.6 (the floor `deepagents` 0.7.21 requires, and the latest on PyPI when tested) |
| `langgraph` | 1.2.12 |
| `traceAI-langchain` | 0.2.0 from this repository, including the `thread_id` → `session.id` change |
| Python | 3.11.12 and 3.13.7 |

`traceAI-langchain`'s own lower bound (`langchain-core>=0.2.43`) is unchanged.
Deep Agents itself needs `langchain-core>=1.6.6` and `langgraph`.

```bash
pip install traceAI-langchain "deepagents==0.7.21" langgraph
```

```python
from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from fi_instrumentation import TraceConfig, register
from fi_instrumentation.fi_types import ProjectType
from traceai_langchain import LangChainInstrumentor

# 1. register(), 2. instrument(), 3. build the agent.
trace_provider = register(project_type=ProjectType.OBSERVE, project_name="deep-agents-app")
LangChainInstrumentor().instrument(
    tracer_provider=trace_provider,
    # Filesystem tool arguments and results can contain file contents.
    config=TraceConfig(hide_inputs=True, hide_outputs=True),
)

# Pass a model explicitly: model=None selects the deprecated claude-sonnet-4-6 default.
agent = create_deep_agent(model=model, tools=[my_tool], backend=StateBackend())
result = agent.invoke(
    {"messages": [{"role": "user", "content": "..."}]},
    config={"configurable": {"thread_id": "t-1"}},  # becomes session.id
)
trace_provider.force_flush()
```

What you get (all from `traceAI-langchain`; Deep Agents adds no keys):

- **LLM** spans with `gen_ai.request.model` and `gen_ai.usage.*` from the model's
  `usage_metadata`. Token counts are only on LLM spans, so a trace's token total is
  the sum of its model calls.
- **TOOL** spans named after the tool that ran. With the default `StateBackend`,
  0.7.21 binds `ls`, `read_file`, `write_file`, `edit_file`, `delete`, `glob`,
  `grep` and `task`, plus your tools. `execute` is bound only for a sandbox backend.
  `write_todos` is not a default tool at this version.
- **Subagents**: the `task` TOOL span is the parent of the subagent's span (named
  after the subagent, e.g. `general-purpose`, kind `CHAIN`) and of its model and
  tool spans, all in one trace. Subagent spans also carry `lc_agent_name`.
- **Graph nodes** (`model`, `tools`, middleware nodes) are `CHAIN` spans; a node whose
  name contains "agent" (e.g. `PatchToolCallsMiddleware.before_agent`) is reported as
  `AGENT` by the existing name heuristic.
- **Session**: `configurable.thread_id` → `session.id` on every span, subagents included.
- **Errors**: a tool that raises ends its TOOL span `ERROR` with an `exception`
  event (LangGraph's default tool error handler re-raises, so `invoke` raises too).
  Cancelling an `astream` early closes every span; the root span ends `ERROR`
  with a `GeneratorExit` description.

Not emitted: cost, user id (set `using_attributes(user_id=...)` yourself),
retrieval (Deep Agents has no retriever tool), and a `gen_ai.provider.name`
(LangChain's raw `ls_provider` metadata is passed through as-is).

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

### Deep Agents Example
- `examples/deep_agents.py` - `create_deep_agent` with a custom tool, built-in file tools and a `task` subagent (runs offline)

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
