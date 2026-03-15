# TraceAI MiniMax Instrumentation

OpenTelemetry instrumentation for [MiniMax](https://www.minimax.io/) - chat completions via the OpenAI-compatible API.

## Installation

```bash
pip install traceai-minimax
```

## Features

- Automatic tracing of MiniMax API calls via OpenAI SDK
- Support for MiniMax-M2.5 and MiniMax-M2.5-highspeed models (204K context)
- Streaming response support
- Token usage tracking
- Function/tool calling support
- Full OpenTelemetry semantic conventions compliance

## Usage

### Basic Setup

```python
from openai import OpenAI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

from traceai_minimax import MiniMaxInstrumentor

# Set up tracing
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)

# Instrument MiniMax
MiniMaxInstrumentor().instrument(tracer_provider=provider)

# Use MiniMax via OpenAI SDK
client = OpenAI(
    api_key="your-minimax-api-key",
    base_url="https://api.minimax.io/v1"
)

response = client.chat.completions.create(
    model="MiniMax-M2.5",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)
```

### MiniMax Chat

```python
from openai import OpenAI

client = OpenAI(
    api_key="your-minimax-api-key",
    base_url="https://api.minimax.io/v1"
)

# Simple chat
response = client.chat.completions.create(
    model="MiniMax-M2.5",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is machine learning?"}
    ],
    temperature=0.7,
    max_tokens=1024
)
print(response.choices[0].message.content)
```

### Streaming Responses

```python
from openai import OpenAI

client = OpenAI(
    api_key="your-minimax-api-key",
    base_url="https://api.minimax.io/v1"
)

# Streaming chat
stream = client.chat.completions.create(
    model="MiniMax-M2.5",
    messages=[{"role": "user", "content": "Tell me a story"}],
    stream=True
)

for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()
```

### Function Calling / Tools

```python
from openai import OpenAI
import json

client = OpenAI(
    api_key="your-minimax-api-key",
    base_url="https://api.minimax.io/v1"
)

tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a location",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "The city name"
                    },
                    "unit": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                        "description": "Temperature unit"
                    }
                },
                "required": ["location"]
            }
        }
    }
]

response = client.chat.completions.create(
    model="MiniMax-M2.5",
    messages=[{"role": "user", "content": "What's the weather in Paris?"}],
    tools=tools,
    tool_choice="auto"
)

message = response.choices[0].message
if message.tool_calls:
    for tool_call in message.tool_calls:
        print(f"Function: {tool_call.function.name}")
        print(f"Arguments: {tool_call.function.arguments}")
```

### Async Usage

```python
import asyncio
from openai import AsyncOpenAI

async def main():
    client = AsyncOpenAI(
        api_key="your-minimax-api-key",
        base_url="https://api.minimax.io/v1"
    )

    response = await client.chat.completions.create(
        model="MiniMax-M2.5",
        messages=[{"role": "user", "content": "Hello!"}]
    )
    print(response.choices[0].message.content)

asyncio.run(main())
```

### JSON Mode

```python
from openai import OpenAI

client = OpenAI(
    api_key="your-minimax-api-key",
    base_url="https://api.minimax.io/v1"
)

response = client.chat.completions.create(
    model="MiniMax-M2.5",
    messages=[
        {"role": "system", "content": "Output valid JSON only."},
        {"role": "user", "content": "List 3 programming languages with their main use cases"}
    ],
    response_format={"type": "json_object"}
)

import json
data = json.loads(response.choices[0].message.content)
print(data)
```

## Configuration Options

### TraceConfig

```python
from fi_instrumentation import TraceConfig
from traceai_minimax import MiniMaxInstrumentor

config = TraceConfig(
    hide_inputs=False,
    hide_outputs=False,
)

MiniMaxInstrumentor().instrument(
    tracer_provider=provider,
    config=config
)
```

## Captured Attributes

### Common Attributes

| Attribute | Description |
|-----------|-------------|
| `fi.span.kind` | "LLM" |
| `llm.system` | "minimax" |
| `llm.provider` | "minimax" |
| `llm.model` | Model name (MiniMax-M2.5, MiniMax-M2.5-highspeed) |
| `llm.token_count.prompt` | Input token count |
| `llm.token_count.completion` | Output token count |
| `llm.token_count.total` | Total token count |

### MiniMax-Specific Attributes

| Attribute | Description |
|-----------|-------------|
| `minimax.response_id` | Unique response ID |
| `minimax.finish_reason` | Response finish reason (stop, tool_calls, length) |
| `minimax.tool_calls_count` | Number of tool calls |
| `minimax.tools_count` | Number of tools provided |

## Available Models

| Model | Description |
|-------|-------------|
| `MiniMax-M2.5` | General-purpose model with 204K context window |
| `MiniMax-M2.5-highspeed` | Faster inference variant with 204K context window |

## Important Notes

1. **OpenAI SDK Required**: MiniMax uses the OpenAI-compatible API, so you need the `openai` package installed.

2. **Base URL**: Always set `base_url="https://api.minimax.io/v1"` when creating the client.

3. **API Key**: Get your API key from the [MiniMax Platform](https://platform.minimax.chat/).

4. **Selective Instrumentation**: The instrumentor only traces calls to MiniMax's API. Regular OpenAI API calls are not affected.

5. **Temperature**: MiniMax requires temperature to be in the range (0.0, 1.0]. A value of exactly 0 is not accepted.

## License

Apache-2.0
