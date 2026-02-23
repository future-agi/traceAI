# TraceAI vLLM Instrumentation

OpenTelemetry instrumentation for vLLM, enabling comprehensive observability for local LLM inference.

## Installation

```bash
pip install traceai-vllm
```

## Quick Start

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_vllm import VLLMInstrumentor

# Setup TraceAI
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="my-vllm-app",
)

# Instrument vLLM (specify your server URLs)
VLLMInstrumentor(
    vllm_base_urls=["localhost:8000"]  # Your vLLM server(s)
).instrument(tracer_provider=trace_provider)

# Now use OpenAI client with vLLM
from openai import OpenAI

client = OpenAI(
    api_key="token",  # vLLM doesn't require a real API key
    base_url="http://localhost:8000/v1",
)

response = client.chat.completions.create(
    model="meta-llama/Llama-2-7b-chat-hf",
    messages=[{"role": "user", "content": "Hello!"}]
)
```

## Configuration

### Custom vLLM Server URLs

You can specify multiple vLLM server URLs to instrument:

```python
VLLMInstrumentor(
    vllm_base_urls=[
        "localhost:8000",
        "production-vllm.internal:8000",
        "staging-vllm.internal:8080",
    ]
).instrument(tracer_provider=trace_provider)
```

## Features

- Automatic tracing of vLLM API calls
- Support for both synchronous and asynchronous operations
- Streaming response support
- Token usage tracking
- Request/response attribute capture
- Support for multiple vLLM server endpoints
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

## vLLM-Specific Parameters

The instrumentation also captures vLLM-specific parameters when provided:

- `best_of` - Number of output sequences to generate
- `use_beam_search` - Whether to use beam search

## Requirements

- Python >= 3.9
- openai >= 1.0.0
- fi-instrumentation >= 0.1.0
- opentelemetry-api >= 1.0.0
- opentelemetry-sdk >= 1.0.0

## Running vLLM Server

To use this instrumentation, you need a running vLLM server:

```bash
# Install vLLM
pip install vllm

# Start the server
python -m vllm.entrypoints.openai.api_server \
    --model meta-llama/Llama-2-7b-chat-hf \
    --port 8000
```

## License

Apache-2.0
