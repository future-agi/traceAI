# HuggingFace Smolagents OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the HuggingFace Smolagents framework. It enables tracing and monitoring of applications built with HuggingFace Smolagents.

## Installation

1. **Install traceAI Smolagents**

```bash
pip install traceAI-smolagents
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
    project_name="smolagents_app"
)
```

### Configure Smolagents Instrumentation
Instrument the Smolagents client to enable telemetry collection.

```python
from traceai_smolagents import SmolagentsInstrumentor

SmolagentsInstrumentor().instrument(tracer_provider=trace_provider)
```

### Create Smolagents Components
Set up your Smolagents client with built-in observability.

```python
from smolagents import (
    CodeAgent,
    DuckDuckGoSearchTool,
    OpenAIServerModel,
    ToolCallingAgent,
)

model = OpenAIServerModel(model_id="gpt-4o")
agent = ToolCallingAgent(
    tools=[DuckDuckGoSearchTool()],
    model=model,
    max_steps=3,
    name="search",
    description=(
        "This is an agent that can do web search. "
        "When solving a task, ask him directly first, he gives good answers. "
        "Then you can double check."
    ),
)
manager_agent = CodeAgent(
    tools=[DuckDuckGoSearchTool()],
    model=model,
    managed_agents=[agent],
)
manager_agent.run(
    "How many seconds would it take for a leopard at full speed to run through Pont des Arts? "
    "ASK YOUR MANAGED AGENT FOR LEOPARD SPEED FIRST"
)
```

## Image observations (attachments)

Pass the shared manager: `SmolagentsInstrumentor().instrument(tracer_provider=..., attachments=manager)`.

- Images smolagents hands to a model (`{"type": "image", "image": <PIL image>}`
  blocks: task images and `ActionStep.observations_images`, such as a browser
  screenshot a step callback stored) are uploaded on the model span as
  `model_input`, once per image object (memory replays are not re-uploaded).
  When the model span captured an image, provider instrumentors below it are
  told the call's media is already owned.
- A tool's returned in-memory PIL image or `AgentImage` is uploaded on the
  tool span as `tool_result`.

Images are PNG-encoded only after the manager admitted them, up to
2048 x 2048 pixels. File-backed images that are not loaded yet, image URLs
and every `AgentAudio` are never read.

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
