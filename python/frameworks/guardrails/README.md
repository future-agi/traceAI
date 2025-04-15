# Guardrails OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the Guardrails framework. It enables tracing and monitoring of applications built with Guardrails.

## Installation

1. **Install traceAI Guardrails**

```bash
pip install traceAI-guardrails
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
    project_name="guardrails_app",
    project_version_name="v1",
    session_name="chat-bot"
)
```

### Configure Guardrails Instrumentation
Instrument the Guardrails client to enable telemetry collection. This step ensures that all interactions with the Guardrails SDK are tracked and monitored.

```python
from traceai_guardrails import GuardrailsInstrumentor

GuardrailsInstrumentor().instrument(tracer_provider=trace_provider)
```

### Create Guardrails Components
Set up your Guardrails client with built-in observability.

```python
from guardrails import Guard

guard = Guard()
tools = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get current temperature for a given location.",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City and country e.g. Bogotá, Colombia"
                }
            },
            "required": [
                "location"
            ],
            "additionalProperties": False
        },
        "strict": True
    }
}]

result = guard(
    messages=[
            {
                "role": "user",
                "content": "What is the weather in Delhi?",
            },
        ],
    model="gpt-4o",
    tools=tools

)

print(f"{result}")
```

