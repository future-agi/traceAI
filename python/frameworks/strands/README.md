# Trace Strands agents with Future AGI

Send Strands' native agent, model, event-loop and tool spans to Future AGI with one OpenTelemetry exporter. The adapter preserves the native trace tree, maps content to Future AGI attributes, and counts token usage only on model spans rather than again on agent aggregates.

## Install

```bash
pip install -U traceai-strands 'strands-agents[otel,openai]' 'fi-instrumentation-otel>=1.2.2'
```

Strands Agents 1.0.0 or newer and Python 3.10 or newer are required. The example uses your own OpenAI API key; you can instead use any model supported by Strands.

## Configure and run

Set `FI_API_KEY`, `FI_SECRET_KEY`, and `OPENAI_API_KEY` in your environment. Configure tracing **before constructing an Agent**, and use one global provider per process.

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.otel import HTTPSpanExporter
from traceai_strands import StrandsSpanExporter

provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="my-strands-agent",
    set_global_tracer_provider=True,
    span_exporter=StrandsSpanExporter(
        HTTPSpanExporter(),
        capture_content=True,
    ),
)

from strands import Agent
from strands.models.openai import OpenAIModel

try:
    agent = Agent(
        model=OpenAIModel(model_id="gpt-4o-mini"),
        trace_attributes={"gen_ai.provider.name": "openai"},
        callback_handler=None,
    )
    print(agent("What is the capital of France?"))
finally:
    provider.force_flush()
    provider.shutdown()
```

This example explicitly enables prompt, response and tool-content capture. Omit `capture_content=True` to export metadata and usage without content. In a long-running service, flush and shut down the provider when the service stops, not after each request.

Open **Observe** in Future AGI and select `my-strands-agent`. An invocation contains an agent span, an event-loop span and a model span; calls to Strands tools add native tool spans. Token usage belongs to model spans, not the enclosing aggregate spans.

`HTTPSpanExporter()` uses both Future AGI authentication headers and sends to `https://api.futureagi.com/tracer/v1/traces` by default. For self-hosting, set `FI_BASE_URL` to your Future AGI base URL. `register()` supplies the required `project_name` and `project_type` resource attributes. Do not add `StrandsTelemetry.setup_otlp_exporter()` or a second raw exporter to this pipeline: it would export another copy outside the adapter's privacy policy. Future AGI accepts traces, not Strands metrics or logs.

## Control content capture

`StrandsSpanExporter` uses the shared `TraceConfig` and its `FI_HIDE_*` environment settings. You can also pass a config explicitly:

```python
from fi_instrumentation import TraceConfig

exporter = StrandsSpanExporter(
    HTTPSpanExporter(),
    capture_content=True,
    config=TraceConfig(hide_inputs=True, hide_error_details=True),
)
```

When content is disabled, native message events, tool payloads and error details are omitted. When content is enabled, input/output privacy settings apply to both native event-based and attribute-based messages. Because native messages are serialized blobs, a granular input or output hide flag omits that direction's entire content, including tool arguments/results, instead of retaining an unmasked copy. Native content events are projected into attributes, not exported a second time.

With `hide_error_details=True`, failed tool responses are also removed from later message history, even when the agent recovers. Tool responses whose success status is unavailable are omitted from history too; newer Strands semantic conventions discard that status. Successful tool spans still retain their own results when the native status confirms success.

If message history cannot be decoded as JSON, for example after truncation by `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT`, its content is omitted rather than exported as unchecked text. This also omits plain-text non-tool messages, including legacy callbacks, when their structure cannot be verified. Valid JSON text and successful direct tool results are preserved. This setting does not detect error text that your application or model copies into ordinary messages.

Span names, model/tool identifiers, session/user IDs and application resource attributes are metadata; do not put secrets in them. Unknown custom native attributes and link attributes are omitted. Other instrumentation scopes pass through unchanged: this adapter is not a privacy filter for the whole application. Content opt-in is not automatic credential detection.

## Migrate existing setup

Keep `traceai-strands` installed. Replace the old `register(...)` plus `configure_strands_tracing(...)` setup with the exporter-wrapped `register(...)` above. Bare `register()` exports native spans but does not perform this adapter's semantic, aggregate-token or privacy conversion.

`configure_strands_tracing`, `StrandsInstrumentor`, `create_traced_agent`, attribute helpers and `StrandsCallbackHandler` remain available. Legacy setup now leaves metrics disabled by default and reuses an existing exporter when no OTLP endpoint is configured. If the provider already contains `StrandsSpanExporter`, legacy setup never adds a raw OTLP copy, even with ambient `OTEL_*` settings. The legacy setup helper does not retrofit the adapter onto exporters you already created. When using the adapter, add a separately wrapped console exporter rather than `enable_console_exporter=True`, which would bypass privacy filtering.

The optional callback handler is a legacy manual event API, not a replacement for native agent/model/tool tracing. Do not attach it to an Agent just to obtain those spans: Strands already emits them. Applications that explicitly call its `on_agent_*`, `on_model_*`, or `on_tool_*` methods can retain those calls; they create additional application spans and should not duplicate native operations. To stop exporting native traces, shut down the provider at application exit. `uninstrument()` only resets the legacy setup helper; it cannot remove Strands' own tracing or undo OpenTelemetry's global provider.

## Troubleshoot

- **No traces:** install the global provider before creating the first Agent. Restart the process after changing a previously installed global provider; OpenTelemetry does not support replacing it.
- **Duplicate spans or usage:** configure one wrapped exporter and remove additional raw OTLP exporters and lifecycle callbacks for native operations.
- **No prompts or responses:** enable `capture_content=True` and check your `FI_HIDE_*` settings. Strands' own content-redaction settings can also remove content before it reaches the adapter.
- **Authentication errors:** supply both `FI_API_KEY` and `FI_SECRET_KEY`, and check `FI_BASE_URL` for self-hosted deployments.

## Resources

- [Strands observability](https://strandsagents.com/docs/user-guide/sdk/observability-evaluation/traces/)
- [Strands model providers](https://strandsagents.com/docs/user-guide/concepts/model-providers/)
- [Future AGI documentation](https://docs.futureagi.com/)
