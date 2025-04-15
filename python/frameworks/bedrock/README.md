# Bedrock OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the Bedrock framework. It enables tracing and monitoring of applications built with Bedrock.

## Installation

1. **Install traceAI Bedrock**

```bash
pip install traceAI-bedrock
```
2. **Install boto3**

```bash
pip install boto3
```


### Set Environment Variables
Set up your environment variables to authenticate with FutureAGI.

```python
import os

os.environ["AWS_ACCESS_KEY_ID"] = AWS_ACCESS_KEY_ID
os.environ["AWS_SECRET_ACCESS_KEY"] = AWS_SECRET_ACCESS_KEY
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
    project_name="bedrock_app",
    project_version_name="v1",
    session_name="s1"
)
```

### Configure Bedrock Instrumentation
Instrument the Bedrock client to enable telemetry collection. This step ensures that all interactions with the Bedrock SDK are tracked and monitored.

```python
from traceai_bedrock import BedrockInstrumentor

BedrockInstrumentor().instrument(tracer_provider=trace_provider)
```

### Create Bedrock Components
Set up your Bedrock client with built-in observability.

```python
def converse_with_claude():
    system_prompt = [{"text": "You are an expert at creating music playlists"}]
    messages = [
        {
            "role": "user",
            "content": [{"text": "Hello, how are you?"}, {"text": "What's your name?"}],
        }
    ]
    inference_config = {"maxTokens": 1024, "temperature": 0.0}

    try:
        response = client.converse(
            modelId="model_id",
            system=system_prompt,
            messages=messages,
            inferenceConfig=inference_config,
        )
        out = response["output"]["message"]
        messages.append(out)
        print(out)
    except Exception as e:
        print(f"Error: {str(e)}")

if __name__ == "__main__":
    converse_with_claude()
```

