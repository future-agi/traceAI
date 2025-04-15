# VertexAI OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the VertexAI framework. It enables tracing and monitoring of applications built with VertexAI.

## Installation

1. **Install traceAI VertexAI**

```bash
pip install traceAI-vertexai
```

2. **Install VertexAI SDK**

```bash
pip install vertexai
```

### Set Environment Variables
Set up your environment variables to authenticate with both FutureAGI.

```python
import os

os.environ["FI_API_KEY"] = FI_API_KEY
os.environ["FI_SECRET_KEY"] = FI_SECRET_KEY
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GOOGLE_APPLICATION_CREDENTIALS
```

## Quickstart

### Register Tracer Provider
Set up the trace provider to establish the observability pipeline. The trace provider:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="vertexai_app",
    project_version_name="v1",
    session_name="chat-bot"
)
```

### Configure VertexAI Instrumentation
Instrument the VertexAI client to enable telemetry collection. This step ensures that all interactions with the VertexAI SDK are tracked and monitored.

```python
from traceai_vertexai import VertexAIInstrumentor

VertexAIInstrumentor().instrument(tracer_provider=trace_provider)
```

### Create VertexAI Components
Set up your VertexAI client with built-in observability.

```python
import vertexai
from vertexai.generative_models import FunctionDeclaration, GenerativeModel, Part, Tool

vertexai.init(
    project="project_name",
)

# Describe a function by specifying its schema (JsonSchema format)
get_current_weather_func = FunctionDeclaration(
    name="get_current_weather",
    description="Get the current weather in a given location",
    parameters={
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "The city and state, e.g. San Francisco, CA",
            },
            "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
        },
        "required": ["location"],
    },
)

# Tool is a collection of related functions
weather_tool = Tool(function_declarations=[get_current_weather_func])

# Use tools in chat
chat = GenerativeModel("gemini-1.5-flash", tools=[weather_tool]).start_chat()

if __name__ == "__main__":
    for response in chat.send_message(
        "What is the weather like in Boston?", stream=True
    ):
        print(response)
    for response in chat.send_message(
        Part.from_function_response(
            name="get_current_weather",
            response={"content": {"weather": "super nice"}},
        ),
        stream=True,
    ):
        print(response)
```

