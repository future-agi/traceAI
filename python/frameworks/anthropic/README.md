# Anthropic OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the Anthropic framework. It enables tracing and monitoring of applications built with Anthropic.

## Installation

1. **Install traceAI Anthropic**

```bash
pip install traceAI-anthropic
```


### Set Environment Variables
Set up your environment variables to authenticate with both FutureAGI.

```python
import os

os.environ["FI_API_KEY"] = FI_API_KEY
os.environ["FI_SECRET_KEY"] = FI_SECRET_KEY
os.environ["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY
```

## Quickstart

### Register Tracer Provider
Set up the trace provider to establish the observability pipeline. The trace provider:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="anthropic_app",
    project_version_name="v1",
    session_name="chat-bot"
)
```

### Configure Anthropic Instrumentation
Instrument the Anthropic client to enable telemetry collection. This step ensures that all interactions with the Anthropic SDK are tracked and monitored.

```python
from traceai_anthropic import AnthropicInstrumentor

AnthropicInstrumentor().instrument(tracer_provider=trace_provider)
```

### Create Anthropic Components
Set up your Anthropic client with built-in observability.

```python
client = anthropic.Anthropic()

response = client.messages.create(
    model="claude-3-5-sonnet-20240620",
    max_tokens=1024,
    tools=[
        {
            "name": "get_weather",
            "description": "Get the current weather in a given location",
            "input_schema": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "The city and state, e.g. San Francisco, CA",
                    },
                    "unit": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                        "description": "The unit of temperature, either 'celsius' or 'fahrenheit'",
                    },
                },
                "required": ["location"],
            },
        },
        {
            "name": "get_time",
            "description": "Get the current time in a given time zone",
            "input_schema": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "The IANA time zone name, e.g. America/Los_Angeles",
                    }
                },
                "required": ["timezone"],
            },
        },
    ],
    messages=[
        {
            "role": "user",
            "content": "What is the weather like right now in New York?"
            " Also what time is it there? Use necessary tools simultaneously.",
        }
    ],
)
print(response)
```

