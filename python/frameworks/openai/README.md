# OpenAI OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the OpenAI framework. It enables tracing and monitoring of applications built with OpenAI.

## Installation

1. **Install traceAI OpenAI**

```bash
pip install traceAI-openai
```


### Set Environment Variables
Set up your environment variables to authenticate with FutureAGI

```python
import os

os.environ["FI_API_KEY"] = FI_API_KEY
os.environ["FI_SECRET_KEY"] = FI_SECRET_KEY
os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
```

## Ollama and self-hosted OpenAI-compatible clients

Keep using this package when your application calls Ollama through the OpenAI
SDK. The official `https://ollama.com/v1` endpoint is attributed to `ollama`
automatically. A local address can host many providers, so set the provider
explicitly for each local or self-hosted client:

```python
from openai import OpenAI
from traceai_openai import OpenAIInstrumentor, set_client_provider

# Use the trace provider configured in the quickstart below.
OpenAIInstrumentor().instrument(tracer_provider=trace_provider)

client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
set_client_provider(client, provider="ollama")
```

The override controls `gen_ai.provider.name` on sync and async requests,
including streams, embeddings, Responses and failures. It changes tracing
metadata only; it adds no spans and sends no provider setting to the server.
Use a lowercase provider identifier (1–64 characters, starting with a letter;
letters, digits, `.`, `_` and `-` are accepted), not a URL or secret.

Set the override before making requests. It takes precedence over host detection;
`set_client_provider(client, provider=None)` restores host detection. Other
clients are unaffected. A client returned by `copy()` or `with_options()` is a
separate instance: set its override explicitly too, especially if its endpoint
changes. Unconfigured loopback, proxy and unknown hosts retain the historical
`openai` label; the instrumentor never assumes a local server is Ollama.

For applications using the native `ollama` Python client instead, use
`traceai-ollama`. Do not install both instrumentors for the same API call.
See [Ollama's OpenAI compatibility guide](https://docs.ollama.com/api/openai-compatibility)
for supported endpoints and models.

## Quickstart

### Register Tracer Provider
Set up the trace provider to establish the observability pipeline. The trace provider:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="openai_app"
)
```

### Configure OpenAI Instrumentation
Set up your OpenAI client with built-in observability. This includes support for text, image, and audio models.

```python
from traceai_openai import OpenAIInstrumentor

OpenAIInstrumentor().instrument(tracer_provider=trace_provider)
```

### Create OpenAI Components
Set up your OpenAI client with built-in observability.  

```python
import openai

openai.api_key = os.environ["OPENAI_API_KEY"]

response = openai.ChatCompletion.create(
    model="gpt-4-0",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Can you tell me a joke?"}
    ]
)

print(response.choices[0].message['content'].strip())
```

