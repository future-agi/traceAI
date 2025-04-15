# Instructor OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the Instructor framework. It enables tracing and monitoring of applications built with Instructor.

## Installation

1. **Install traceAI Instructor**

```bash
pip install traceAI-instructor
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
    project_name="instructor_app",
    project_version_name="v1",
    session_name="chat-bot"
)
```

### Configure Instructor Instrumentation
Instrument the Instructor client to enable telemetry collection. This step ensures that all interactions with the Instructor SDK are tracked and monitored.

```python
from traceai_instructor import InstructorInstrumentor

InstructorInstrumentor().instrument(tracer_provider=trace_provider)
```

### Create Instructor Components
Set up your Instructor client with built-in observability.

```python
import instructor
from openai import OpenAI
from pydantic import BaseModel

# Define the output structure
class UserInfo(BaseModel):
    name: str
    age: int

# Patch the OpenAI client
client = instructor.patch(client=OpenAI())

user_info = client.chat.completions.create(
    model="gpt-3.5-turbo",
    response_model=UserInfo,
    messages=[
        {
            "role": "system",
            "content": "Extract the name and age from the text and return them in a structured format.",
        },
        {"role": "user", "content": "John Doe is nine years old."},
    ],
)

print(user_info, type(user_info))
```

