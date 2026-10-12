# Microsoft Agent Framework OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with Microsoft Agent Framework. It enables tracing and monitoring of applications built with Agent Framework.

## Installation

1. **Install traceAI Agent Framework**

```bash
pip install traceAI-agent-framework
```

2. **Install Microsoft Agent Framework**

```bash
pip install agent-framework
```


### Set Environment Variables
Set up your environment variables to authenticate with FutureAGI

```python
import os

os.environ["FI_API_KEY"] = FI_API_KEY
os.environ["FI_SECRET_KEY"] = FI_SECRET_KEY
os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
```

## Quickstart

### Register Tracer Provider
Set up the trace provider to establish the observability pipeline. The trace provider:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="agent_framework_app",
    set_global_tracer_provider=True,
)
```

### Configure Agent Framework Instrumentation
Turn on Agent Framework's native OpenTelemetry emission and install the FI attribute mapping.

```python
from agent_framework.observability import enable_instrumentation
from traceai_agent_framework import enable_fi_attribute_mapping

enable_instrumentation(enable_sensitive_data=True)
enable_fi_attribute_mapping()
```

`enable_sensitive_data=True` records prompts, completions and tool arguments on the spans. Leave it off to keep that content out of your traces.

Agent Framework emits on the global tracer provider only, so keep `set_global_tracer_provider=True` in `register(...)`.

### Create Agent Framework Components
Set up your Agent Framework client with built-in observability.

```python
import asyncio
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient

agent = Agent(
    OpenAIChatClient(model="gpt-4o-mini"),
    name="weather_agent",
    instructions="You are a concise weather assistant.",
)

async def main():
    response = await agent.run("What's the weather in Paris?")
    print(response)

asyncio.run(main())
```

### Sessions
All spans of one run get the same `session.id`, which Observe groups sessions on. It comes from, in order:

1. `using_session(...)` around the run. This works with every chat client, including ones that keep history locally such as `OpenAIChatCompletionClient`:

```python
from fi_instrumentation import using_session

session = agent.create_session()
with using_session("support-chat-42"):
    await agent.run("What's the weather in Paris?", session=session)
    await agent.run("And in Tokyo?", session=session)
```

2. The `gen_ai.conversation.id` the run starts with, which is the session's `service_session_id`. With the Responses API and no stored conversation, that id is the previous response id, so each turn lands in its own session. Use `using_session` for one session across turns.

