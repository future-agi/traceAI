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

## Vercel AI Gateway

Use the same `OpenAIInstrumentor` for Chat Completions sent to Vercel AI Gateway;
no gateway-specific instrumentation package is needed. After registering and
instrumenting the tracer as below, create your client with:

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="https://ai-gateway.vercel.sh/v1",
    api_key=os.environ["AI_GATEWAY_API_KEY"],
)
```

The documented gateway host is labelled `gen_ai.provider.name="vercel"`.
This identifies the service the client calls, **not** the upstream model provider
or a fallback provider. Unknown proxies and look-alike hosts retain their existing
provider label. Sync, async, streamed and failed calls use the same attribution.

Client spans capture prompts, responses and returned token usage. They do not
reconstruct gateway routing, retries, cost or provider attempts. Vercel's separate
[Trace Drains](https://vercel.com/docs/ai-gateway/observability-and-spend/trace-drains)
provide those gateway details but omit prompts and completions. A drain is not
configured by `OpenAIInstrumentor`; do not sum client and drain token counts for
the same request as separate model usage.

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

