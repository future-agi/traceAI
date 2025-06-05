# MCP OpenTelemetry Integration

## Overview
This integration provides support for using OpenTelemetry with the MCP framework. It enables tracing and monitoring of applications built with MCP.

## Installation

1. **Install traceAI MCP**

```bash
pip install traceAI-mcp traceAI-openai
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
    project_name="my-mcp-project"
)
```

### Configure MCP Instrumentation
Set up your MCP Instrumentation with built-in observability.

```python
from traceai_openai import OpenAIInstrumentor
from traceai_mcp import MCPInstrumentor

OpenAIInstrumentor().instrument(tracer_provider=trace_provider)
MCPInstrumentor().instrument(tracer_provider=tracer_provider)
```

Start your MCP server as usual. TraceAI will automatically trace all MCP requests which will be visible in the FutureAGI platform.