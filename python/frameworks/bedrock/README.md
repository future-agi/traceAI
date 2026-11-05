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
    project_name="bedrock_app"
)
```

### Configure Bedrock Instrumentation
Call the instrumentor before creating your boto3 clients to trace supported Bedrock model, retrieval, and agent operations.

```python
from traceai_bedrock import BedrockInstrumentor

BedrockInstrumentor().instrument(tracer_provider=trace_provider)
```

### Stream with Converse (Python)

Use `client.converse_stream(...)` with the same arguments as `client.converse(...)`.
The span records the response as you consume `response["stream"]`, including text,
tool calls, stop reason, and token usage when returned by Bedrock. Consume the
whole stream: usage arrives after `messageStop` in a metadata event.

```python
import os
import boto3

client = boto3.client("bedrock-runtime", region_name=os.environ["AWS_REGION"])
response = client.converse_stream(
    modelId=os.environ["BEDROCK_MODEL_ID"],
    messages=[{"role": "user", "content": [{"text": "Suggest three places to visit in Lisbon."}]}],
)
stream = response["stream"]
try:
    for event in stream:
        text = event.get("contentBlockDelta", {}).get("delta", {}).get("text")
        if text:
            print(text, end="", flush=True)
finally:
    stream.close()
    trace_provider.force_flush()
```

Create `client` as a boto3 `bedrock-runtime` client after instrumenting, with your
AWS region and usual credential chain. If you stop early, call `stream.close()`;
the span contains only consumed output and usage, and closing never drains the
response. A stream that is neither consumed nor closed remains open. Request and
stream exceptions propagate unchanged.

For async applications, install `aiobotocore` (tested from 2.23.0) and create its
client after calling the same `BedrockInstrumentor().instrument(...)`. Await
`client.converse(...)` or `client.converse_stream(...)`, and consume streaming
responses with `async for event in response["stream"]`. The aiobotocore stream's
`close()` is synchronous. No second instrumentor is needed.

Async coverage is limited to these two Runtime methods. `invoke_inline_agent`
and `apply_guardrail` are not instrumented. Do not enable another Bedrock
instrumentor for the same client operations. This package requires
`fi-instrumentation-otel>=1.2.2` so content-hiding settings also protect exception
details. Pass `config=TraceConfig(hide_inputs=True, hide_outputs=True)` to the
instrumentor to hide prompt, response, and tool-argument content.

### Create Bedrock Components
Set up your Bedrock client with built-in observability.

```python
import os
import boto3

client = boto3.client("bedrock-runtime", region_name=os.environ["AWS_REGION"])

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
            modelId=os.environ["BEDROCK_MODEL_ID"],
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

