# MistralAI OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the MistralAI framework. It enables tracing and monitoring of applications built with MistralAI.

## Installation

1. **Install traceAI MistralAI**

```bash
pip install traceAI-mistralai
```


### Set Environment Variables
Set up your environment variables to authenticate with both FutureAGI.

```python
import os

os.environ["FI_API_KEY"] = FI_API_KEY
os.environ["FI_SECRET_KEY"] = FI_SECRET_KEY
os.environ["MISTRAL_API_KEY"] = MISTRAL_API_KEY
```

## Quickstart

### Register Tracer Provider
Set up the trace provider to establish the observability pipeline. The trace provider:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="mistralai_app",
    project_version_name="v1",
    session_name="chat-bot"
)
```

### Configure MistralAI Instrumentation
Instrument the MistralAI client to enable telemetry collection. This step ensures that all interactions with the MistralAI SDK are tracked and monitored.

```python
from traceai_mistralai import MistralAIInstrumentor

MistralAIInstrumentor().instrument(tracer_provider=trace_provider)
```

### Create MistralAI Components
Set up your MistralAI client with built-in observability.

```python
from mistralai import Mistral

client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])

if __name__ == "__main__":
    response = client.agents.complete(
        agent_id="agent_id",
        messages=[
            {"role": "user", "content": "plan a vacation for me in Tbilisi"},
        ],
    )
    print(response)
```

