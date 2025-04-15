# HuggingFace Smolagents OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the HuggingFace Smolagents framework. It enables tracing and monitoring of applications built with HuggingFace Smolagents.

## Installation

1. **Install traceAI Smolagents**

```bash
pip install traceAI-smolagents
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
    project_name="smolagents_app",
    project_version_name="v1",
    session_name="chat-bot"
)
```

### Configure Smolagents Instrumentation
Instrument the Smolagents client to enable telemetry collection.

```python
from traceai_smolagents import SmolagentsInstrumentor

SmolagentsInstrumentor().instrument(tracer_provider=trace_provider)
```

### Create Smolagents Components
Set up your Smolagents client with built-in observability.

```python
from smolagents import (
    CodeAgent,
    DuckDuckGoSearchTool,
    OpenAIServerModel,
    ToolCallingAgent,
)

model = OpenAIServerModel(model_id="gpt-4o")
agent = ToolCallingAgent(
    tools=[DuckDuckGoSearchTool()],
    model=model,
    max_steps=3,
    name="search",
    description=(
        "This is an agent that can do web search. "
        "When solving a task, ask him directly first, he gives good answers. "
        "Then you can double check."
    ),
)
manager_agent = CodeAgent(
    tools=[DuckDuckGoSearchTool()],
    model=model,
    managed_agents=[agent],
)
manager_agent.run(
    "How many seconds would it take for a leopard at full speed to run through Pont des Arts? "
    "ASK YOUR MANAGED AGENT FOR LEOPARD SPEED FIRST"
)
```

