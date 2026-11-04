# traceAI-agentscope

Send AgentScope's native reply, model-call and tool-execution spans to Future AGI. This adapter uses AgentScope's `TracingMiddleware`; it does not wrap model SDKs or create a second set of spans.

Requires Python 3.11–3.13 and AgentScope 2.0.10–2.0.x.

```bash
pip install traceAI-agentscope
export FI_API_KEY="YOUR_FI_API_KEY"
export FI_SECRET_KEY="YOUR_FI_SECRET_KEY"
export OPENAI_API_KEY="YOUR_OPENAI_API_KEY"
```

```python
import asyncio
import os

from agentscope.agent import Agent
from agentscope.credential import OpenAICredential
from agentscope.message import UserMsg
from agentscope.model import OpenAIChatModel
from agentscope.state import AgentState
from traceai_agentscope import configure, TracingMiddleware


async def main():
    tracing = configure(project_name="support-agent")
    try:
        agent = Agent(
            name="support",
            system_prompt="Explain technical concepts in one short paragraph.",
            model=OpenAIChatModel(
                credential=OpenAICredential(api_key=os.environ["OPENAI_API_KEY"]),
                model="gpt-4o-mini",
            ),
            state=AgentState(session_id="support-session-001"),
            middlewares=[TracingMiddleware()],
        )
        answer = await agent.reply(UserMsg(name="user", content="What is a trace?"))
        print(answer)
    finally:
        # Lifecycle methods are synchronous; keep blocking work off your async loop.
        await asyncio.to_thread(tracing.shutdown, 5000)


asyncio.run(main())
```

Open **Observe** and select `support-agent`. Replies are `AGENT` spans, model calls are `LLM` spans, and tool executions are `TOOL` spans. Native trace IDs, parentage and timing are preserved. Model token counts stay on model spans. The native conversation ID is also exported as `session.id`; use an opaque session identifier, not an email address or prompt.

Use the adapter middleware **instead of**, not alongside, the native `TracingMiddleware`. Configuration alone does not correct the native middleware's returned-tool-error status. Do not also instrument the model SDK for the same calls. Returned tool results with `state=ERROR` (`ToolResultBlock`, `ToolChunk`, or `ToolResponse`) are marked as errors at this destination, including intermediate streamed results. Exception messages and stacks remain excluded. Native synthetic spans for receipts of externally executed tools are not exported: they do not measure execution of those tools.

## Content controls

Metadata-only is the default. Recognized operational identifiers and numeric settings are retained; arbitrary attributes, resource fields, link attributes, scope attributes, status descriptions and event payloads are not forwarded. Span names are rebuilt from the operation and an allowlisted identifier. Names, model identifiers and session IDs are metadata, not automatically anonymized; keep sensitive data out of them.

Set `capture_content=True` in `configure` to include native messages and tool arguments/results, with `input.value` and `output.value` aliases. Known credential and stacktrace fields in structured content are redacted, including nested JSON strings. This is **not** a free-text secret detector: messages and tool output can contain personal data or credentials in prose. Enable content capture only for approved application data. Malformed, overly nested or oversized structured values (over 65,536 characters) are omitted. AgentScope 2.0.10 emits tool results as opaque object representations, which are also omitted; upgrade to 2.0.11 to capture structured tool output.

Filtering applies only to this adapter's destination, before its export queue. AgentScope records content in memory unconditionally. Other processors on the global provider still receive the original native spans and need their own privacy controls.

## Provider and lifecycle

`configure` reuses the global OpenTelemetry SDK provider, or installs one when no provider is registered. An explicit `provider=` must be that global provider or be installed before any other provider. An incompatible or disabled provider is rejected rather than silently exporting nothing. Sampling and span limits belong to the provider; the adapter does not change them.

The returned handle exposes `provider` and `owns_provider` (whether the adapter created it). It owns only its export pipeline: shutdown never stops the provider or other destinations, including when the adapter created the provider. A global provider cannot be replaced, so the installed provider stays reusable.

- `force_flush(timeout_millis=5000)` drains ended spans without detaching.
- `shutdown(timeout_millis=5000)` detaches immediately, drains queued spans and closes the exporter. Call it after finishing your agent operations.
- Both return `False` on a deadline or cleanup failure; a successful flush means queue drainage, not confirmed delivery to Future AGI. Cleanup may finish in the background after a deadline.
- Repeated configuration with the same explicit options shares one pipeline. Different options require shutdown first. Do not change credentials through environment variables while a pipeline is active; shut it down and configure again.
- Spans already open when you configure, or still open when you shut down, are not exported. Reconfiguration does not adopt old spans. At most 65,536 open spans and 2,048 queued ended spans are retained; overload drops spans.
- Reconfiguration waits for previous cleanup to complete by rejecting attempts while it is still closing. Overlapping configuration calls fail fast. A provider shut down through OpenTelemetry cannot be reused.

`FI_BASE_URL` selects your Future AGI origin. Alternatively pass a full OTLP HTTP `endpoint` and `headers`, or a custom OpenTelemetry `exporter` (not both). The default exporter uses OTLP HTTP/protobuf with a 10-second request timeout. A custom exporter transfers to this pipeline and must implement `export`, `force_flush` and `shutdown`. Lifecycle deadlines bound callers' waits even for a stuck custom exporter; daemon cleanup cannot guarantee delivery on process termination.
