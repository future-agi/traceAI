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
Set up your environment variables to authenticate with both FutureAGI.

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
    project_name="crewai_app",
    project_version_name="v1",
    session_name="chat-bot"
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

