# Groq OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the Groq framework. It enables tracing and monitoring of applications built with Groq.

## Installation

1. **Install traceAI Groq**

```bash
pip install traceAI-groq
```


### Set Environment Variables
Set up your environment variables to authenticate with FutureAGI.

```python
import os

os.environ["FI_API_KEY"] = FI_API_KEY
os.environ["FI_SECRET_KEY"] = FI_SECRET_KEY
os.environ["GROQ_API_KEY"] = GROQ_API_KEY
```

## Quickstart

### Register Tracer Provider
Set up the trace provider to establish the observability pipeline. The trace provider:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="groq_app",
    project_version_name="v1",
    session_name="chat-bot"
)
```

### Configure Groq Instrumentation
Instrument the Groq client to enable telemetry collection. This step ensures that all interactions with the Groq SDK are tracked and monitored.

```python
from groq import Groq

def test():
    client = Groq(
        api_key=os.environ.get("GROQ_API_KEY"),
    )

    weather_function = {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "finds the weather for a given city",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "The city to find the weather for, e.g. 'London'",
                    }
                },
                "required": ["city"],
            },
        },
    }

    sys_prompt = "Respond to the user's query using the correct tool."
    user_msg = "What's the weather like in San Francisco?"

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_msg},
    ]
    response = client.chat.completions.create(
        model="mixtral-8x7b-32768",
        messages=messages,
        temperature=0.0,
        tools=[weather_function],
        tool_choice="required",
    )

    message = response.choices[0].message
    assert (tool_calls := message.tool_calls)
    tool_call_id = tool_calls[0].id
    messages.append(message)
    messages.append(
        ChatCompletionToolMessageParam(
            content="sunny", role="tool", tool_call_id=tool_call_id
        ),
    )
    final_response = client.chat.completions.create(
        model="mixtral-8x7b-32768",
        messages=messages,
    )
    return final_response

if __name__ == "__main__":
    response = test()
    print("Response\n")
    print(response)
```

