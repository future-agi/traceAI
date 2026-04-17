# Azure OpenAI OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with Azure OpenAI. It enables tracing and monitoring of applications built with Azure OpenAI, capturing chat completions, embeddings, and completions with Azure-specific attributes such as deployment name and API version.

## Installation

1. **Install traceAI Azure OpenAI**

```bash
pip install traceAI-azure-openai
```

### Set Environment Variables
Set up your environment variables to authenticate with FutureAGI and Azure OpenAI

```python
import os

os.environ["FI_API_KEY"] = FI_API_KEY
os.environ["FI_SECRET_KEY"] = FI_SECRET_KEY
os.environ["AZURE_OPENAI_ENDPOINT"] = AZURE_OPENAI_ENDPOINT
os.environ["AZURE_OPENAI_API_KEY"] = AZURE_OPENAI_API_KEY
os.environ["AZURE_OPENAI_DEPLOYMENT"] = AZURE_OPENAI_DEPLOYMENT
os.environ["AZURE_OPENAI_API_VERSION"] = AZURE_OPENAI_API_VERSION
```

## Quickstart

### Register Tracer Provider
Set up the trace provider to establish the observability pipeline. The trace provider:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="azure_openai_app"
)
```

### Configure Azure OpenAI Instrumentation
Set up your Azure OpenAI client with built-in observability.

```python
from traceai_azure_openai import AzureOpenAIInstrumentor

AzureOpenAIInstrumentor().instrument(tracer_provider=trace_provider)
```

### Create Azure OpenAI Components
Set up your Azure OpenAI client with built-in observability.

```python
from openai import AzureOpenAI

client = AzureOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version=os.environ["AZURE_OPENAI_API_VERSION"],
)

response = client.chat.completions.create(
    model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Can you tell me a joke?"}
    ]
)

print(response.choices[0].message.content)
```

## Azure-Specific Attributes

This instrumentation captures the following Azure-specific span attributes in addition to the standard GenAI semantic conventions:

| Attribute | Description |
|-----------|-------------|
| `gen_ai.provider.name` | Set to `azure` |
| `gen_ai.azure.deployment` | The Azure OpenAI deployment name |
| `gen_ai.azure.api_version` | The Azure OpenAI API version |
| `server.address` | The Azure OpenAI endpoint hostname |
