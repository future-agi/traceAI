# TraceAI Fireworks AI Instrumentation

OpenTelemetry instrumentation for Fireworks AI, enabling comprehensive observability for LLM API calls.

## Installation

```bash
pip install traceai-fireworks
```

## Quick Start

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_fireworks import FireworksInstrumentor

# Setup TraceAI
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="my-fireworks-app",
)

# Instrument Fireworks
FireworksInstrumentor().instrument(tracer_provider=trace_provider)

# Now use OpenAI client with Fireworks
from openai import OpenAI

client = OpenAI(
    api_key="your-fireworks-api-key",
    base_url="https://api.fireworks.ai/inference/v1",
)

response = client.chat.completions.create(
    model="accounts/fireworks/models/llama-v3p1-8b-instruct",
    messages=[{"role": "user", "content": "Hello!"}]
)
```

## Features

- Automatic tracing of Fireworks AI API calls
- Support for both synchronous and asynchronous operations
- Streaming response support
- Token usage tracking
- Request/response attribute capture
- OpenTelemetry semantic conventions for GenAI

## Captured Attributes

- `gen_ai.request.model` - Model name
- `gen_ai.request.max_tokens` - Maximum tokens
- `gen_ai.request.temperature` - Temperature setting
- `gen_ai.usage.input_tokens` - Input token count
- `gen_ai.usage.output_tokens` - Output token count
- `gen_ai.prompt.{n}.role` - Message roles
- `gen_ai.prompt.{n}.content` - Message contents
- `gen_ai.completion.{n}.content` - Response content

## Requirements

- Python >= 3.9
- openai >= 1.0.0
- fi-instrumentation >= 0.1.0
- opentelemetry-api >= 1.0.0
- opentelemetry-sdk >= 1.0.0

## License

Apache-2.0
