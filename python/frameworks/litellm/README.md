# Litellm OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the Litellm framework. It enables tracing and monitoring of applications built with Litellm.

## Installation

1. **Install traceAI Litellm**

```bash
pip install traceAI-litellm
```


### Set environment variables

Set your Future AGI credentials for telemetry and your own OpenAI API key for the examples below. Future AGI credentials do not authenticate model inference; OpenAI bills inference to your OpenAI account. When you use another provider through LiteLLM, configure that provider's credentials instead.

```bash
export FI_API_KEY="your-future-agi-api-key"
export FI_SECRET_KEY="your-future-agi-secret-key"
export OPENAI_API_KEY="your-openai-api-key"
```

## Quickstart

### Register Tracer Provider
Set up the trace provider to establish the observability pipeline. The trace provider:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="litellm_app"
)
```

### Configure Litellm Instrumentation
Instrument the LiteLLM client to trace `completion` / `acompletion`, `completion_with_retries`, `embedding` / `aembedding`, and `image_generation` / `aimage_generation`. On LiteLLM versions that expose them, `responses` / `aresponses` are also traced. Completion and Responses streams are traced as you consume them; other SDK endpoints are not covered.

Exhaust streams or explicitly call `close()` / `await aclose()` to finish telemetry and release provider resources promptly. Discarding a stream finalizes its span on garbage collection, but does not guarantee immediate transport cleanup.

```python
from traceai_litellm import LiteLLMInstrumentor

LiteLLMInstrumentor().instrument(tracer_provider=trace_provider)
```

Use one tracing integration for each LiteLLM call. Do not also enable LiteLLM's native OpenTelemetry callback for calls traced by this instrumentor.

### Create Litellm Components
Set up your Litellm client with built-in observability.

```python
import asyncio
import litellm

async def run_examples():
    # Simple single message completion call
    litellm.completion(
        model="gpt-3.5-turbo",
        messages=[{"content": "What's the capital of China?", "role": "user"}],
    )

    # Multiple message conversation completion call with added param
    litellm.completion(
        model="gpt-3.5-turbo",
        messages=[
            {"content": "Hello, I want to bake a cake", "role": "user"},
            {
                "content": "Hello, I can pull up some recipes for cakes.",
                "role": "assistant",
            },
            {"content": "No actually I want to make a pie", "role": "user"},
        ],
        temperature=0.7,
    )

    # Multiple message conversation acompletion call with added params
    await litellm.acompletion(
        model="gpt-3.5-turbo",
        messages=[
            {"content": "Hello, I want to bake a cake", "role": "user"},
            {
                "content": "Hello, I can pull up some recipes for cakes.",
                "role": "assistant",
            },
            {"content": "No actually I want to make a pie", "role": "user"},
        ],
        temperature=0.7,
        max_tokens=20,
    )

    # Completion with retries
    litellm.completion_with_retries(
        model="gpt-3.5-turbo",
        messages=[{"content": "What's the highest grossing film ever", "role": "user"}],
    )

    # Embedding call
    litellm.embedding(
        model="text-embedding-ada-002", input=["good morning from litellm"]
    )

    # Asynchronous embedding call
    await litellm.aembedding(
        model="text-embedding-ada-002", input=["good morning from litellm"]
    )

    # Image generation call
    litellm.image_generation(model="dall-e-2", prompt="cute baby otter")

    # Asynchronous image generation call
    await litellm.aimage_generation(model="dall-e-2", prompt="cute baby otter")

asyncio.run(run_examples())
```

