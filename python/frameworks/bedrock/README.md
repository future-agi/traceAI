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

## Trace an existing Bedrock Agents Classic agent

Use a synchronous boto3 `bedrock-agent-runtime` client created **after** calling
`BedrockInstrumentor().instrument(...)`. Set `enableTrace=True` to include the
agent's internal model and tool events. These AWS events are converted into
OpenTelemetry spans; Agents Classic does not send them as native OTLP.

```python
import boto3
import os

client = boto3.client("bedrock-agent-runtime", region_name=os.environ["AWS_REGION"])
response = client.invoke_agent(
    agentId=os.environ["BEDROCK_AGENT_ID"],
    agentAliasId=os.environ["BEDROCK_AGENT_ALIAS_ID"],
    sessionId="travel-planning-session",
    inputText="Plan a three-day trip to Lisbon.",
    enableTrace=True,
)
completion = response["completion"]
try:
    answer = b"".join(
        event["chunk"]["bytes"] for event in completion if "chunk" in event
    )
    print(answer.decode("utf-8"))
finally:
    completion.close()
    trace_provider.force_flush()
```

Use your usual AWS credential chain, an existing agent and alias, and permission
to invoke that agent. Consume the completion stream to record the full answer.
If you stop early, call `completion.close()`; the span then contains only the
output and trace events you consumed. Closing does not drain the remaining events.
Decode assembled bytes rather than individual chunks, because a UTF-8 character
can span chunks. A stream that is neither exhausted nor closed stays open.

This agent route supports synchronous boto3 `invoke_agent`. It does not
instrument `invoke_inline_agent` or async `aiobotocore`/`aioboto3` clients.
Do not enable another instrumentor for the same operations.

For new agent deployments, use [Amazon Bedrock AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html).
[Agents Classic is in maintenance mode](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-classic-maintenance-mode.html),
with no planned end-of-life for existing customers. Bedrock Runtime and Knowledge
Bases remain supported; this package is not deprecated.

