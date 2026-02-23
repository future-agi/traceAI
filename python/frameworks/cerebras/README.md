# TraceAI Cerebras Instrumentation

OpenTelemetry instrumentation for [Cerebras Cloud SDK](https://github.com/Cerebras/cerebras-cloud-sdk-python), enabling comprehensive observability for Cerebras LLM API calls.

## Installation

```bash
pip install traceai-cerebras
```

For full functionality, also install the Cerebras SDK:

```bash
pip install cerebras-cloud-sdk
```

## Quick Start

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_cerebras import CerebrasInstrumentor

# Setup TraceAI
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="cerebras-app",
)

# Instrument Cerebras
CerebrasInstrumentor().instrument(tracer_provider=trace_provider)

# Now use Cerebras normally
from cerebras.cloud.sdk import Cerebras

client = Cerebras()
response = client.chat.completions.create(
    model="llama3.1-8b",
    messages=[{"role": "user", "content": "What is machine learning?"}]
)
print(response.choices[0].message.content)
```

## Features

### Chat Completions Tracing

Automatically captures:
- Model name and parameters
- Input messages
- Output responses
- Token usage (prompt, completion, total)
- Cerebras-specific time_info metrics

### Streaming Support

Full support for streaming responses with:
- Content aggregation
- Chunk-by-chunk tracing
- Final usage metrics from last chunk

### Cerebras-Specific Metrics

Captures Cerebras `time_info` for performance analysis:
- `cerebras.queue_time` - Time spent in queue
- `cerebras.prompt_time` - Time for prompt processing
- `cerebras.completion_time` - Time for completion generation
- `cerebras.total_time` - Total request time

## Span Attributes

### Request Attributes

| Attribute | Description |
|-----------|-------------|
| `gen_ai.system` | Always "cerebras" |
| `gen_ai.request.model` | Requested model (e.g., "llama3.1-8b") |
| `gen_ai.request.max_tokens` | Maximum tokens requested |
| `gen_ai.request.temperature` | Temperature setting |
| `gen_ai.request.top_p` | Top-p sampling parameter |
| `gen_ai.prompt.{i}.role` | Role of input message |
| `gen_ai.prompt.{i}.content` | Content of input message |

### Response Attributes

| Attribute | Description |
|-----------|-------------|
| `gen_ai.response.model` | Model that generated response |
| `gen_ai.completion.{i}.role` | Role of output message |
| `gen_ai.completion.{i}.content` | Content of output message |
| `gen_ai.usage.input_tokens` | Prompt token count |
| `gen_ai.usage.output_tokens` | Completion token count |
| `gen_ai.usage.total_tokens` | Total token count |

### Cerebras-Specific Attributes

| Attribute | Description |
|-----------|-------------|
| `cerebras.queue_time` | Time in queue (seconds) |
| `cerebras.prompt_time` | Prompt processing time (seconds) |
| `cerebras.completion_time` | Completion generation time (seconds) |
| `cerebras.total_time` | Total request time (seconds) |

## Configuration

### Basic Configuration

```python
from traceai_cerebras import CerebrasInstrumentor

CerebrasInstrumentor().instrument()
```

### With Custom Tracer Provider

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_cerebras import CerebrasInstrumentor

provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="my-cerebras-app",
)

CerebrasInstrumentor().instrument(tracer_provider=provider)
```

### With Trace Config

```python
from fi_instrumentation import TraceConfig
from traceai_cerebras import CerebrasInstrumentor

config = TraceConfig(
    mask_input=True,  # Mask input content
    mask_output=True,  # Mask output content
)

CerebrasInstrumentor().instrument(config=config)
```

## Examples

### Basic Chat Completion

```python
from cerebras.cloud.sdk import Cerebras

client = Cerebras()
response = client.chat.completions.create(
    model="llama3.1-8b",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Explain quantum computing."}
    ],
    max_tokens=500,
    temperature=0.7,
)

print(response.choices[0].message.content)
```

### Streaming Response

```python
from cerebras.cloud.sdk import Cerebras

client = Cerebras()
stream = client.chat.completions.create(
    model="llama3.1-8b",
    messages=[{"role": "user", "content": "Write a short story."}],
    stream=True,
)

for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

### Async Usage

```python
import asyncio
from cerebras.cloud.sdk import AsyncCerebras

async def main():
    client = AsyncCerebras()
    response = await client.chat.completions.create(
        model="llama3.1-8b",
        messages=[{"role": "user", "content": "Hello!"}],
    )
    print(response.choices[0].message.content)

asyncio.run(main())
```

## Supported Models

- llama3.1-8b
- llama3.1-70b
- And other Cerebras-hosted models

## Requirements

- Python >= 3.9
- cerebras-cloud-sdk >= 1.0.0
- opentelemetry-api >= 1.0.0
- opentelemetry-sdk >= 1.0.0
- fi-instrumentation >= 0.1.0

## License

Apache-2.0
