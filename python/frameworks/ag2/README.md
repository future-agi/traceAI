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
tested on 1.0.0, 1.0.3 and 1.1.2), `fi-instrumentation-otel>=1.1.0` and
Python 3.10 to 3.13. `ag2` is
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
`TelemetryMiddleware` is skipped. One `AG2SpanProcessor` serves the provider:
a later `setup(..., config=TraceConfig(...))` replaces its `TraceConfig` (the
most recent call wins and a warning is logged when it changes), while
`config=None` keeps the current one.

Sessions and users come from traceAI's context helpers, as with the other
traceAI integrations:

```python
from fi_instrumentation import using_attributes, using_session

with using_session("chat-123"):
    reply = await agent.ask("Hello")  # every AG2 span gets session.id="chat-123"

with using_attributes(session_id="chat-123", user_id="user-7", metadata={"tier": "pro"}):
    reply = await agent.ask("Hello")
```

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

With content on, `TraceConfig` (or the matching `FI_HIDE_*` variables) still
removes it in the span processor. AG2 records message content as one JSON
string per span (`gen_ai.input.messages`, `gen_ai.output.messages`), not as
the per-message keys that `TraceConfig.mask`'s text and image rules match, so
the processor drops whole attributes:

| `TraceConfig` flag | Removed from AG2 spans |
|---|---|
| `hide_inputs`, `hide_input_text` | `gen_ai.input.messages`, `gen_ai.system_instructions`, `gen_ai.tool.call.arguments`, `ag2.human_input.prompt` |
| `hide_outputs`, `hide_output_text` | `gen_ai.output.messages`, `gen_ai.tool.call.result`, `ag2.human_input.response` |
| `hide_input_messages` | `gen_ai.input.messages`, `gen_ai.system_instructions` |
| `hide_output_messages` | `gen_ai.output.messages` |
| `hide_llm_invocation_parameters` | `gen_ai.request.parameters` (through `TraceConfig.mask`) |
| `hide_input_images`, `hide_embedding_vectors` | Nothing: AG2 records only text parts (binary inputs are omitted) and no embeddings |

The text flags cannot redact just the text inside the JSON, so they remove the
whole message attribute, which is the privacy-safe choice.

## Conformance

| Field | What you get | When it is omitted |
|---|---|---|
| Span kind | `AGENT`, `LLM`, `TOOL` from `gen_ai.operation.name` | `await_human_input` and `record_usage` spans get no kind (see below) |
| Model | `gen_ai.request.model`, `gen_ai.response.model` pass through | When AG2 did not set them |
| Provider | `gen_ai.provider.name` from `provider_name` or the model response | When neither is known |
| Tokens | `gen_ai.usage.input_tokens` / `output_tokens` pass through; cache and thinking tokens are aliased and kept | When the usage field is zero/absent. Cost is not emitted |
| Session | Not emitted by AG2. `session.id` from traceAI's `using_session` / `using_attributes` context, which wins over a static value; or set it yourself: `setup(..., span_attributes={"session.id": "..."})` | When neither is set |
| User, metadata, tags | `user.id`, `metadata`, `tag.tags` (and the other traceAI context keys) from `using_user` / `using_metadata` / `using_tags` / `using_attributes`; a key AG2 or `span_attributes` already set is not overridden | When the context does not set them |
| Tools | `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.type`; arguments/result only with `capture_content=True` | |
| Errors | OTel status `ERROR` on failing tool / model / agent spans | |

Notes:

- AG2 also emits `await_human_input {agent}` spans. That operation is not in
  the Microsoft Agent Framework kind table, so no kind is guessed for it.
- From ag2 1.0.3, AG2 also emits a `record_usage {kind}` span per usage event
  (`ag2.usage.kind` = `model_call`, `aggregation`, `compaction` or `subtask`).
  It has no operation name and gets no span kind; the aliases are applied.
  Future AGI totals tokens by summing the promoted
  `gen_ai.usage.input_tokens` / `output_tokens` / `total_tokens` over every
  span in a trace, so the processor moves those three keys to
  `ag2.usage.input_tokens` / `output_tokens` / `total_tokens` (values kept on
  the span) exactly where a chat span already counts the same spend:

  | `ag2.usage.kind` | Same tokens already on a chat span? | Promoted tokens |
  |---|---|---|
  | `model_call` | Yes: the chat span of that LLM call | Moved to `ag2.usage.*` |
  | `aggregation` | No: memory aggregation calls the model client directly, outside the middleware | Kept |
  | `compaction` | No: history compaction calls the model client directly | Kept |
  | `subtask` | Only if the sub-agent named in `ag2.usage.label` is itself instrumented and its `invoke_agent` span ended earlier in the same trace | Moved when it is, kept otherwise |

  With that, the trace total equals AG2's own `UsageReport` total; the tests
  check this with real AG2 for every kind above. For the `subtask` match, the
  sub-agent's `TelemetryMiddleware` must use the agent's own name (`setup`
  does; `create_telemetry_middleware(agent_name=...)` must pass `agent.name`).
  On ag2 1.0.0 to 1.0.2 there are no `record_usage` spans, so aggregation,
  compaction and uninstrumented sub-agent spend does not reach the trace.
- Network trace propagation (`ag2.otel.traceparent`) is upstream. Apart from
  that usage move, the processor never removes attributes it did not hide by
  request.

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
