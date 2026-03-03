# TraceAI DeepSeek Instrumentation

OpenTelemetry instrumentation for [DeepSeek](https://www.deepseek.com/) - chat completions, reasoning models, and function calling.

## Installation

```bash
pip install traceai-deepseek
```

## Features

- Automatic tracing of DeepSeek API calls via OpenAI SDK
- Support for DeepSeek Chat, Coder, and Reasoner (R1) models
- Streaming response support
- Token usage tracking including prompt cache metrics
- Reasoning content extraction for R1 models
- Function/tool calling support
- Full OpenTelemetry semantic conventions compliance

## Usage

### Basic Setup

```python
from openai import OpenAI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

from traceai_deepseek import DeepSeekInstrumentor

# Set up tracing
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)

# Instrument DeepSeek
DeepSeekInstrumentor().instrument(tracer_provider=provider)

# Use DeepSeek via OpenAI SDK
client = OpenAI(
    api_key="your-deepseek-api-key",
    base_url="https://api.deepseek.com/v1"
)

response = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)
```

### DeepSeek Chat

```python
from openai import OpenAI

client = OpenAI(
    api_key="your-deepseek-api-key",
    base_url="https://api.deepseek.com/v1"
)

# Simple chat
response = client.chat.completions.create(
    model="deepseek-chat",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is machine learning?"}
    ],
    temperature=0.7,
    max_tokens=1024
)
print(response.choices[0].message.content)
```

### DeepSeek Reasoner (R1 Models)

DeepSeek R1 models provide reasoning capabilities with detailed thought processes.

```python
from openai import OpenAI

client = OpenAI(
    api_key="your-deepseek-api-key",
    base_url="https://api.deepseek.com/v1"
)

# Using DeepSeek Reasoner
response = client.chat.completions.create(
    model="deepseek-reasoner",
    messages=[
        {"role": "user", "content": "Solve this step by step: If a train travels 120 km in 2 hours, what is its average speed?"}
    ]
)

# Access the reasoning content (thought process)
message = response.choices[0].message
print("Reasoning:", message.reasoning_content)
print("Answer:", message.content)

# Token usage includes reasoning tokens
print(f"Reasoning tokens: {response.usage.completion_tokens_details.reasoning_tokens}")
```

### Streaming Responses

```python
from openai import OpenAI

client = OpenAI(
    api_key="your-deepseek-api-key",
    base_url="https://api.deepseek.com/v1"
)

# Streaming chat
stream = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "Tell me a story"}],
    stream=True
)

for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()

# Streaming with R1 (includes reasoning)
stream = client.chat.completions.create(
    model="deepseek-reasoner",
    messages=[{"role": "user", "content": "What is 15% of 240?"}],
    stream=True
)

reasoning_content = ""
content = ""
for chunk in stream:
    delta = chunk.choices[0].delta
    if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
        reasoning_content += delta.reasoning_content
    if delta.content:
        content += delta.content

print("Reasoning:", reasoning_content)
print("Answer:", content)
```

### Function Calling / Tools

```python
from openai import OpenAI
import json

client = OpenAI(
    api_key="your-deepseek-api-key",
    base_url="https://api.deepseek.com/v1"
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
    model="deepseek-chat",
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
        api_key="your-deepseek-api-key",
        base_url="https://api.deepseek.com/v1"
    )

    response = await client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": "Hello!"}]
    )
    print(response.choices[0].message.content)

asyncio.run(main())
```

### JSON Mode

```python
from openai import OpenAI

client = OpenAI(
    api_key="your-deepseek-api-key",
    base_url="https://api.deepseek.com/v1"
)

response = client.chat.completions.create(
    model="deepseek-chat",
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
from traceai_deepseek import DeepSeekInstrumentor

config = TraceConfig(
    hide_inputs=False,
    hide_outputs=False,
)

DeepSeekInstrumentor().instrument(
    tracer_provider=provider,
    config=config
)
```

## Captured Attributes

### Common Attributes

| Attribute | Description |
|-----------|-------------|
| `fi.span.kind` | "LLM" |
| `llm.system` | "deepseek" |
| `llm.provider` | "deepseek" |
| `llm.model` | Model name (deepseek-chat, deepseek-reasoner, etc.) |
| `llm.token_count.prompt` | Input token count |
| `llm.token_count.completion` | Output token count |
| `llm.token_count.total` | Total token count |

### DeepSeek-Specific Attributes

| Attribute | Description |
|-----------|-------------|
| `deepseek.response_id` | Unique response ID |
| `deepseek.finish_reason` | Response finish reason (stop, tool_calls, length) |
| `deepseek.prompt_cache_hit_tokens` | Tokens served from cache |
| `deepseek.prompt_cache_miss_tokens` | Tokens not in cache |
| `deepseek.reasoning_tokens` | Tokens used for reasoning (R1 models) |
| `deepseek.reasoning_content` | Full reasoning text (R1 models) |
| `deepseek.tool_calls_count` | Number of tool calls |
| `deepseek.tools_count` | Number of tools provided |

## Available Models

| Model | Description |
|-------|-------------|
| `deepseek-chat` | General chat model |
| `deepseek-coder` | Code-focused model |
| `deepseek-reasoner` | Reasoning model (R1) with chain-of-thought |

## Prompt Caching

DeepSeek supports prompt caching to reduce costs and latency for repeated prompts. The instrumentation tracks cache hit/miss metrics:

```python
response = client.chat.completions.create(
    model="deepseek-chat",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},  # Cached
        {"role": "user", "content": "Hello!"}
    ]
)

# Access cache metrics from usage
print(f"Cache hits: {response.usage.prompt_cache_hit_tokens}")
print(f"Cache misses: {response.usage.prompt_cache_miss_tokens}")
```

## Important Notes

1. **OpenAI SDK Required**: DeepSeek uses the OpenAI-compatible API, so you need the `openai` package installed.

2. **Base URL**: Always set `base_url="https://api.deepseek.com/v1"` when creating the client.

3. **API Key**: Get your API key from the [DeepSeek Platform](https://platform.deepseek.com/).

4. **Selective Instrumentation**: The instrumentor only traces calls to DeepSeek's API. Regular OpenAI API calls are not affected.

5. **R1 Models**: When using `deepseek-reasoner`, the `reasoning_content` field contains the model's thought process before the final answer.

## License

Apache-2.0
