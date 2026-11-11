# OpenAI Agents OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the OpenAI Agents framework. It enables tracing and monitoring of applications built with OpenAI Agents.

## Installation

1. **Install traceAI OpenAI Agents**

```bash
pip install traceAI-openai-agents
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
    project_name="openai_agents_app"
)
```

### Configure OpenAI Agents Instrumentation
Instrument the OpenAI Agents client to enable telemetry collection. This step ensures that all interactions with the OpenAI Agents SDK are tracked and monitored.

```python
from traceai_openai_agents import OpenAIAgentsInstrumentor

OpenAIAgentsInstrumentor().instrument(tracer_provider=trace_provider)
```

### Create OpenAI Agents Components
Set up your OpenAI Agents client with built-in observability.

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")
result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")

print(result.final_output)

```

## Tool media and computer-use screenshots (attachments)

Pass the shared manager: `OpenAIAgentsInstrumentor().instrument(tracer_provider=..., attachments=manager)`
(`traceai.init` passes it for you). On the tool's function span, before
`output.value` is written:

- the computer tool's screenshot (`computer_screenshot`), which the SDK records
  on the `computer` function span from openai-agents 0.10.0;
- `ToolOutputImage` / `{"type": "image", "image_url": "data:..."}` and
  `ToolOutputFileContent` with `file_data` (openai-agents 0.4.0+; MCP image
  results become these from 0.8.0), as `tool_result`.

Admitted values become `fi-attachment:<occurrence_id>` in `output.value`.
Nothing is recorded or uploaded when `RunConfig.trace_include_sensitive_data`
is off. Model-call input media is left to the OpenAI instrumentor's own span:
with both enabled, a screenshot is uploaded once as the tool output and once
as the next model input.

Attachments are off unless the provider's attachment manager is enabled
(`fi_instrumentation.register(..., attachment_config=AttachmentConfig(enabled=True))`
or `traceai.init(attachment_config=...)`). Tool media is tool output and
potential model input, so every input and output hide flag
(`hide_inputs`, `hide_outputs` and their message/text variants),
`hide_input_images` for images, and `pii_redaction` deny the upload; they
cannot be relaxed by the server or the core. Only typed inline bytes are
read, and only after the manager admitted them; URLs, file IDs, resource URIs
and local paths are never fetched or opened. Call the manager's `flush()`
before a short process exits. Upload completion means "transferred for
validation"; playback needs an authorized occurrence through the media
gateway.
