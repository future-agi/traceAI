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

## Cloudflare AI Gateway

Use the same `OpenAIInstrumentor` with Cloudflare's [REST API](https://developers.cloudflare.com/ai-gateway/usage/rest-api/) base URL, `https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1`. Existing `/v1/{account_id}/{gateway_id}/compat` clients on `gateway.ai.cloudflare.com` remain supported; Cloudflare recommends the REST API for new integrations.

On these endpoints, documented model prefixes identify the requested provider: `openai/`, `anthropic/`, `google/` or `google-ai-studio/`, and `xai/` or `grok/`. Workers AI's `@cf/` prefix identifies Cloudflare, not the model's author. Dynamic routes (`dynamic/{route}`), unknown prefixes and unsupported Cloudflare paths leave `gen_ai.provider.name` unset rather than guessing. `server.address` (and a non-default `server.port`) retains the Cloudflare endpoint; account IDs, gateway IDs, query strings and URL credentials are not captured as endpoint attributes.

Cloudflare's native gateway export is a separate tracing route. Enabling both produces separate application and gateway spans; this instrumentor does not deduplicate them.

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

