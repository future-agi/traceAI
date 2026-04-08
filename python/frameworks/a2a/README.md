# traceAI-a2a

**OpenTelemetry instrumentation for the [Google Agent-to-Agent (A2A) Protocol](https://google.github.io/A2A/) — distributed tracing across multi-agent boundaries.**

[![PyPI](https://img.shields.io/pypi/v/traceAI-a2a)](https://pypi.org/project/traceAI-a2a/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](../../LICENSE)
[![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-1.0+-purple)](https://opentelemetry.io/)

---

## The Problem

When an orchestrator agent calls a remote specialist agent over A2A, the trace breaks. The child agent starts a fresh trace. There is no parent-child link, no distributed context propagation, no visibility into what the remote agent did.

`traceai-a2a` fixes this.

---

## What Gets Captured

| Data | Attribute |
|------|-----------|
| Task ID | `gen_ai.a2a.task.id` |
| Task State | `gen_ai.a2a.task.state` |
| Remote Agent URL | `gen_ai.a2a.agent.url` |
| AgentCard Name | `gen_ai.a2a.agent.card.name` |
| AgentCard Version | `gen_ai.a2a.agent.card.version` |
| Message Role | `gen_ai.a2a.message.role` |
| Message Parts Count | `gen_ai.a2a.message.parts.count` |
| SSE Streaming | `gen_ai.a2a.streaming` |
| Artifact Type | `gen_ai.a2a.artifact.type` |
| Propagated Trace ID | `gen_ai.a2a.propagated_trace_id` |

---

## Install

```bash
pip install traceAI-a2a
```

For A2A SDK support:
```bash
pip install "traceAI-a2a[a2a]"
```

---

## Quickstart

```python
import os
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_a2a import A2AInstrumentor

os.environ["FI_API_KEY"] = "<your-api-key>"
os.environ["FI_SECRET_KEY"] = "<your-secret-key>"

# Register tracer provider
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="my_multi_agent_app"
)

# Instrument A2A — tracing propagates across agent boundaries automatically
A2AInstrumentor().instrument(tracer_provider=trace_provider)

# Now use A2AClient as normal — both agents appear in the same trace!
from a2a.client import A2AClient
import httpx

async with httpx.AsyncClient() as http_client:
    client = A2AClient(httpx_client=http_client, url="http://specialist-agent:8080")
    task_id = await client.send_task(
        payload={
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "Summarize the Q3 report"}]
            }
        }
    )
```

---

## How It Works

```
Orchestrator Agent                      Specialist Agent
─────────────────                       ────────────────
[span: A2A_CLIENT]                      [span: A2A_SERVER]  ← child of A2A_CLIENT span
  task_id: abc123          ────────────→   task_id: abc123
  agent_url: http://...    traceparent     message_role: user
  streaming: false         header injected  [span: LLM]
  task_state: completed ←────────────       model: gpt-4
```

Both spans share **the same trace ID** — giving you end-to-end visibility across agent boundaries in a single FutureAGI trace view.

---

## ASGI Server Middleware

For the receiving agent (Starlette/FastAPI):

```python
from starlette.applications import Starlette
from traceai_a2a import A2ATracingMiddleware

app = Starlette()
app.add_middleware(A2ATracingMiddleware, tracer_provider=trace_provider)
```

The middleware automatically extracts the incoming `traceparent` header and creates child spans in the distributed trace.

---

## See Also

- [traceAI Documentation](https://docs.futureagi.com)
- [A2A Protocol Specification](https://google.github.io/A2A/)
- [Examples](./examples/multi_agent_demo.py)
