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

### Make a traced request
Create the client after calling `OpenAIInstrumentor().instrument(...)`.

```python
from openai import OpenAI

# OpenAI reads OPENAI_API_KEY from your environment.
with OpenAI() as client:
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Can you tell me a joke?"},
        ],
    )
    print((response.choices[0].message.content or "").strip())
trace_provider.force_flush()  # Flush before a short-lived script exits.
```

## Azure OpenAI

Use the same `traceAI-openai` package and `OpenAIInstrumentor` for Azure OpenAI.
Keep the tracer registration and instrumentation above, and replace the OpenAI
client and request with one of the following examples. Do not enable a second
OpenAI instrumentor for the same calls.

Set your Azure resource key and **deployment name** (not the underlying model ID).
You do not need an OpenAI API key:

```bash
export AZURE_OPENAI_API_KEY="your-azure-resource-key"
export AZURE_OPENAI_DEPLOYMENT="your-deployment-name"
```

### v1 API

For a resource that supports the [Azure OpenAI v1 API](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/api-version-lifecycle),
set its complete base URL, including `/openai/v1/`:

```bash
export AZURE_OPENAI_BASE_URL="https://your-resource.openai.azure.com/openai/v1/"
```

```python
import os
from openai import OpenAI

with OpenAI(
    base_url=os.environ["AZURE_OPENAI_BASE_URL"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
) as client:
    response = client.chat.completions.create(
        model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        messages=[{"role": "user", "content": "Say hello in five words."}],
    )
    print(response.choices[0].message.content)
trace_provider.force_flush()
```

### Versioned Azure API

For an existing `AzureOpenAI` application, keep your resource endpoint and an
API version supported by your deployment. Public Azure resources use
`https://your-resource.openai.azure.com`; [Azure Government](https://learn.microsoft.com/en-us/azure/azure-government/compare-azure-government-global-azure)
resources use `https://your-resource.openai.azure.us`:

```bash
export AZURE_OPENAI_ENDPOINT="https://your-resource.openai.azure.us"
export AZURE_OPENAI_API_VERSION="<supported-api-version>"
```

```python
import os
from openai import AzureOpenAI

with AzureOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_version=os.environ["AZURE_OPENAI_API_VERSION"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
) as client:
    response = client.chat.completions.create(
        model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        messages=[{"role": "user", "content": "Say hello in five words."}],
    )
    print(response.choices[0].message.content)
trace_provider.force_flush()
```

Both clients use the existing instrumentation, including async clients and
streaming. Spans from resource hosts under `openai.azure.com` and
`openai.azure.us` are labelled `gen_ai.provider.name="azure"`; custom gateways
retain their existing provider label. If you use Microsoft Entra ID, keep the
SDK token-provider configuration for your cloud instead of the API-key setup.

Azure Government does not change the trace export destination. Configure an
approved Future AGI destination and content settings before tracing sensitive
workloads; prompts and responses are captured by default.

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

