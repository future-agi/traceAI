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

### MCP 1.8.1 dependency compatibility

MCP 1.8.1 uses a private Pydantic API that is absent in Pydantic 2.14.2.
If you must stay on MCP 1.8.1, pin its compatible Pydantic dependency:

```bash
pip install "traceAI-mcp" "mcp==1.8.1" "pydantic==2.11.10"
```

This pin applies only to MCP 1.8.1; do not impose it on newer SDK versions.

### Instrumentation lifecycle

`uninstrument()` disables this instrumentor's per-message propagation on
already-instrumented open streams as well as on newly opened transports; it
does not close sessions or remove wrappers installed by other libraries.
Reinstalling re-enables the existing instrumented streams and instruments new
transports exactly once. Sessions created while instrumentation is off are
not retroactively wrapped; reopen them after reinstall to instrument them.
Queued server messages remain intact across transitions; context captured while
enabled is attached only if the reader is enabled when it consumes the item.
An already-running request retains the context under which it started. Native
MCP SDK tracing remains independent and can continue when this instrumentor is
off, particularly with MCP 2.x.
