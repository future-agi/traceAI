# Llama Index OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the Llama Index framework. It enables tracing and monitoring of applications built with Llama Index.

## Installation

1. **Install traceAI Llama Index**

```bash
pip install traceAI-llamaindex
```

2. **Install Llama Index**

```bash
pip install llama-index
```


### Set Environment Variables
Set up your environment variables to authenticate with both FutureAGI.

```python
import os

os.environ["FI_API_KEY"] = FI_API_KEY
os.environ["FI_SECRET_KEY"] = FI_SECRET_KEY
```

## Quickstart

### Register Tracer Provider
Set up the trace provider to establish the observability pipeline. The trace provider:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="llama_index_app",
    project_version_name="v1",
    session_name="chat-bot"
)
```

### Configure Llama Index Instrumentation
Instrument the Llama Index client to enable telemetry collection. This step ensures that all interactions with the Llama Index SDK are tracked and monitored.

```python
from traceai_llamaindex import LlamaIndexInstrumentor

LlamaIndexInstrumentor().instrument(tracer_provider=trace_provider)
```

### Create Llama Index Components
Set up your Llama Index client with built-in observability.

```python
from llama_index.agent.openai import OpenAIAgent
from llama_index.core import Settings
from llama_index.core.tools import FunctionTool
from llama_index.llms.openai import OpenAI

def multiply(a: int, b: int) -> int:
    """Multiply two integers and return the result."""
    return a * b

def add(a: int, b: int) -> int:
    """Add two integers and return the result."""
    return a + b

multiply_tool = FunctionTool.from_defaults(fn=multiply)
add_tool = FunctionTool.from_defaults(fn=add)
agent = OpenAIAgent.from_tools([multiply_tool, add_tool])
Settings.llm = OpenAI(model="gpt-3.5-turbo")

if __name__ == "__main__":
    response = agent.query("What is (121 * 3) + 42?")
    print(response)
```

