# Python Quickstart

Get started with traceAI in your Python AI application in under 5 minutes.

## Basic Setup

### 1. Install Dependencies

```bash
pip install fi-instrumentation traceai-openai openai
```

### 2. Set Environment Variables

```python
import os

os.environ["FI_API_KEY"] = "your-api-key"
os.environ["FI_SECRET_KEY"] = "your-secret-key"
os.environ["OPENAI_API_KEY"] = "your-openai-key"
```

### 3. Instrument Your Application

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_openai import OpenAIInstrumentor
import openai

# Register tracer provider
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="my_chatbot"
)

# Instrument OpenAI
OpenAIInstrumentor().instrument(tracer_provider=trace_provider)

# Use OpenAI as normal - tracing is automatic!
client = openai.OpenAI()
response = client.chat.completions.create(
    model="gpt-4",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is the capital of France?"}
    ]
)

print(response.choices[0].message.content)
```

That's it! Your OpenAI calls are now being traced.

## Adding Context

Add metadata to help organize and filter your traces:

```python
from fi_instrumentation import using_attributes

with using_attributes(
    session_id="session-123",
    user_id="user-456",
    metadata={"feature": "chat", "version": "1.0"},
    tags=["production", "chatbot"]
):
    response = client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": "Hello!"}]
    )
```

## Multiple Frameworks

Instrument multiple frameworks in the same application:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_openai import OpenAIInstrumentor
from traceai_langchain import LangChainInstrumentor
from traceai_pinecone import PineconeInstrumentor

# Register once
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="rag_app"
)

# Instrument all frameworks
OpenAIInstrumentor().instrument(tracer_provider=trace_provider)
LangChainInstrumentor().instrument(tracer_provider=trace_provider)
PineconeInstrumentor().instrument(tracer_provider=trace_provider)
```

## Async Support

traceAI fully supports async applications:

```python
import asyncio
from openai import AsyncOpenAI

async def main():
    client = AsyncOpenAI()

    response = await client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": "Hello!"}]
    )

    print(response.choices[0].message.content)

asyncio.run(main())
```

## Streaming Support

Streaming responses are automatically traced:

```python
stream = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "Tell me a story."}],
    stream=True
)

for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

## Running Experiments

Use `ProjectType.EXPERIMENT` to run AI evaluations:

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    ProjectType, EvalTag, EvalTagType,
    EvalSpanKind, EvalName, ModelChoices
)

# Define evaluations
eval_tags = [
    EvalTag(
        type=EvalTagType.OBSERVATION_SPAN,
        value=EvalSpanKind.LLM,
        eval_name=EvalName.TOXICITY,
        custom_eval_name="toxicity_check",
        mapping={"output": "raw.output"},
        model=ModelChoices.TURING_SMALL
    ),
    EvalTag(
        type=EvalTagType.OBSERVATION_SPAN,
        value=EvalSpanKind.LLM,
        eval_name=EvalName.CONTEXT_ADHERENCE,
        custom_eval_name="adherence_check",
        mapping={
            "context": "raw.input",
            "output": "raw.output"
        },
        model=ModelChoices.TURING_SMALL
    )
]

# Register with experiments
trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    project_name="my_experiment",
    project_version_name="v1.0",
    eval_tags=eval_tags
)

OpenAIInstrumentor().instrument(tracer_provider=trace_provider)
```

## Privacy Controls

Hide sensitive data from traces:

```python
# Via environment variables
os.environ["FI_HIDE_INPUTS"] = "true"
os.environ["FI_HIDE_OUTPUTS"] = "true"

# Or programmatically
from fi_instrumentation.instrumentation.config import TraceConfig

config = TraceConfig(
    hide_inputs=True,
    hide_outputs=True
)
```

## Next Steps

- [TraceConfig Reference](../configuration/trace-config.md) - Privacy settings
- [Context Managers](../python/context-managers.md) - Adding metadata
- [Evaluation Tags](../configuration/eval-tags.md) - AI evaluations
- [OpenAI Examples](../examples/python/basic-openai.md) - More examples
