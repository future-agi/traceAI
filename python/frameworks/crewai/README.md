# Crewai OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the Crewai framework. It enables tracing and monitoring of applications built with Crewai.

## Installation

1. **Install traceAI Crewai**

```bash
pip install traceAI-crewai
```

2. **Install Crewai and Crewai_tools**

```bash
pip install crewai crewai_tools
```


### Set Environment Variables
Set up your environment variables to authenticate with FutureAGI

```python
import os

os.environ["FI_API_KEY"] = FI_API_KEY
os.environ["FI_SECRET_KEY"] = FI_SECRET_KEY

# Set these before importing CrewAI to opt out of its separate native telemetry.
os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"
os.environ["CREWAI_DISABLE_TRACKING"] = "true"
```

### Privacy and credentials

Task, crew and tool spans project business content rather than serializing agent,
LLM, client or tool configuration objects. Credential-named fields in structured
inputs and outputs, including complete JSON object/array strings, are redacted
by default. This includes nested `client.config.api_key`, API-key headers,
authorization, access/refresh tokens, secrets and passwords. Model/provider
identifiers, usage counts and unrelated business values remain visible. Tracing
does not change your inputs, tool results or raised exceptions.

This is not arbitrary free-text or unknown-encoding secret detection. If prose
may contain secrets, set `FI_HIDE_INPUTS=true` and/or `FI_HIDE_OUTPUTS=true`
before instrumentation, or pass the corresponding `TraceConfig` settings.
Directional text/message hiding also covers CrewAI's content aliases. Automatic
wrapper failures record an ERROR status without copying exception messages or
stacktraces. For manually recorded error events, use `FI_HIDE_ERROR_DETAILS=true`.
This integration does not filter your application's own console output or logs.

Call `instrument()` before constructing agents, tools or crews. While
instrumented, traceAI disables CrewAI's separate native telemetry, including the
legacy exporter in CrewAI 0.41.1, which ignores the environment flags above.
`uninstrument()` restores CrewAI's native behavior. This does not retract any
telemetry already queued before instrumentation. Do not set
`OTEL_SDK_DISABLED=true`: that also disables the traceAI spans you want to collect.

## Quickstart

### Register Tracer Provider
Set up the trace provider to establish the observability pipeline. The trace provider:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="crewai_app"
)
```

### Configure Crewai Instrumentation
Instrument the Crewai client to enable telemetry collection. This step ensures that all interactions with the Crewai SDK are tracked and monitored.

```python
from traceai_crewai import CrewAIInstrumentor

CrewAIInstrumentor().instrument(tracer_provider=trace_provider)
```

### Create Crewai Components
Set up your Crewai client with built-in observability.

```python
from crewai import LLM, Agent, Crew, Process, Task
from crewai_tools import SerperDevTool

def story_example():
    llm = LLM(
        model="gpt-4",
        temperature=0.8,
        max_tokens=150,
        top_p=0.9,
        frequency_penalty=0.1,
        presence_penalty=0.1,
        stop=["END"],
        seed=42,
    )

    writer = Agent(
        role="Writer",
        goal="Write creative stories",
        backstory="You are a creative writer with a passion for storytelling",
        allow_delegation=False,
        llm=llm,
    )

    writing_task = Task(
        description="Write a short story about a magical forest",
        agent=writer,
        expected_output="A short story about a magical forest",
    )

    crew = Crew(agents=[writer], tasks=[writing_task])

    # Execute the crew
    result = crew.kickoff()
    print(result)

if __name__ == "__main__":
    story_example()
```

