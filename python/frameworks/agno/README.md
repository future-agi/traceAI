# Trace Agno with Future AGI

Send Agno agent, model, tool, team, and workflow spans to Future AGI using the maintained [OpenInference Agno instrumentor](https://github.com/Arize-ai/openinference/tree/main/python/instrumentation/openinference-instrumentation-agno). Agno's own tracing setup delegates to the same instrumentor; this package configures it rather than adding a second span tree.

## Install

Use Python 3.10 or later and Agno 2.7.2 or later. The current OpenInference instrumentor no longer supports the old Agno 2.0 API. The `agno` extra installs the framework and its workflow import dependencies.

```bash
pip install 'traceai-agno[agno]' openai
```

Set `FI_API_KEY` and `FI_SECRET_KEY` to your Future AGI project credentials, and `OPENAI_API_KEY` to your own OpenAI key. For a self-hosted Future AGI deployment, also set `FI_BASE_URL` to its origin, without `/tracer/v1/traces`.

## Run an agent

Configure tracing once, before running agents. This example uses OTLP/HTTP protobuf with the two Future AGI authentication headers. It hides captured inputs and outputs while retaining model, usage, and span hierarchy.

```python
import os

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from openinference.instrumentation import TraceConfig
from traceai_agno import configure_agno_tracing

tracing = configure_agno_tracing(
    otlp_endpoint=os.getenv("FI_BASE_URL", "https://api.futureagi.com").rstrip("/")
    + "/tracer/v1/traces",
    otlp_headers={
        "X-Api-Key": os.environ["FI_API_KEY"],
        "X-Secret-Key": os.environ["FI_SECRET_KEY"],
    },
    project_name="agno-agent",
    config=TraceConfig(hide_inputs=True, hide_outputs=True),
)

try:
    agent = Agent(model=OpenAIChat(id="gpt-4o-mini"), telemetry=False)
    response = agent.run("What is the capital of France?")
    print(response.content)
    tracing.tracer_provider.force_flush()
finally:
    tracing.uninstrument()
```

Open **agno-agent** in Future AGI's Observe view and inspect the agent run and its model child span. Inputs and outputs are hidden by the configuration above. Resource attributes identify the project as `observe` and its semantic convention as `openinference`.

## Use an existing Future AGI provider

If your application already owns a provider, pass it explicitly. It does not need to be the global OpenTelemetry provider. Configure export and project settings on that provider, not again on `configure_agno_tracing`.

```python
from agno.agent import Agent
from agno.models.openai import OpenAIChat
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.otel import SemanticConvention
from openinference.instrumentation import TraceConfig
from traceai_agno import configure_agno_tracing

provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="agno-agent",
    semantic_convention=SemanticConvention.OPENINFERENCE,
)
tracing = configure_agno_tracing(
    tracer_provider=provider,
    config=TraceConfig(hide_inputs=True, hide_outputs=True),
)
try:
    agent = Agent(model=OpenAIChat(id="gpt-4o-mini"), telemetry=False)
    print(agent.run("What is the capital of France?").content)
    provider.force_flush()
finally:
    tracing.uninstrument()
    provider.shutdown()
```

`uninstrument()` closes providers created by endpoint configuration. It does **not** close a provider you supplied. Shut shared providers down only when the application exits and in-flight runs have finished.

## Choose what content to capture

Use `openinference.instrumentation.TraceConfig` or `OPENINFERENCE_*` environment variables. To include inputs and outputs, pass `TraceConfig(hide_inputs=False, hide_outputs=False)`. Without an explicit config, OpenInference uses its environment settings and defaults, which capture content.

The `FI_HIDE_*` variables do not automatically apply to this upstream instrumentor. Input/output hiding is not a blanket secret scrubber: names, custom metadata, and exception details can still contain sensitive text. Keep secrets out of those fields or apply your application's export filtering policy.

## Configuration and helpers

- Call `configure_agno_tracing` once per process. Repeating the same settings is safe; different settings require `uninstrument()` first.
- Do not combine it with `agno.tracing.setup_tracing`, a separate `AgnoInstrumentor().instrument()`, or another model instrumentor for the same calls. Use one instrumentation path to avoid duplicate spans.
- `setup_traceai_exporter(endpoint, headers, ..., project_name=..., set_global=False)` creates a provider without instrumenting Agno. Its historical default is `set_global=True`; use `False` when another library owns the global provider. Flush and shut down the returned provider yourself.
- `get_agent_attributes`, `get_tool_attributes`, `get_team_attributes`, `get_model_provider`, and `create_trace_context` remain available. They return values for manual use; they do not attach attributes or context to automatically emitted spans.
- Automatic spans retain OpenInference names such as `openinference.span.kind`, `llm.model_name`, and `llm.token_count.prompt`. The manual helpers' `gen_ai.*` attributes are not an automatic mapper.

See the [examples](./examples) for agent, tool, team, and workflow applications.

## Troubleshooting

- **Setup raises an import or dependency error:** install the `agno` extra and check that Agno and OpenInference satisfy the package requirements. Setup does not report success when the instrumentor is missing.
- **No spans:** supply either a configured provider or a complete OTLP traces URL. Setting exporter environment variables alone does not create a provider. Flush before exiting.
- **Authentication fails:** use `/tracer/v1/traces` and both `X-Api-Key` and `X-Secret-Key`. Project names belong to resource attributes, not an `x-project-name` header.
- **Already instrumented:** remove the other Agno setup call. The wrapper does not replace or take ownership of another instrumentor's configuration.

## License

Apache-2.0
