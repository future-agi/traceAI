# traceAI-ag2-classic

Send AG2 Classic traces to Future AGI. AG2 Classic is the `autogen` distribution
at 0.14.x, imported as `autogen`, from `ag2ai/ag2classic`. It ships its own
OpenTelemetry instrumentation, `autogen.opentelemetry`. This package turns that
on against a Future AGI tracer provider and maps its span attributes to Future
AGI conventions. Alpha. Validated against fixtures and a loopback fake model; no
live vendor run is claimed.

| You installed | Import | Package |
|---|---|---|
| `autogen` 0.14.x | `autogen` | `traceAI-ag2-classic` (this package) |
| `ag2` 1.x | `ag2` | `traceAI-ag2` |
| `autogen-agentchat` | `autogen_agentchat` (Microsoft AutoGen) | `traceAI-autogen` |
| `pyautogen` | older alias | not supported |

Supported: `autogen` 0.14.0 and 0.14.1. Upstream `ag2ai/ag2classic` at
`86fa5ccb3f9424cee87edb4a9ea58f191ea958ba`, Apache-2.0, copyright AG2ai.
`autogen` is a normal dependency (`>=0.14.0,<0.15`); it is never vendored.

Note on 0.14.0: PyPI `autogen==0.14.0` is a small alias whose only requirement
is `ag2==0.14.0`, and the `autogen` module is shipped by that `ag2` 0.14.0
distribution. Its `autogen/opentelemetry` tree is identical to 0.14.1's, so the
version guard accepts exactly that pairing. `autogen==0.14.1` ships the module
itself and installs no `ag2`. `ag2` 1.x ships only an `ag2` module.

## Install

```bash
pip install "autogen>=0.14.0,<0.15"
pip install traceAI-ag2-classic
```

`autogen[tracing]` also works. That extra adds `opentelemetry-sdk` and the OTLP
gRPC exporter; this package never constructs that exporter.

```bash
export FI_API_KEY="YOUR_API_KEY"
export FI_SECRET_KEY="YOUR_SECRET_KEY"
export FI_PROJECT_NAME="my-chatbot"
```

## Set up

```python
from autogen import ConversableAgent
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_ag2_classic import setup

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="my-chatbot",
)

assistant = ConversableAgent("assistant", llm_config={"config_list": [{"model": "gpt-4o-mini"}]})
user = ConversableAgent("user", llm_config=False, human_input_mode="NEVER")

tracing = setup(tracer_provider=trace_provider, agents=[assistant, user])
user.initiate_chat(assistant, message="Hello", max_turns=2)

trace_provider.force_flush()  # short scripts: flush before exit
```

Upstream instruments agents per instance, so pass every agent you want traced,
or call `tracing.instrument_agent(agent)` for agents created later. Pass a
`GroupChatManager` and its group chat members and speaker selection are
instrumented too. Pass group patterns with `patterns=[...]` or
`tracing.instrument_pattern(pattern)`. `tracing.instrument_a2a_server(server)`
is available only when upstream exports it (the `autogen[a2a]` extra); it is
never called unless you ask.

`setup()`:

1. Checks that `autogen` is AG2 Classic 0.14.x. It raises
   `AG2ClassicCompatibilityError`, naming the packages above, when `autogen`
   is missing (for example only `ag2` 1.x or Microsoft AutoGen is installed),
   when the version is not 0.14.x, when `autogen.opentelemetry` is missing, or
   when the `autogen` module comes from `pyautogen` or from `ag2` outside the
   0.14.0 alias pairing.
2. Puts `AG2ClassicSpanProcessor` first on the provider you pass, ahead of the
   Future AGI exporter.
3. Calls upstream `instrument_llm_wrapper(capture_messages=capture_content)`,
   then `instrument_agent` and `instrument_pattern` for what you pass. If one
   of those raises (for example a non-agent in `agents=`), `setup()` restores
   `OpenAIWrapper.create` and re-raises.

It never creates a provider, never sets the global provider, and never adds an
exporter. One provider, the one `register()` returned. HTTP to the collector is
the default; pass `transport=Transport.GRPC` (from `fi_instrumentation.otel`) to `register()` for gRPC. Do not
also install the `[tracing]` extra's gRPC exporter as a global provider, or
spans are duplicated.

`tracing.uninstrument()` restores `OpenAIWrapper.create`, so no new LLM spans.
Agents stay instrumented; upstream has no per-agent undo. The processor stays on
the provider, so spans from those agents still have content removed and
aggregate usage moved off non-LLM spans. It is shared by every handle on that
provider and shuts down with `trace_provider.shutdown()`.

## Conformance

