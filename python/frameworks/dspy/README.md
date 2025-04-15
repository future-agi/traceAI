# DSPy OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the DSPy framework. It enables tracing and monitoring of applications built with DSPy.

## Installation

1. **Install traceAI DSPy**

```bash
pip install traceAI-DSPy
```

2. **Install DSPy**
```bash
pip install dspy
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
    project_name="dspy_app",
    project_version_name="v1",
    session_name="chat-bot"
)
```

### Configure DSPy Instrumentation
Instrument the DSPy client to enable telemetry collection. This step ensures that all interactions with the DSPy SDK are tracked and monitored.

```python
from traceai_dspy import DSPyInstrumentor

DSPyInstrumentor().instrument(tracer_provider=trace_provider)
```

### Create DSPy Components
Set up your DSPy client with built-in observability.

```python
import dspy

class BasicQA(dspy.Signature):
    """Answer questions with short factoid answers."""

    question = dspy.InputField()
    answer = dspy.OutputField(desc="often between 1 and 5 words")

if __name__ == "__main__":
    turbo = dspy.LM(model="openai/gpt-4")

    dspy.settings.configure(lm=turbo)

    # Define the predictor.
    generate_answer = dspy.Predict(BasicQA)

    # Call the predictor on a particular input.
    pred = generate_answer(question="What is the capital of the united states?")
    print(f"Predicted Answer: {pred.answer}")
```

