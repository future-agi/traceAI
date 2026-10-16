# traceAI A2A Go

OpenTelemetry tracing for A2A (Agent2Agent) protocol clients built with the official Go SDK,
[`github.com/a2aproject/a2a-go/v2`](https://github.com/a2aproject/a2a-go), sending spans to
[Future AGI](https://futureagi.com).

It is an `a2aclient.CallInterceptor`, the SDK's own extension point: you pass it with
`a2aclient.WithCallInterceptors` when you create the client. Each call to a remote agent becomes
one client span with `gen_ai.span.kind=AGENT` and the A2A attribute names that the Python
`traceai-a2a` package uses (`gen_ai.a2a.task.id`, `gen_ai.a2a.task.state`, `gen_ai.a2a.agent.url`,
...). The interceptor also sends the W3C `traceparent` (as an HTTP header on JSON-RPC and REST, as call
metadata on gRPC), so a remote agent that reads it continues the same trace.

| | |
|---|---|
| Module | `github.com/future-agi/traceAI/go/a2a` (package `traceaia2a`) |
| A2A SDK | `github.com/a2aproject/a2a-go/v2` v2.5.0 or later (the `go.mod` minimum); tested on v2.6.0, the latest on 2026-10-16, and v2.5.0 |
| Go | 1.26 or later (OpenTelemetry Go v1.47.0 and a2a-go v2.6.0 require it); tested on 1.26.9 and 1.27.2 |
| Transports | JSON-RPC, HTTP+JSON (REST) and gRPC (`a2agrpc/v1`), each tested against a2a-go's own server |
| Dependencies | the OpenTelemetry API (`go.opentelemetry.io/otel`, `.../trace`) and a2a-go. No exporter is built in. |

This is not the Python `traceai-a2a` package, which is unchanged. The Go module writes the same
`gen_ai.a2a.*` names where a2a-go reports the same fact; see [Attributes](#attributes) for the
differences.

## Install

```sh
go get github.com/future-agi/traceAI/go/a2a@latest
```

## Use

```go
// tp is an *sdktrace.TracerProvider that exports to Future AGI (see below).
client, err := a2aclient.NewFromCard(ctx, card,
	a2aclient.WithCallInterceptors(traceaia2a.NewInterceptor(traceaia2a.WithTracerProvider(tp))))

ctx = traceaia2a.ContextWithSessionID(ctx, "session-123") // optional
resp, err := client.SendMessage(ctx, &a2a.SendMessageRequest{Message: msg})
```

Without `WithTracerProvider` the OpenTelemetry global provider is used. Attach the interceptor
first when you also use other interceptors, so its span covers theirs.

Export with the stock OTLP/HTTP exporter. Future AGI needs both auth headers, the `project_name`
resource attribute and an uncompressed body:

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
defer tp.Shutdown(context.Background()) // flush before exit, and on SIGTERM
```

The Future AGI keys go only to the exporter. The interceptor never puts them on the request to the
remote agent; that request gets `traceparent` (and `tracestate` when set) only.

[`example/main.go`](example/main.go) is a complete program: an agent span, A2A calls (streamed
with `-stream`), a failing task (`-fail`), and a demo remote agent built with a2a-go's server
package whose own spans join the trace.

```sh
export FI_API_KEY=... FI_SECRET_KEY=... FI_PROJECT_NAME=my-service
go run ./example -stream -fail
```

## What is traced

| Client method | Span | `gen_ai.operation.name` |
|---|---|---|
| `SendMessage` | `a2a.SendMessage` | `a2a.send` |
| `SendStreamingMessage` | `a2a.SendStreamingMessage` | `a2a.send` |
| `GetTask` | `a2a.GetTask` | `a2a.get_task` |
| `CancelTask` | `a2a.CancelTask` | `a2a.cancel_task` |
| `SubscribeToTask` | `a2a.SubscribeToTask` | `a2a.subscribe_to_task` |

`ListTasks`, the push-notification config methods and `GetExtendedAgentCard` pass through without
a span.

A stream is one span. Every streamed event is a span event named `a2a.event` (with
`gen_ai.a2a.event.type`, and the task state or artifact type when the event has one), and the span
ends at the agent's reply message or when the task reaches a terminal state (`completed`,
`failed`, `canceled`, `rejected`) or an interrupted one (`input-required`, `auth-required`), which
is when a2a-go closes the stream.

## Attributes

| Attribute | When |
|---|---|
| `gen_ai.span.kind` = `AGENT`, `gen_ai.operation.name`, `gen_ai.a2a.streaming` | always |
| `gen_ai.a2a.agent.url` | the agent URL from the card, without user info, query or fragment |
| `gen_ai.a2a.agent.card.name`, `gen_ai.a2a.agent.card.version` | when the client has a card with them |
| `gen_ai.a2a.task.id` | only when the request or the response carries a task id (a direct message reply has none) |
| `gen_ai.a2a.task.state` | only when a2a-go reported a state, spelled as a2a-go spells it (`TASK_STATE_COMPLETED`, ...) |
| `gen_ai.a2a.message.role`, `gen_ai.a2a.message.parts.count` | for the message you send (`ROLE_USER`) |
| `gen_ai.a2a.artifact.type` | `text`, `file` or `data`, from the artifact's first part |
| `gen_ai.a2a.context.id` (Go only) | the A2A context id |
| `session.id` | the id from `ContextWithSessionID`; otherwise the A2A context id |
| `user.id` | the id from `ContextWithUserID` |
| `gen_ai.a2a.stream.event_count` (Go only) | streamed calls |
| `input.value` | the text and data parts you send (content) |
| `output.value` | artifact text, then the reply message or the final status message (content) |
| `error.type`, `error.message` | on errors, see below |

Python `traceai-a2a` writes `gen_ai.span.kind=A2A_CLIENT` and lower-case v0.3 state names
(`completed`). This module writes `AGENT`, which Future AGI shows as an agent span, and the a2a-go
v2 state strings. It never writes an LLM span: a model call inside the remote agent is not seen
here, and no token or cost attribute is invented.

## Errors and cancellation

- An error a2a-go returns ends the span with status ERROR and `error.type`: the A2A reason for
  protocol errors (`TASK_NOT_FOUND`, `UNSUPPORTED_OPERATION`, ...), `canceled` or `timeout` for the
  context, and `transport` for everything else (connection, HTTP status, decoding). The remote
  agent's own error message is added as `error.message` unless inputs or outputs are hidden.
  Transport error text (URLs, host names) is never recorded.
- A task the agent reports as `failed` or `rejected` is ERROR with `error.type` set to the state.
  `canceled`, `input-required` and `auth-required` are recorded in `gen_ai.a2a.task.state` and
  are not errors.
- A stream ends without a callback when you stop reading early. The span then ends, as ERROR
  `canceled`, when the context you passed is cancelled, so pass one you cancel (`defer cancel()`).
- The interceptor never changes a request, a response or an error, and never fails a call.

## Privacy

Content is captured by default, as in every traceAI instrumentor. The traceAI `TraceConfig`
switches, with the same names and defaults as Python, turn it off:

| Variable | Effect |
|---|---|
| `FI_HIDE_INPUTS` | `input.value` becomes `__REDACTED__`; `error.message` is dropped |
| `FI_HIDE_OUTPUTS` | `output.value` becomes `__REDACTED__`; `error.message` is dropped |
| `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT`, `FI_HIDE_INPUT_IMAGES`, `FI_HIDE_LLM_INVOCATION_PARAMETERS`, `FI_BASE64_IMAGE_MAX_LENGTH` | read for parity; they act on attributes this module does not write (and on spans you start yourself through `TraceConfig.SetAttributes`) |

A value counts only when it is `true` (any case). `WithTraceConfig(traceaia2a.TraceConfig{...})`
sets them in code instead. Task ids, states, the agent URL, the session id and the error class are
kept in every mode.

In every mode: file parts (raw bytes or file URLs) are never copied, credentials and query strings
in the agent URL are stripped, request headers are never copied, transport error text is never
recorded, values are cut at 32 000 bytes, and the module adds no resource attributes.

## Not supported

- Server-side tracing (`a2asrv`). The server interceptor is a different interface and is not part
  of this module; a remote agent joins the trace only if it reads `traceparent` itself (the
  example's demo agent shows how).
- Push notifications and the a2a CLI.

## Test

```sh
go test -race ./...
```

The tests run a real a2a-go server (`a2asrv` behind its JSON-RPC, REST and gRPC handlers) on
127.0.0.1 and the in-memory span recorder; no external network and no key. The example test checks the export request a fake Future AGI
collector receives (path, both headers, `project_name`, no gzip, one stitched trace).
