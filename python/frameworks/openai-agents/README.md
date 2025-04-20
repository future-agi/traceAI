# OpenAI Agents OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the OpenAI Agents framework. It enables tracing and monitoring of applications built with OpenAI Agents.

## Installation

1. **Install traceAI OpenAI Agents**

```bash
pip install traceAI-openai-agents
```


### Set Environment Variables
Set up your environment variables to authenticate with FutureAGI

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
    project_name="openai_agents_app"
)
```

### Configure OpenAI Agents Instrumentation
Instrument the OpenAI Agents client to enable telemetry collection. This step ensures that all interactions with the OpenAI Agents SDK are tracked and monitored.

```python
from traceai_openai_agents import OpenAIAgentsInstrumentor

OpenAIAgentsInstrumentor().instrument(tracer_provider=trace_provider)
```

### Create OpenAI Agents Components
Set up your OpenAI Agents client with built-in observability.

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")
result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")

print(result.final_output)

```

