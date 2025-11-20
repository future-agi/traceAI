# traceAI-livekit

OpenTelemetry instrumentation for LiveKit agents integrated with Future AGI.

This package provides automatic attribute mapping from LiveKit's native OpenTelemetry instrumentation to Future AGI's semantic conventions, ensuring rich and structured traces in your dashboard.

## Installation

```bash
pip install traceai-livekit
```

## Quick Start

To instrument your LiveKit agent, simply initialize `fi-instrumentation` and enable the attribute mapping **inside your agent's entrypoint**.

```python
from livekit.agents import AgentServer, JobContext
from fi_instrumentation.otel import register, ProjectType
from traceai_livekit import enable_http_attribute_mapping

server = AgentServer()

@server.rtc_session()
async def entrypoint(ctx: JobContext):
    # 1. Initialize TraceAI (inside entrypoint for multiprocessing safety)
    register(
        project_name="My LiveKit Agent",
        project_type=ProjectType.OBSERVE,
        set_global_tracer_provider=True,
    )
    
    # 2. Enable LiveKit attribute mapping
    # This automatically hooks into LiveKit's telemetry system
    enable_http_attribute_mapping()
    
    # ... rest of your agent logic ...
    await ctx.connect()

if __name__ == "__main__":
    server.run()
```

## Features

*   **Automatic Attribute Mapping**: Converts LiveKit attributes (like `lk.user_input`, `gen_ai.usage.input_tokens`) to Future AGI standard attributes (`input.value`, `llm.tokenCount.prompt`).
*   **Raw Data Preservation**: Keeps original LiveKit attributes and adds `raw.input` / `raw.output` for full debugging context.
*   **Multiprocessing Support**: Designed to work with LiveKit's worker process model.
*   **Event Parsing**: Extracts LLM outputs from `gen_ai.choice` events when available.

## Examples

Check the `examples/` directory for complete working agents:
*   `simple_agent.py`: A basic voice assistant agent.
*   `egress_agent.py`: An agent with call recording (Egress) enabled.