Every row was read from `autogen/opentelemetry/instrumentators/` at 0.14.1.
LLM, agent, tool, conversation, group-chat speaker selection, pattern and
`initiate_chats` spans are exercised by real `autogen` runs against a loopback
fake model in `tests/`; code execution, human input and remote-agent spans are
covered by mapping unit tests only.

| Field | What you get | When it is omitted |
|---|---|---|
| Span kind | `gen_ai.span.kind` from upstream `ag2.span.type`: `llm` → `LLM`; `agent` → `AGENT`; `tool`, `code_execution` → `TOOL`; `conversation`, `multi_conversation`, `speaker_selection`, `human_input` → `CHAIN`; `handoff` → `CHAIN` (declared upstream as TODO, never emitted at 0.14.x). `ag2.span.type` and `gen_ai.operation.name` are kept. | The `a2a-execution` span from `instrument_a2a_server` has no `ag2.span.type` and gets no kind. |
| Model, provider | `gen_ai.request.model`, `gen_ai.provider.name` on LLM, agent and conversation spans; `gen_ai.response.model` on LLM spans. Upstream keys, passed through. | Agents without an LLM config carry no model or provider. |
| Tokens | `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` on LLM spans (from the response `usage`), plus `gen_ai.usage.total_tokens` added here. | A response without `usage` gives no token keys. Conversation spans carry upstream's aggregate input/output tokens for the whole chat. Future AGI sums tokens over every span in a trace, so those move to `ag2.usage.input_tokens` / `ag2.usage.output_tokens`; each LLM call counts once. |
| Cost | `gen_ai.cost.total` on LLM spans, copied from upstream `gen_ai.usage.cost` (AG2's own price table). Upstream also reports `response.cost` on a cache hit (`oai/client.py:1230-1241` returns the cached response with its stored cost), so with `cache_seed` set a cached reply carries the cost of the original call and Future AGI over-reports cost for cached responses; its token counts are the original call's too. Not fixed here; upstream behaviour. | Conversation spans carry upstream's aggregate `gen_ai.usage.cost`; it moves to `ag2.usage.cost`, so no `gen_ai.usage.*` or `gen_ai.cost.total` key is left on any non-LLM span. |
| Session | `session.id` from upstream `gen_ai.conversation.id` (`ChatResult.chat_id`) on the outermost `conversation` span. Inside `using_session(...)` or `using_attributes(session_id=...)` (from `fi_instrumentation`), that id is set on every AG2 span instead and wins over the chat id; `gen_ai.conversation.id` keeps the chat id. | Nested chats (for example the internal chat behind group-chat speaker selection, or an inner chat a tool starts) keep `gen_ai.conversation.id` but get no `session.id`. Child spans end before the chat id exists, so only the conversation span carries it. A `GroupChatManager` run emits no own conversation span at 0.14.x. `initiate_chats` gives one session per chat: each chat is its own outermost conversation, and the parent `initiate_chats` span (`multi_conversation`) has no `session.id`, so one trace holds several sessions. For one session over the whole call, wrap it in `with using_session("..."):`. |
| User | `user.id` (and `metadata`, `tag.tags`) from `using_user(...)` / `using_attributes(...)`, set on every AG2 span started inside it. | Not emitted upstream; absent without that context. |
| Tools | `gen_ai.tool.name`, `gen_ai.tool.type` (`function`), `gen_ai.tool.call.id`. | Arguments and result (`gen_ai.tool.call.arguments` / `.result`) only with `capture_content=True`. |
| Errors | Exceptions keep OTel's ERROR status. A model call that raises keeps ERROR, and upstream sets `error.type` to the exception class (for example `BadRequestError`, `llm_wrapper.py:97-101`); the span has no usage or cost. A failing tool is caught inside `ConversableAgent.execute_function`, so upstream only sets `error.type=ExecutionError`; this package sets status ERROR from `error.type`. A non-zero code exit (`error.type=CodeExecutionError`) also becomes ERROR. | |
| Retrieval | Not emitted upstream. | Always. |

## Privacy

Content is off unless you opt in with `setup(..., capture_content=True)`.

Upstream has a content flag only on `instrument_llm_wrapper`
(`capture_messages`, default False). Its agent, conversation, tool,
human-input and code-execution spans always record message bodies, tool
arguments and results, the human prompt and answer, code output and chat
summaries. With content off this package removes these keys before export:
`gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.tool.call.arguments`,
`gen_ai.tool.call.result`, `ag2.human_input.prompt`, `ag2.human_input.response`,
`ag2.code_execution.output`, `ag2.chats.summaries`, and any `input.value` /
`output.value`. That is stricter than upstream. With content on they are kept
and lifted into `input.value` / `output.value`.

The filtering lives in `AG2ClassicSpanProcessor`, which must run before any
exporting processor. `setup()` keeps it first on the provider:

- `trace_provider.add_span_processor(...)` after `setup()`: Future AGI's
  provider shuts down and removes every processor on the first such call after
  `register()` (`fi_instrumentation/otel.py:336-339`), this one included.
  `setup()` wraps that method on your provider instance, so after the call this
  processor is put back first and re-enabled. Your processor sees filtered
  spans.
- `tracing.uninstrument()` leaves the processor in place, because agents
  instrumented earlier keep emitting spans.
- Editing `_active_span_processor._span_processors` yourself bypasses this;
  call `setup()` again to put the processor back first.

## Attribute inventory (autogen 0.14.1)

Paths are relative to `autogen/opentelemetry/`. Scope name
`opentelemetry.instrumentation.ag2`, schema
`https://opentelemetry.io/schemas/1.11.0` (`consts.py:25-26`).

| Span (name) | `ag2.span.type` | Keys emitted | Source |
|---|---|---|---|
| `chat {model}` | `llm` | `gen_ai.operation.name=chat`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.agent.name`, `gen_ai.request.{temperature,max_tokens,top_p,frequency_penalty,presence_penalty}`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons`, `gen_ai.usage.cost`, `error.type`; messages only if `capture_messages` | `instrumentators/llm_wrapper.py:74-135,153`, `utils.py:213-222` |
| `invoke_agent {agent}` | `agent` | `gen_ai.operation.name=invoke_agent`, `gen_ai.agent.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.input.messages`, `gen_ai.output.messages` (always) | `instrumentators/agent_instrumentators/reply.py:36-58,75-95` |
| `invoke_agent {agent}` (remote) | `agent` | `gen_ai.agent.remote`, `server.address` | `instrumentators/agent_instrumentators/remote.py:43-48` |
| `conversation {agent}` | `conversation` | `gen_ai.operation.name=conversation`, `gen_ai.agent.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.conversation.max_turns`, `gen_ai.input.messages`, `gen_ai.conversation.id`, `gen_ai.conversation.turns`, `gen_ai.output.messages`, `gen_ai.usage.cost`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | `instrumentators/agent_instrumentators/chat.py:33-82,100-147` |
| `conversation {agent}` (resume, run_chat) | `conversation` | `gen_ai.conversation.resumed`; run_chat: messages | `instrumentators/agent_instrumentators/chat.py:167-170,191-237` |
| `agent.initiate_chats` / `initiate_chats` | `multi_conversation` | `gen_ai.operation.name=initiate_chats`, `ag2.chats.{count,mode,recipients,ids,summaries,prerequisites}` | `instrumentators/agent_instrumentators/chat.py:253-318` |
| `execute_tool {name}` | `tool` | `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, `gen_ai.tool.type`, `gen_ai.tool.call.id`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`, `error.type=ExecutionError` | `instrumentators/agent_instrumentators/tool.py:26-47,65-86` |
| `execute_code {agent}` | `code_execution` | `gen_ai.operation.name=execute_code`, `gen_ai.agent.name`, `ag2.code_execution.exit_code`, `ag2.code_execution.output`, `error.type=CodeExecutionError` | `instrumentators/agent_instrumentators/code.py:43-68` |
| `await_human_input {agent}` | `human_input` | `gen_ai.operation.name=await_human_input`, `gen_ai.agent.name`, `ag2.human_input.prompt`, `ag2.human_input.response` | `instrumentators/agent_instrumentators/human_input.py:24-32,48-55` |
| `speaker_selection` | `speaker_selection` | `gen_ai.operation.name=speaker_selection`, `ag2.speaker_selection.candidates`, `ag2.speaker_selection.selected` | `instrumentators/pattern.py:163-176,193-206` |
| `a2a-execution` | none | none | `instrumentators/a2a.py:61` |

## Troubleshooting

- No traces: `register()` needs `project_name` or `FI_PROJECT_NAME`; the
  collector drops a batch whose resource has no `project_name`.
- No LLM spans after a second `setup()` on another provider: upstream patches
  `OpenAIWrapper.create` once and keeps the first provider
  (`llm_wrapper.py:64-65`). Call `uninstrument()` on the first handle first.
- Calling `trace_provider.add_span_processor(...)` after `register()` drops
  Future AGI's default exporter; that is `fi_instrumentation` behaviour.
  `setup()` does not use it, and puts this package's processor back first
  when you call it (see Privacy).
- `setup()` raises and names three packages: you have Microsoft AutoGen, `ag2`
  1.x, or `pyautogen` instead of `autogen` 0.14.x.
- Content missing: content is off by default here; pass `capture_content=True`.
- Exporter errors are logged and never fail the agent.
