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
Instrument the Google Gen AI client to enable telemetry collection. This step ensures that all interactions with the Google Gen AI SDK are tracked and monitored.

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

