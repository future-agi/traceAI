# LangChain OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the LangChain framework. It enables tracing and monitoring of applications built with LangChain.

## Installation

1. **Install traceAI LangChain**

```bash
pip install traceAI-langchain
```

2. **Install LangChain OpenAI**

```bash
pip install langchain-openai
```


### Set Environment Variables
Set up your environment variables to authenticate with FutureAGI.

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
    project_name="langchain_app",
    project_version_name="v1",  
    session_name="chat-bot"
)
```

### Configure LangChain Instrumentation
Instrument the LangChain client to enable telemetry collection. This step ensures that all interactions with the LangChain SDK are tracked and monitored.

```python
from traceai_langchain import LangChainInstrumentor

LangChainInstrumentor().instrument(tracer_provider=trace_provider)
```

### Create LangChain Components
Set up your LangChain client with built-in observability.

```python
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

prompt = ChatPromptTemplate.from_template("{x} {y} {z}?").partial(x="why is", z="blue")
chain = prompt | ChatOpenAI(model_name="gpt-3.5-turbo")

def run_chain():
    try:
        result = chain.invoke({"y": "sky"})
        print(f"Response: {result}")
    except Exception as e:
        print(f"Error executing chain: {e}")

if __name__ == "__main__":
    run_chain()
```

