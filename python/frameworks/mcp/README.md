# MCP Auto-Instrumentor By TraceAI

Python auto-instrumentation library for MCP's Python SDK. This library enables context propagation between spans, allowing the active span during an MCP tool call to be linked with spans generated during execution. Note that this library does not generate any telemetry data.

## Installation

```bash
pip install traceai-mcp
```

## Usage

```python
from traceai_mcp import MCPInstrumentor
from traceai_openai import OpenAInstrumentor
from fi_instrumentation import register
from fi_instrumentation.types import ProjectType


tracer_provider = register(
    project_name="my-mcp-project",
    project_type=ProjectType.OBSERVE,
)

MCPInstrumentor().instrument(tracer_provider=tracer_provider)
OpenAInstrumentor().instrument(tracer_provider=tracer_provider)


# Start Implementing Here

```




