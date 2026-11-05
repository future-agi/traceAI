# Google Gen AI OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the Google Gen AI framework. It enables tracing and monitoring of applications built with Google Gen AI.

## Installation

1. **Install traceAI Google Gen AI**

```bash
pip install traceAI-google-genai
```


### Set Environment Variables
Set up your environment variables to authenticate with FutureAGI

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
    project_name="google_genai_project"
)
```

### Configure Google Gen AI Instrumentation
Instrument the Google Gen AI client before making requests. The same instrumentor supports synchronous and asynchronous calls to:

- `models.generate_content` and `models.generate_content_stream`;
- `models.embed_content`, with embedding model, dimensions and vectors;
- `caches.create`, `caches.get`, `caches.update` and `caches.delete`;
- `interactions.create` and `interactions.get`, including streaming, when available in your installed SDK.

Cache operations appear as `CHAIN` spans, not model generations. Their cached token counts are recorded separately from inference usage. Interactions support both the legacy resource classes and the newer NextGen classes. Other SDK resources, image-generation methods and Live sessions are not instrumented by this package. Use the separate Live integration for Live sessions, and do not enable two Google GenAI instrumentors for the same client.

Interactions stream spans stay open until you exhaust the stream or close it. Use `with stream:` or `async with stream:` when stopping early. Stream output contains distinct content/step event fragments rather than a reconstructed model response, with a 64 KiB capture limit; larger content is omitted and marked `google.genai.content_truncated`. Automatic-continuation usage is summed across completed hops.

Request and response content is captured by default. Set `FI_HIDE_INPUTS=true` and `FI_HIDE_OUTPUTS=true` before instrumentation to disable content capture. Set `FI_HIDE_EMBEDDING_VECTORS=true` to omit embedding vectors independently. The new resource spans exclude transport headers and HTTP options; review any custom attributes your application adds separately.

```python
from traceai_google_genai import GoogleGenAIInstrumentor

GoogleGenAIInstrumentor().instrument(tracer_provider=trace_provider)
```

### Make a request
Set up your Google Gen AI client with built-in observability.

```python
from google import genai
from google.genai import types

client = genai.Client(vertexai=True, project="your_project_name", location="global")

content = types.Content(
    role="user",
    parts=[
        types.Part.from_text(text="Hello how are you?"),
    ],
)
response = client.models.generate_content(
    model="gemini-2.0-flash-001", contents=content
)

print(response)
```

