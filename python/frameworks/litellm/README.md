# Litellm OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the Litellm framework. It enables tracing and monitoring of applications built with Litellm.

## Installation

1. **Install traceAI Litellm**

```bash
pip install pip install traceAI-litellm
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
    project_name="litellm_app",
    project_version_name="v1",
    session_name="chat-bot"
)
```

### Configure Litellm Instrumentation
Instrument the Litellm client to enable telemetry collection. This step ensures that all interactions with the Litellm SDK are tracked and monitored.

```python
from traceai_litellm import LiteLLMInstrumentor

LiteLLMInstrumentor().instrument(tracer_provider=trace_provider)
```

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

