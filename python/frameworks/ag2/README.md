# traceAI-ag2

Send AG2 1.x traces to Future AGI through AG2's own `TelemetryMiddleware`.
Alpha, fixture-validated. No live vendor run is claimed.

AG2 1.x is the PyPI package `ag2`, imported as `ag2`. It is not Microsoft AutoGen
(`autogen-agentchat`) and it is not AG2 Classic (`autogen` 0.14.x).

| You installed | Import | Package |
|---|---|---|
| `ag2` 1.x | `ag2` | `traceAI-ag2` (this package) |
| `autogen` 0.14.x | `autogen` | AG2 Classic integration (TH-8237) |
| `autogen-agentchat` | `autogen_agentchat` | `traceAI-autogen` |

Requires `ag2>=1.0.0` (the first stable 1.x that ships `TelemetryMiddleware`;
tested on 1.0.0, 1.0.3 and 1.1.2) and Python 3.10 to 3.13. `ag2` is
Apache-2.0 and is a dependency only; nothing from it is vendored.

## What it does

AG2 1.x already emits OpenTelemetry GenAI spans from `TelemetryMiddleware`.
This package does not wrap `Agent`. It:

1. registers the Future AGI exporter through `fi_instrumentation.register()`
   (or uses the provider you pass),
2. attaches `TelemetryMiddleware` to your agents with `capture_content=False`
   through AG2's public `Agent.add_middleware`,
3. installs `AG2SpanProcessor`, which, on spans from AG2's
   `opentelemetry.instrumentation.ag2` scope only:
   - sets `gen_ai.span.kind` from `gen_ai.operation.name`
     (`chat` -> `LLM`, `execute_tool` -> `TOOL`, `invoke_agent` -> `AGENT`),
   - copies AG2's usage keys onto the GenAI semconv names and keeps the originals:

     | AG2 key | Alias |
     |---|---|
     | `gen_ai.usage.cache_creation_input_tokens` | `gen_ai.usage.cache_creation.input_tokens` |
     | `gen_ai.usage.cache_read_input_tokens` | `gen_ai.usage.cache_read.input_tokens` |
     | `gen_ai.usage.thinking_tokens` | `gen_ai.usage.reasoning.output_tokens` |

   - applies `TraceConfig` (`FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, ...) as a
     second content gate.

## Set up

```bash
pip install traceAI-ag2 "ag2[openai]"   # the openai extra is only for the OpenAIConfig example below
export FI_API_KEY="YOUR_API_KEY"
export FI_SECRET_KEY="YOUR_SECRET_KEY"
export FI_PROJECT_NAME="my-chatbot"
```

AG2 attaches middleware per agent, so pass your agents to `setup`:

```python
from ag2 import Agent
from ag2.config import OpenAIConfig
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_ag2 import setup

trace_provider = register(project_type=ProjectType.OBSERVE, project_name="my-chatbot")

agent = Agent("assistant", "You are helpful.", config=OpenAIConfig(model="gpt-4o-mini"))
setup(agent, tracer_provider=trace_provider)  # capture_content=False

reply = await agent.ask("Hello")  # inside an async function
trace_provider.force_flush()  # short scripts: flush before exit
```

This snippet is illustrative and was not run against a live model; the
offline example below is what the tests execute.

`setup(agent)` without `tracer_provider` calls
`register(project_type=ProjectType.OBSERVE, project_name=project_name)` itself
and returns the provider.

Agents created later, or per-call middleware:

```python
from traceai_ag2 import create_telemetry_middleware

mw = create_telemetry_middleware(tracer_provider=trace_provider, agent_name="worker")
worker = Agent("worker", config=cfg, middleware=[mw])
# or: await agent.ask("...", middleware=[mw])
```

Call `setup(tracer_provider=trace_provider)` once in that case too, so the span
processor is installed. `setup` is idempotent: an agent that already has a
`TelemetryMiddleware` is skipped.

`provider_name` is never invented. Pass it if you want it on every span;
otherwise AG2 fills it from the model response when the client reports one.

An offline example with a scripted model (no model API key) is in
`examples/offline_weather_agent.py`.

## Privacy

Content is off unless you opt in. Upstream `TelemetryMiddleware` defaults
`capture_content=True`; `setup` and `create_telemetry_middleware` pass `False`.
With it off, spans carry no prompts, completions, tool arguments, tool results
or human-input text.

```python
setup(agent, tracer_provider=trace_provider, capture_content=True)  # opt in
```

`TraceConfig(hide_inputs=True)` / `hide_outputs=True` (or `FI_HIDE_INPUTS` /
`FI_HIDE_OUTPUTS`) still removes captured content in the span processor.

## Conformance

| Field | What you get | When it is omitted |
|---|---|---|
| Span kind | `AGENT`, `LLM`, `TOOL` from `gen_ai.operation.name` | `await_human_input` and `record_usage` spans get no kind (see below) |
| Model | `gen_ai.request.model`, `gen_ai.response.model` pass through | When AG2 did not set them |
| Provider | `gen_ai.provider.name` from `provider_name` or the model response | When neither is known |
| Tokens | `gen_ai.usage.input_tokens` / `output_tokens` pass through; cache and thinking tokens are aliased and kept | When the usage field is zero/absent. Cost is not emitted |
| Session | Not emitted by AG2 | Set it yourself: `setup(..., span_attributes={"session.id": "..."})` |
| User | Not emitted | Always |
| Tools | `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.type`; arguments/result only with `capture_content=True` | |
| Errors | OTel status `ERROR` on failing tool / model / agent spans | |

Notes:

- AG2 also emits `await_human_input {agent}` spans. That operation is not in
  the Microsoft Agent Framework kind table, so no kind is guessed for it.
- From ag2 1.0.3, AG2 also emits a `record_usage {kind}` span per usage event,
  which carries the same token counts as the `chat` span it accounts for.
  It has no operation name and gets no span kind; the aliases are applied.
- Network trace propagation (`ag2.otel.traceparent`) is upstream. The
  processor never removes attributes it did not hide by request.

## Troubleshooting

- No traces: `register()` needs `project_name` or `FI_PROJECT_NAME`; the
  collector drops a batch whose resource has no `project_name`.
- Duplicate spans: do not attach a second `TelemetryMiddleware`, and do not
  also enable `traceai-autogen` in an `ag2` process.
- Export failures are logged and never fail the agent.
- Content appeared without opting in: you constructed `TelemetryMiddleware`
  yourself. Pass `capture_content=False`, or use `create_telemetry_middleware`.
- This package never imports `autogen`. If your code imports `autogen`, you
  are on AG2 Classic or Microsoft AutoGen; use that integration instead.
