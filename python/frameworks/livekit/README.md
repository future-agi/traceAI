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

`enable_http_attribute_mapping()` (or `enable_grpc_attribute_mapping()`) hands the provider to LiveKit for you. If you call
LiveKit's `set_tracer_provider` yourself on the provider `register()` returned, call it after `enable_*`: before it, the call
drops the Future AGI exporter. LiveKit installs its privacy filter once per provider, so an `allow_pii` passed in a later call is
ignored. To keep conversation content out of your traces, set `LIVEKIT_TELEMETRY_ALLOW_PII=false` before calling `enable_*`
(livekit-agents 1.8 and later).

## Compatibility

*   **livekit-agents**: 1.x. 1.8 is the tested version (it needs Python 3.10 or later). Since 1.7, LiveKit names its content
    attributes `lk.pii.*` (for example `lk.pii.user_input`); both those names and the earlier ones are mapped to `input.value`
    and `output.value`.
*   **fi-instrumentation-otel**: 1.1.0 or later (1.0.0 fails inside `register()` with OpenTelemetry 1.45, which livekit-agents
    1.8.7 and later require). 1.0 moved span attributes from the `llm.*` names to the OpenTelemetry GenAI `gen_ai.*` names, and
    renamed the `SpanAttributes` constants with them. Code that only calls `register()` is unaffected; if your code sets or reads
    those attributes directly, see the
    [mapping from the old names](https://github.com/future-agi/traceAI/blob/main/docs/OTEL_GENAI_SEMANTIC_CONVENTIONS.md#migration-from-openinference-llm).

## Features

*   **Automatic Attribute Mapping**: Converts LiveKit attributes (like `lk.user_input`, `gen_ai.usage.input_tokens`) to Future AGI standard attributes (`input.value`, `llm.tokenCount.prompt`).
*   **Raw Data Preservation**: Keeps original LiveKit attributes and adds `raw.input` / `raw.output` for full debugging context.
*   **Multiprocessing Support**: Designed to work with LiveKit's worker process model.
*   **Event Parsing**: Extracts LLM outputs from `gen_ai.choice` events when available.

## Examples

Check the `examples/` directory for complete working agents:
*   `simple_agent.py`: A basic voice assistant agent.
*   `egress_agent.py`: An agent with call recording (Egress) enabled.
