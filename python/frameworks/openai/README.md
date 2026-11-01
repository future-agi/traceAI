# OpenAI OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the OpenAI framework. It enables tracing and monitoring of applications built with OpenAI.

## Installation

1. **Install traceAI OpenAI**

```bash
pip install traceAI-openai
```


### Set environment variables
Set the Future AGI keys for tracing and the OpenAI key for the Quickstart request:

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
    project_name="openai_app"
)
```

### Configure OpenAI Instrumentation
Set up your OpenAI client with built-in observability. This includes support for text, image, and audio models.

```python
from traceai_openai import OpenAIInstrumentor

OpenAIInstrumentor().instrument(tracer_provider=trace_provider)
```

### Make a traced request
Create the client after calling `OpenAIInstrumentor().instrument(...)`.

```python
from openai import OpenAI

client = OpenAI()
response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Tell me a short joke."}],
)
print(response.choices[0].message.content)
trace_provider.force_flush()  # Flush before a short-lived script exits.
```

## Vercel AI Gateway

Use the same `OpenAIInstrumentor` for Chat Completions sent to Vercel AI Gateway;
no gateway-specific package or OpenAI key is needed. Set `AI_GATEWAY_API_KEY` to your Vercel
AI Gateway key and `AI_GATEWAY_MODEL` to a current
[model ID](https://vercel.com/ai-gateway/models):

```bash
export AI_GATEWAY_API_KEY="your-vercel-ai-gateway-key"
export AI_GATEWAY_MODEL="<provider>/<model>"
```

Keep the tracer registration and instrumentation from the Quickstart above,
then replace its OpenAI client and request with:

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="https://ai-gateway.vercel.sh/v1",
    api_key=os.environ["AI_GATEWAY_API_KEY"],
)
response = client.chat.completions.create(
    model=os.environ["AI_GATEWAY_MODEL"],
    messages=[{"role": "user", "content": "Say hello in five words."}],
)
print(response.choices[0].message.content)
trace_provider.force_flush()  # Flush before a short-lived script exits.
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

