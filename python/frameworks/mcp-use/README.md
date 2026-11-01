# traceAI-mcp-use

OpenTelemetry tracing for [mcp-use](https://pypi.org/project/mcp-use/)
agents (`mcp_use.MCPAgent`), sent to Future AGI.

`FutureAGICallback` is a LangChain callback handler. You pass it to the
agent with `MCPAgent(..., callbacks=[...])`. It records one span for each
agent run, each LLM call and each tool call. It patches nothing, imports
neither `langfuse` nor `traceai_mcp`, and sets no environment variables.

This package traces the agent loop. [`traceAI-mcp`](../mcp) (`traceai_mcp`)
works at the MCP transport level and is a different layer: a green
transport test does not show the agent run, its model calls or its tool
calls. See [Other tracing in the same process](#other-tracing-in-the-same-process).

## Installation

```bash
pip install traceAI-mcp-use
```

It accepts `mcp-use>=1.7.1,<2`, `langchain-core>=1.0.0,<2` and
`fi-instrumentation-otel>=1.1.0`, on Python `>=3.11,<3.14` (mcp-use itself
requires 3.11). The suite runs with mcp-use 1.7.1 (the latest release on
2026-10-16) on Python 3.11, 3.12 and 3.13, and was run end to end against a
real model and a real stdio MCP server with mcp-use 1.7.1, langchain-core
1.6.12, langchain-openai 1.7.2 and langgraph 1.2.16.

## Usage

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from mcp_use import MCPAgent, MCPClient
from traceai_mcp_use import FutureAGICallback

trace_provider = register(project_type=ProjectType.OBSERVE, project_name="mcp-use-agent")

client = MCPClient.from_dict({"mcpServers": {"calculator": {"command": "python", "args": ["server.py"]}}})
agent = MCPAgent(
    llm=llm,  # any LangChain chat model
    client=client,
    callbacks=[FutureAGICallback(tracer_provider=trace_provider)],
)
result = await agent.run("What is 2 + 3?")
```

Pass the provider `register()` returns: `register()` does not set the
global provider unless you ask it to, and without `tracer_provider` the
callback uses the global one. One callback can serve many agents and
concurrent runs. See [`examples/agent_with_tools.py`](examples/agent_with_tools.py).

`MCPAgent.run()`, `stream()` and `stream_events()` are all traced. The
Python `MCPAgent` is async only.

## Spans

| Span | Name | `gen_ai.span.kind` | One per |
|---|---|---|---|
| Agent | `mcp_use.agent` | `AGENT` | `run()`, `stream()` or `stream_events()` call |
| LLM | `chat <model>` (`text_completion <model>` for completion models; `chat` when no model name is reported) | `LLM` | model call |
| Tool | `execute_tool <tool>` | `TOOL` | tool call |

The LLM and tool spans are children of the agent span. In the LangChain
run tree, LangGraph's `model` and `tools` nodes sit between them; graph
nodes and middleware are not spans, so a tool span is a sibling of the LLM
spans, not a child of the LLM call that requested it. The agent span is a
child of the span that is current when the run starts, or a root span.

A streamed completion is one LLM span, ended when the stream ends. Each
token LangChain reports is a `mcp_use.llm.chunk` event carrying only its
index (`mcp_use.llm.chunk.index`, never the text), for the first 128
tokens; `mcp_use.llm.chunk_count` holds the exact number.

The spans are not made current while their run executes, as in
`traceAI-langchain`: a span another instrumentation starts inside a tool
call (for example an HTTP client span) is not a child of the tool span.

## Span attributes

| Attribute | Span | Value |
|---|---|---|
| `gen_ai.operation.name` | all | `invoke_agent`, `chat`, `text_completion` or `execute_tool` |
| `mcp_use.agent.llm_call_count`, `mcp_use.agent.tool_call_count`, `mcp_use.agent.tool_error_count` | agent | Model calls, tool calls and failed tool calls in the run |
| `gen_ai.request.model` | LLM | From LangChain's `ls_model_name`, else the invocation `model` / `model_name` / `model_id` |
| `gen_ai.provider.name`, `gen_ai.request.temperature`, `gen_ai.request.max_tokens` | LLM | From LangChain's `ls_provider`, `ls_temperature`, `ls_max_tokens` |
| `gen_ai.response.model`, `gen_ai.response.finish_reasons` | LLM | From the response metadata |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.total_tokens`, `gen_ai.usage.cache_read_tokens` | LLM | From the message's `usage_metadata` (or the provider's `token_usage`) |
| `mcp_use.llm.input_message_count` | LLM | Messages sent to the model |
| `mcp_use.llm.tool_call_count` | LLM | Tool calls the model asked for |
| `mcp_use.llm.chunk_count` | LLM | Streamed tokens, when streamed |
| `gen_ai.tool.name`, `gen_ai.tool.call.id` | tool | The tool and the model's tool-call id |
| `mcp_use.tool.error_type` | tool | Error type, when the tool's failure came back as its result |
| `mcp_use.cancelled`, `mcp_use.incomplete` | any | See [Errors and cancellation](#errors-and-cancellation) |

A value the callback is not given is omitted, never written as 0. Content
is recorded too, unless a `TraceConfig` hide flag turns it off (see
[Privacy](#privacy)):

| Attribute | Span | Value |
|---|---|---|
| `input.value` | agent | The last user message of the run's input |
| `output.value` | agent | The final answer (the last assistant message without tool calls) |
| `gen_ai.input.messages.<i>.message.role` / `.content` / `.tool_calls.<j>.tool_call.*` | LLM | The 32 most recent input messages |
| `gen_ai.output.messages.<i>.message.*` | LLM | The model's reply, with up to 16 tool calls (name, id, arguments) |
| `input.value`, `output.value` | LLM | The last input message's text and the reply's text |
| `gen_ai.tool.call.arguments`, `input.value` | tool | The arguments, as JSON |
| `gen_ai.tool.call.result`, `output.value` | tool | The tool's result text |

Spans come from an `fi_instrumentation.FITracer`, so the attributes of
`using_session`, `using_user`, `using_metadata`, `using_tags` and
`using_attributes` (`session.id`, `user.id`, `metadata`, `tag.tags`) are
set on every span of a run started inside them:

```python
from fi_instrumentation import using_metadata, using_session, using_tags, using_user

with using_session("session-1"), using_user("user-1"), using_metadata({"tenant": "t1"}), using_tags(["beta"]):
    await agent.run("What is 2 + 3?")
```

The Python `MCPAgent` has no `set_metadata` or `set_tags` (the TypeScript
agent does); use these context managers instead.

## Privacy

Content follows `TraceConfig`, like every traceAI instrumentor: prompts,
messages, model output, tool arguments, tool results and error messages are
recorded by default, and the standard hide flags turn them off. There is no
separate switch in this package.

```bash
export FI_HIDE_INPUTS=true    # prompts, input messages, tool arguments
export FI_HIDE_OUTPUTS=true   # model output, output messages, tool results
```

or `FutureAGICallback(tracer_provider=..., config=TraceConfig(hide_inputs=True, hide_outputs=True))`.
With both flags on, spans carry names, kinds, the model and provider,
request parameters, usage, finish reasons, counts, the tool-call id,
statuses and exception types; `input.value` and `output.value` are
`__REDACTED__`.

Never recorded, whatever the flags:

- stack traces. The `exception` event has `exception.type` and, when
  content is on, `exception.message`; never `exception.stacktrace`. mcp-use
  sends a failed tool call back to the model as `format_error()`'s dict,
  whose `stack` is a full traceback; in that tool message the trace is
  replaced with `[stack trace removed]` and the rest of the JSON is kept.
  Any other text loses everything from `Traceback (most recent call last):`
  on;
- absolute local paths (`/Users/...`, `/home/...`, `/tmp/...`, `C:\...`
  and similar), replaced with `[path]`;
- this machine's user name and host name, replaced with `[user]` and
  `[host]` (names of 4 characters or more that are not generic, such as
  `root` or `app`).

```python
FutureAGICallback(
    tracer_provider=trace_provider,
    config=TraceConfig(hide_inputs=True),  # optional; default reads FI_HIDE_* from the environment
    redact=["my-mcp-server-token"],        # extra secret strings to remove
)
```

`redact` must be an iterable of strings; anything else raises `TypeError`.

Secrets are removed from every text the callback writes, including names
and ids, with content on or hidden. They are replaced by `[redacted]`:

- the strings passed as `redact=`;
- the value (8 characters or longer) of every environment variable whose
  name contains `KEY`, `SECRET`, `TOKEN`, `PASSWORD`, `PASSWD`,
  `CREDENTIAL` or `AUTH`, read each time a text is written. That covers
  provider keys such as `OPENAI_API_KEY` and `FI_API_KEY`/`FI_SECRET_KEY`;
- `Bearer`/`Basic` credentials, `sk-`/`pk-`/`rk-` style keys and
  `api_key=`, `token:`, `password=` or `Authorization:` assignments.

The callback never reads LangChain's serialized model payload (which can
include the model's repr, and so its key) or the invocation parameters
other than the model name.

`TraceConfig` settings apply. Pass `config=TraceConfig(...)` or set the
environment variables before creating the callback:

| Setting | Effect |
|---|---|
| `hide_inputs` / `FI_HIDE_INPUTS=true` | `input.value` is `__REDACTED__`; input messages and tool-call arguments (on tool spans and in model replies) are dropped. Every other text of the run has each input it has seen replaced with `__REDACTED__`: the user messages, the messages sent to the model, and each string value of each tool call's arguments. Error messages are dropped: the status and the `exception` event carry the exception type only. |
| `hide_outputs` / `FI_HIDE_OUTPUTS=true` | `output.value` is `__REDACTED__`; output messages and tool results are dropped. Model replies and tool results sent back to the model as input messages (later in the same run, or from earlier turns; see below) keep their role but lose their content and tool-call arguments, and an LLM span whose last input message is one of them has `input.value` `__REDACTED__`. Every other text of the run has each output it has seen (model replies, their tool-call arguments, tool results, earlier turns' included) replaced with `__REDACTED__`. Error messages are dropped: the status and the `exception` event carry the exception type only. |
| `hide_input_messages`, `hide_output_messages`, `hide_input_text`, `hide_output_text` (`FI_HIDE_INPUT_MESSAGES`, ...) | Applied by `FITracer` to the `gen_ai.input.messages.*` / `gen_ai.output.messages.*` attributes, as for every traceAI instrumentor. |
| `pii_redaction` / `FI_PII_REDACTION=true` | Emails, phone numbers, SSNs, card numbers, IPv4 addresses and `sk-`/`pk-` keys become tokens such as `<EMAIL_ADDRESS>` in every recorded text: attributes, the error status and the `exception` event. It runs after secrets (and hidden inputs/outputs) are removed and before the size caps. |

`config` must be a `fi_instrumentation.TraceConfig`; anything else raises
`TypeError`. Other `TraceConfig` fields act only through `FITracer`'s
attribute masking.

mcp-use's own console logging is not part of the trace and is not changed
by these flags: at its default level it prints query previews and tool
results (INFO), and a failed tool call's formatted error with its stack
trace (ERROR, `mcp_use/errors/error_formatting.py`). `Logger.set_debug(0)`
(`from mcp_use import Logger`) hides the INFO lines;
`logging.getLogger("mcp_use").setLevel(logging.CRITICAL)` also hides the
ERROR line.

Multi-turn conversations: `MCPAgent` keeps memory by default
(`memory_enabled=True`) and sends the earlier turns, model replies and tool
results included, back to the model as input on the next `run()`,
`stream()` or `stream_events()` call (`mcp_use/agents/mcpagent.py:76`,
`:724-732`, `:879-880`, `:981-983`); `external_history` does the same.
Each call is its own agent span. Under `hide_outputs`, every assistant- or
tool-role message (any role other than user and system) among a run's
input messages counts as an output: its content and tool-call arguments
are dropped from the input messages and removed from every other text of
that run, error text included. The callback reads them when the run
starts, before it writes any of the run's spans. This is
tested over three turns on one agent through `run()`, `stream()`,
`stream_events()` and `external_history`, and on the export. User messages
of earlier turns are inputs: they are recorded unless `hide_inputs` is on.
The texts a run remembers include its replayed history, so in a long
conversation the per-run limit below is reached sooner.

The hide flags match each text verbatim, and also in its Python-repr and
JSON-quoted forms; a copy that was escaped differently, cut or reworded
stays. Texts shorter than 3 characters are only matched in quoted form,
and non-string argument values (numbers, booleans) are not matched. With a
hide flag on, the callback remembers at most 2048 text forms or about 4
million characters per run; past that it fails closed and records no
content for the rest of that run (no messages, arguments, results or
error messages).

## Errors and cancellation

A failed tool call is a tool span with status ERROR and one `exception`
event. mcp-use 1.7.1 does not raise for a failed MCP call: it returns a
formatted error to the model as the tool's result
(`mcp_use/agents/adapters/langchain_adapter.py:181-206`). The callback
recognises that result (an object with mcp-use's `error`, `details`,
`stack` and `code` keys and a `tool` key equal to the tool's name; a tool's
own result with those keys but no matching `tool` is a result), takes the
error type from it
(`mcp_use.tool.error_type`) and counts it in
`mcp_use.agent.tool_error_count`. A tool call LangChain reports as failed
(for example arguments that fail validation) is recorded the same way,
with the exception's type. Either way the model sees the error and the run
usually goes on, so the agent span ends OK when the run returns an answer.

When the run itself fails (for example the model call raises), the LLM
span and the agent span end with status ERROR and an `exception` event,
and the exception reaches your code unchanged.

By default the status description is `<type>: <message>` and the event
carries `exception.type` and `exception.message` (cut to 1 KB, cleaned as
described in [Privacy](#privacy): secrets, local paths, user and host name
removed). With `hide_inputs` or `hide_outputs` both carry the exception type
only. No stack trace is recorded.

Cancelling the task that runs the agent (`asyncio.CancelledError`) or
leaving `stream()` before it ends (`GeneratorExit`) ends the open spans
with status ERROR, description `cancelled` and `mcp_use.cancelled` =
`true`, without an exception event. A span whose own end never came
because its parent ended first is ended with `mcp_use.incomplete` =
`true` and status ERROR, `ended without a result`.

If the callback itself fails (starting a span, reading a payload, setting
an attribute), it logs at debug level and the agent goes on unchanged.
Inside `fi_instrumentation.suppress_tracing()` it records nothing.

## Turning it off

Leave the callback out. `callbacks=[]` is the Python switch for no
callbacks at all: mcp-use's `ObservabilityManager` returns that empty list
as is and does not add its default Langfuse handler
(`mcp_use/agents/observability/callbacks_manager.py:72-74`). With
`callbacks=None`, mcp-use uses its Langfuse handler when Langfuse is
configured. There is no `observe=False` on the Python `MCPAgent` (that is
the TypeScript agent).

## Other tracing in the same process

- **traceAI-mcp (`traceai_mcp`)** works at the MCP transport. Version
  0.1.2 records no spans of its own; it carries trace context in the
  `_meta` of MCP requests. With both packages installed you get the same
  spans as with this one alone: one agent span and one tool span per tool
  call, none duplicated (tested). Because this package's spans are not made
  current, the context `traceai_mcp` sends is whichever span is current in
  your code. mcp-use binds `stdio_client` when it is imported
  (`mcp_use/client/task_managers/stdio.py:12`), so run
  `MCPInstrumentor().instrument()` before importing `mcp_use` for
  `traceai_mcp`'s stdio wrapper to apply.
- **traceAI-langchain** adds its own tracer to every LangChain callback
  manager, so an `MCPAgent` run would also produce a full LangChain tree
  (graph, nodes, model and tool runs). Use one of the two for an agent.
- **mcp-use's own observability**: the Langfuse handler is used only when
  `callbacks` is `None` (see above). Laminar is set up when `mcp_use` is
  imported if `LAMINAR_PROJECT_API_KEY` is set and `lmnr` is installed,
  whatever `callbacks` holds (`mcp_use/agents/observability/__init__.py:6`,
  `laminar.py:19-37`); `MCP_USE_LAMINAR=false` turns it off.
- **mcp-use's usage telemetry** sends anonymized product events to PostHog
  and Scarf, not spans; this package does not change it.
  `MCP_USE_ANONYMIZED_TELEMETRY=false` turns it off
  (`mcp_use/telemetry/telemetry.py:139`).

## Limits

- Recorded text is cut on a UTF-8 character boundary after it is cleaned
  (secrets, then hidden inputs/outputs, then PII): 4 KB for each content
  attribute, 256 bytes for names and ids (model, provider, tool name,
  tool-call id, exception type) and 1 KB for the error message in the
  status and the `exception` event. With `pii_redaction`, `FITracer` runs its own PII pass on every
  attribute after the cut; a token such as `<PHONE_NUMBER>` can make an
  attribute a few bytes longer than its cap. The status and events are not
  passed through it. At most 32 messages and 16 tool calls per message are
  recorded per LLM span, and 128 chunk events per streamed LLM span.
- One callback tracks at most 10,000 open runs. Past that, the oldest
  agent run is ended with `mcp_use.incomplete` and forgotten. Ending a run
  looks only at the open runs of its own agent run, not at other agents'.
- With `use_server_manager=True`, mcp-use restarts the graph run when the
  tool set changes during a run, up to 3 times
  (`mcp_use/agents/mcpagent.py:741-876`); each restart is a new agent span.
  The server manager's own tools return their failures as mcp-use errors
  without a `tool` key (`mcp_use/agents/managers/tools/connect_server.py:73`,
  `disconnect_server.py:39`, `list_servers_tool.py:47`), so those tool spans
  end OK with the error as their result. This path is not covered by the
  tests.
- With `output_schema`, mcp-use formats the answer after the graph run with
  a model call that gets no callbacks (`mcp_use/agents/mcpagent.py:595`),
  so that call has no span.
- A remote agent (`MCPAgent(agent_id=...)`) never reads `callbacks`
  (`mcp_use/agents/mcpagent.py:117-120`) and is not traced.
- The Python `MCPAgent` has no `flush()`. The provider `register()`
  returns exports what is left when the process exits; in a long-running
  process call `trace_provider.force_flush()` when you need the spans now.
- The callback runs on the agent's event loop, not in a thread. With
  `register(batch=False)`, each span is exported synchronously on that loop
  as it ends.
- Passed to a LangChain runnable other than `MCPAgent`, the callback still
  names the outermost run `mcp_use.agent`.
- Tested with mcp-use 1.7.1 only (the latest release on 2026-10-16).

## Tests

From the repository root:

```bash
env -u PYTHONPATH PYTHONPATH="python/frameworks/mcp-use:python:python/tests" \
  uv run --no-project --python 3.11 \
  --with pytest --with pytest-asyncio --with opentelemetry-api --with opentelemetry-sdk \
  --with opentelemetry-instrumentation --with opentelemetry-exporter-otlp-proto-http \
  --with wrapt --with requests --with jsonschema --with protobuf --with opentelemetry-proto \
  --with 'mcp-use==1.7.1' \
  pytest python/frameworks/mcp-use/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

Nothing calls a real model, MCP server or Future AGI. The LLM is a
scripted LangChain chat model. The MCP server is the FastMCP server in
`tests/_mcp_server.py`, on a 127.0.0.1 port for most tests and over stdio
for the contract test; the example test uses the example's own stdio
calculator server. Those send spans through
`fi_instrumentation.register()` to the shared `harness.Receiver` on
127.0.0.1 and check the `/tracer/v1/traces` path, both auth headers,
`project_type=observe`, that content is exported by default and not with
`FI_HIDE_INPUTS`/`FI_HIDE_OUTPUTS`, and that no key, stack trace or local
path is exported either way.
The example test runs `examples/agent_with_tools.py` as its own process,
once alone and once with `traceai_mcp` instrumented. The suite turns off
mcp-use's usage telemetry.
