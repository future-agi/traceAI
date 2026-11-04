# traceAI Eino (Go)

OpenTelemetry tracing for [CloudWeGo Eino](https://github.com/cloudwego/eino) applications, sending spans to
[Future AGI](https://futureagi.com).

It is an Eino `callbacks.Handler`, the framework's own extension point. Register it once with
`callbacks.AppendGlobalHandlers` and every component run Eino reports becomes one span: graphs, chains and
workflows, their nodes, chat models, tools, retrievers, embedders, prompts, lambdas and ADK agents. A span's
parent is the span of the enclosing run, taken from the context Eino passes to the callback, so one graph or
agent run is one trace.

| | |
|---|---|
| Module | `github.com/future-agi/traceAI/go/eino` (package `traceaieino`) |
| Eino | `github.com/cloudwego/eino` v0.9.19 or later (the `go.mod` minimum) |
| Go | 1.26.0 or later, the common minimum across traceAI Go modules |
| Tested components | ADK `ChatModelAgent` through `adk.Runner` (streamed and not), `compose` graphs and chains, chat model nodes (the eino-ext OpenAI component and test models), `ToolsNode` and `InferTool` tools, lambdas (invoke, stream, collect), retriever, embedding and prompt callbacks |
| Dependencies | the OpenTelemetry API (`go.opentelemetry.io/otel`, `.../trace`) and Eino. No exporter is built in. |

This is not the OpenAI, Anthropic or Gemini Go module (`go/openai`, `go/anthropic`, `go/genai`): those wrap a
provider SDK's HTTP calls. This module reads Eino's callbacks, so it sees every component Eino runs, whatever
model provider is behind it. It does not replace or use Eino's own Langfuse, APMPlus or CozeLoop handlers
(`eino-ext/callbacks/*`); they can stay registered next to it.

## Install

**Release availability:** the command below is for tagged releases. Until the first
`go/eino/v0.1.0` tag exists, use `@dev` instead of `@latest`. See the
[Go SDK index](../README.md) for all modules and the release scheme.

```sh
go get github.com/future-agi/traceAI/go/eino@latest
```

## Use

```go
// tp is an *sdktrace.TracerProvider that exports to Future AGI (see below).
h := traceaieino.NewHandler(traceaieino.WithTracerProvider(tp))
callbacks.AppendGlobalHandlers(h) // once, before any graph or agent runs

ctx = traceaieino.ContextWithSessionID(ctx, "session-123") // optional
events := adk.NewRunner(ctx, adk.RunnerConfig{Agent: agent}).Query(ctx, "What should I pack for Kyoto?")
```

`AppendGlobalHandlers` is not thread-safe: call it once during start-up, before any graph or agent runs.
Registering the handler twice gives two spans per component. To trace a single run instead, pass the handler
to that run: `compose.WithCallbacks(h)` for a graph, `adk.WithCallbacks(h)` for an agent. Without
`WithTracerProvider` the OpenTelemetry global provider is used.

Export with the stock OTLP/HTTP exporter. Future AGI needs both auth headers, the `project_name` resource
attribute and an uncompressed body:

```go
exp, _ := otlptracehttp.New(ctx,
	otlptracehttp.WithEndpointURL("https://api.futureagi.com/tracer/v1/traces"),
	otlptracehttp.WithHeaders(map[string]string{
		"X-Api-Key":    os.Getenv("FI_API_KEY"),
		"X-Secret-Key": os.Getenv("FI_SECRET_KEY"),
	}),
	otlptracehttp.WithCompression(otlptracehttp.NoCompression),
)
res, _ := resource.Merge(resource.Default(), resource.NewSchemaless(
	attribute.String("project_name", "my-service"),
	attribute.String("project_type", "observe"),
))
tp := sdktrace.NewTracerProvider(sdktrace.WithResource(res), sdktrace.WithBatcher(exp))
```

On exit (and on SIGTERM) stop the handler, then flush and close the provider:

```go
_ = h.Shutdown(ctx)  // later callbacks are no-ops; waits for streamed spans still being finished
_ = tp.Shutdown(ctx) // exports the last batch
```

The Future AGI keys go only to the exporter. The handler never reads or writes credentials, and no component
configuration (API keys, base URLs) is copied onto spans.

[`example/main.go`](example/main.go) is a complete program: an ADK `ChatModelAgent` with the eino-ext OpenAI
chat model (any OpenAI-compatible endpoint through `OPENAI_BASE_URL`) and a `get_weather` tool, streamed with
`-stream`, plus a run whose model does not exist with `-fail`.

```sh
export FI_API_KEY=... FI_SECRET_KEY=... FI_PROJECT_NAME=my-service OPENAI_API_KEY=...
go run ./example -stream -fail
```

## What is traced

| `RunInfo.Component` | `gen_ai.span.kind` | `gen_ai.operation.name` |
|---|---|---|
| `ChatModel`, `AgenticModel` | `LLM` | `chat` |
| `Tool` | `TOOL` | `execute_tool` |
| `Agent`, `AgenticAgent` (ADK) | `AGENT` | `invoke_agent` |
| `Retriever` | `RETRIEVER` | |
| `Embedding` | `EMBEDDING` | `embeddings` |
| `Graph`, `Chain`, `Workflow`, `Lambda`, `Passthrough`, `ToolsNode`, `ChatTemplate`, `Indexer`, `Loader`, `DocumentTransformer` | `CHAIN` | |
| anything else (custom components) | `UNKNOWN`, kept with its name | |

The span name is the node, tool or agent name (`compose.WithNodeName`, the tool name, the agent name), otherwise
Eino's display name `{Type}{Component}` (for example `OpenAIChatModel`).

A streamed run (`Stream`, `Transform`) is one span: it ends when the handler has read its own copy of the
stream to the end. Chunks are not spans. An ADK agent's span ends when the agent's event stream ends, so it
covers the whole run. An ADK `ChatModelAgent` also shows Eino's internal ReAct graph (`ReAct`, `Init`,
`ChatModel`, `ToolNode`, `CancelCheck`, ...) as `CHAIN` spans, because Eino reports them.

## Attributes

| Attribute | When |
|---|---|
| `gen_ai.span.kind`, `gen_ai.operation.name`, `eino.component`, `eino.type`, `eino.name` | always (`eino.*` from `RunInfo`) |
| `gen_ai.request.model`, `gen_ai.request.max_tokens`, `gen_ai.request.temperature`, `gen_ai.request.top_p`, `gen_ai.request.stop_sequences`, `gen_ai.request.parameters` | when the chat model reports its config in the callback (eino-ext models do; a model the graph wraps reports only the messages). Zero values are not written: Eino cannot tell 0 from unset |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.total_tokens`, `gen_ai.usage.cache_read_tokens`, `gen_ai.usage.output_tokens.reasoning` | only when the payload reports a non-zero count. For a stream, from the chunk that reports it (the largest total when several do); chunks are never summed |
| `gen_ai.response.finish_reasons` | when the reply has one |
| `gen_ai.tool.definitions.{i}.tool.json_schema` | tools bound to the chat model |
| `gen_ai.input.messages.*`, `gen_ai.output.messages.*` | chat model spans (content) |
| `gen_ai.tool.name`, `gen_ai.tool.call.id` | tool spans |
| `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` | tool spans (content) |
| `gen_ai.agent.name`, `eino.agent.interrupted`, `eino.agent.resumed` | agent spans |
| `gen_ai.retrieval.query` (content), `gen_ai.retrieval.top_k`, `retrieval.documents.{i}.document.id`, `.document.score`, `.document.content` (content), `eino.retriever.documents.count` | retriever spans |
| `gen_ai.embeddings.dimension.count`, `eino.embedding.count` | embedding spans (the vectors are never written) |
| `input.value`, `output.value` (content) | every span whose input or output can be encoded |
| `session.id`, `user.id` | from `ContextWithSessionID` / `ContextWithUserID` on the context you pass to the run |
| `error.type`, `error.message` | on errors, see below |

`gen_ai.provider.name` is not written: `RunInfo.Type` names the component (`OpenAI`), not the endpoint behind it.

## Errors

- `OnError` ends the span with status ERROR and `error.type`: `canceled` or `timeout` for the context, otherwise
  the Go type of the innermost error (for example `*openai.APIError`), or `error` for a plain error. A later
  `OnEnd` for the same run is a no-op; concurrent `OnEnd` and `OnError` end it once.
- `error.message` is the first line of the error with URLs, email addresses, file paths, IP addresses,
  `host:port` pairs, and this machine's host name, user name and home directory replaced. It is dropped when
  inputs or outputs are hidden. No exception event and no stack trace are ever recorded.
- A stream that fails part-way ends its span ERROR. An agent whose event stream carries an error ends ERROR.
- A component that reports `OnError` without an `OnStart` (it failed while building its request) gets one ERROR
  span of its own. An `OnEnd` that does not belong to a run the handler started is ignored; it never ends
  another run's span.
- The handler never changes an input, an output or an error, never returns an error to Eino, and recovers from
  its own panics.

## Privacy

Content is captured by default, as in every traceAI instrumentor. The traceAI `TraceConfig` switches, with
the same names and defaults as Python, turn it off:

| Variable | Effect |
|---|---|
| `FI_HIDE_INPUTS` | `input.value` becomes `__REDACTED__`; input messages, tool-call arguments, the retriever query and `error.message` are dropped; streamed inputs are not read |
| `FI_HIDE_OUTPUTS` | `output.value` becomes `__REDACTED__`; output messages, tool-call results, retrieved document text and `error.message` are dropped |
| `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES` | drop `gen_ai.input.messages.*` / `gen_ai.output.messages.*` only |
| `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT` | redact message text, keep roles and tool calls |
| `FI_HIDE_INPUT_IMAGES`, `FI_BASE64_IMAGE_MAX_LENGTH` | drop input images / redact inline images longer than the limit (default 32 000) |
| `FI_HIDE_LLM_INVOCATION_PARAMETERS` | drop `gen_ai.request.parameters` |

A value counts only when it is `true` (any case); the variables are read once, in `NewHandler`.
`WithTraceConfig(traceaieino.TraceConfig{...})` sets them in code instead. Model names, token counts, tool and
agent names, document ids and error types are kept in every mode.

In every mode: audio, video and file bytes and embedding vectors are never copied, values are cut at 32 000
bytes, credentials and component configuration are never copied, and the module adds no resource attributes
(no host name, user name or local path).

## Not supported

- Components that do not fire Eino callbacks. The handler sees only what Eino reports. Tested: the eino-ext
  OpenAI chat model fires its own callbacks (with model name and usage), and a component used as a graph node
  is reported by the graph even when it fires none itself. A component that fires none, called outside a
  graph, produces no span.
- ADK interrupt/resume and checkpoints, DeepAgent, multi-agent transfer and workflow agents are not tested.
  They go through the same callbacks; the handler marks an agent span `eino.agent.interrupted` when an event
  carries an interrupt and `eino.agent.resumed` when the run resumes, but no test covers either path.
- `AgenticModel` / `AgenticAgent` (`*schema.AgenticMessage`) runs get their span kind; their content is written
  only as generic JSON in `input.value` / `output.value`, not as message attributes. Not tested.
- `gen_ai.usage.cache_write_tokens` (Eino v0.9.21 adds the field; the module supports v0.9.19).
- Cost. Future AGI computes it from the model name and token counts when it knows the model.

## Test

```sh
go mod tidy -diff
go vet ./...
go test -race ./...
go build -o /tmp/traceai-eino-example ./example
```

The tests run real Eino graphs, chains, lambdas, `ToolsNode` and ADK `ChatModelAgent` runs with a scripted chat
model and a real tool, against the in-memory span recorder; no network and no key. The example test runs
`example/main.go` against a fake OpenAI-compatible server and a fake Future AGI collector, and checks the export
request (path, both headers, `project_name`, no gzip, no key in the body, one trace per question, the failing
run as ERROR, and nothing of the conversation with `FI_HIDE_INPUTS` / `FI_HIDE_OUTPUTS` on).
