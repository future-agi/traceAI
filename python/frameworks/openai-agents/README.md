# OpenAI Agents tracing

Trace OpenAI Agents SDK workflows, model calls, tools and handoffs in Future AGI using the SDK's official tracing processor interface.

## Install

Use Python 3.10–3.13 and OpenAI Agents SDK 0.2.6 or later.

```bash
pip install traceAI-openai-agents
```

Set `FI_API_KEY`, `FI_SECRET_KEY` and your own `OPENAI_API_KEY` in your application's environment.

## Trace an agent

```python
from agents import Agent, RunConfig, Runner
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_openai_agents import OpenAIAgentsInstrumentor

provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="openai_agents_app",
)
OpenAIAgentsInstrumentor().instrument(tracer_provider=provider)

agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
try:
    result = Runner.run_sync(
        agent,
        "Write a haiku about recursion in programming.",
        run_config=RunConfig(
            group_id="recursion-session",
            trace_metadata={"feature": "haiku"},
            trace_include_sensitive_data=False,
        ),
    )
    print(result.final_output)
finally:
    provider.force_flush()
```

Open **openai_agents_app** in Future AGI to inspect the workflow and its child spans.

## Export destinations and lifecycle

`exclusive_processor=True` is the default. Instrumentation replaces **all existing SDK trace processors**, including the default OpenAI tracing exporter. Set `exclusive_processor=False` to preserve them; those processors continue sending data to their configured destinations. This changes tracing destinations, not model API calls.

Configure instrumentation once before running agents. Do not also instrument the underlying OpenAI client for these calls: the Agents processor already records model spans, and a second instrumentor can produce duplicates. The SDK does not provide a public per-processor removal API; `uninstrument()` does not restore processors that were replaced.

Call `provider.force_flush()` after a short script or before terminating your application. Processor `force_flush()` and `shutdown()` also flush the supplied provider. They do not shut it down, because other integrations may share it. Call `provider.shutdown()` only when your application owns that provider and all producers have stopped. Direct users of `FiTracingProcessor(tracer)` can pass `tracer_provider=provider` to enable lifecycle forwarding.

## Captured fields and privacy

- Trace `group_id` becomes `session.id`; trace and supported span metadata are stored as JSON `metadata`.
- Workflow and enclosing spans retain the first available model input and the last completed model output, for both Responses and Chat Completions paths, including streaming.
- Model spans retain token usage, including explicit zeros. SDK task and turn spans include their final usage when the installed SDK supplies those span types.
- Guardrails expose their final `guardrail.triggered` outcome; handoffs retain source and destination agents.
- Audio spans include available text, model settings and format/timing fields. Binary or base64 audio is not exported.

The example disables sensitive model/tool payload capture in the SDK. To include input/output content, use `RunConfig(trace_include_sensitive_data=True)` only with data approved for tracing. SDK privacy settings do not scrub custom metadata, names or every error detail.

For exporter-side input/output masking, pass a `TraceConfig` when instrumenting:

```python
from fi_instrumentation import TraceConfig

OpenAIAgentsInstrumentor().instrument(
    tracer_provider=provider,
    config=TraceConfig(hide_inputs=True, hide_outputs=True),
)
```

Use this in place of the first instrumentation call. Keep secrets out of span names and custom metadata. See the [OpenAI tracing documentation](https://openai.github.io/openai-agents-python/tracing/) for SDK-level controls.
