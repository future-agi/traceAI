# traceAI A2A Go

OpenTelemetry tracing for A2A (Agent2Agent) protocol clients and servers built with the official Go SDK,
[`github.com/a2aproject/a2a-go/v2`](https://github.com/a2aproject/a2a-go), sending spans to
[Future AGI](https://futureagi.com).

The client uses `a2aclient.CallInterceptor`, the SDK's own extension point: you pass it with
`a2aclient.WithCallInterceptors` when you create the client. Each call to a remote agent becomes
one client span with `gen_ai.span.kind=AGENT` and the A2A attribute names that the Python
`traceai-a2a` package uses (`gen_ai.a2a.task.id`, `gen_ai.a2a.task.state`, `gen_ai.a2a.agent.url`,
...). The interceptor also sends the W3C `traceparent` (as an HTTP header on JSON-RPC and REST, as call
metadata on gRPC). Add `NewServerInterceptor` to the receiving agent to continue the trace
with one server span per call, without copying context extraction into your application.

| | |
|---|---|
| Module | `github.com/future-agi/traceAI/go/a2a` (package `traceaia2a`) |
| A2A SDK | `github.com/a2aproject/a2a-go/v2` v2.7.0 or later (the `go.mod` minimum) |
| Go | 1.26.0 or later, the common minimum across traceAI Go modules |
| Transports | JSON-RPC, HTTP+JSON (REST) and gRPC (`a2agrpc/v1`), each tested against a2a-go's own server |
| Dependencies | the OpenTelemetry API (`go.opentelemetry.io/otel`, `.../trace`) and a2a-go. No exporter is built in. |

This is not the Python `traceai-a2a` package, which is unchanged. The Go module writes the same
`gen_ai.a2a.*` names where a2a-go reports the same fact; see [Attributes](#attributes) for the
differences.

## Install

**Release availability:** the command below is for tagged releases. Until the first
`go/a2a/v0.1.0` tag exists, use `@dev` instead of `@latest`. See the
[Go SDK index](../README.md) for all modules and the release scheme.

```sh
go get github.com/future-agi/traceAI/go/a2a@latest
go get go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp@v1.47.0 go.opentelemetry.io/otel/sdk@v1.47.0
```

The second command installs the exporter and SDK used by the setup below and resolves their
`go.sum` entries. The integration itself does not create an exporter.

## Use

```go
// tp is an *sdktrace.TracerProvider that exports to Future AGI (see below).
sdkClient, err := a2aclient.NewFromCard(ctx, card,
	a2aclient.WithCallInterceptors(traceaia2a.NewInterceptor(traceaia2a.WithTracerProvider(tp))))

if err != nil {
	return err
}
client := traceaia2a.WrapClient(sdkClient)
defer client.Destroy()

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
package whose maintained server interceptor joins the trace. When it calls `api.openai.com`, the demo marks its own LLM
span with `gen_ai.provider.name=openai`; other OpenAI-compatible hosts remain unlabelled rather than
guessing a vendor. The A2A interceptor itself still never creates an LLM span or invents a provider.

```sh
export FI_API_KEY=... FI_SECRET_KEY=... FI_PROJECT_NAME=my-service
go run ./example -stream -fail
```

## Trace the receiving agent

Use the same tracer provider and content settings with the official server hook.
Keep a2a-go at v2.7.0 or later: earlier versions skip the completion hook on required-extension
rejections, leaving unary server spans open or incorrectly marked as canceled.
The module's dependency minimum selects the fixed SDK automatically.

Attach the interceptor when creating your handler:

```go
handler := traceaia2a.WrapHandler(a2asrv.NewHandler(executor,
	a2asrv.WithCallInterceptors(traceaia2a.NewServerInterceptor(
		traceaia2a.WithTracerProvider(tp)))))
httpHandler := a2asrv.NewJSONRPCHandler(handler)
```

Here `executor` is your `a2asrv.AgentExecutor`. Serve `httpHandler` using `net/http`.
For REST use `a2asrv.NewRESTHandler(handler)`; for gRPC use
`a2agrpc.NewHandler(handler).RegisterWith(grpcServer)` from `a2agrpc/v1`.
Attach the interceptor once, first in the list. It traces protocol calls, not agent-card
requests, transport parsing/authentication failures that occur before the hook, or separate
executor lifetimes. A background task may outlive a nonblocking request's span.

When you add middleware that can reject a response by returning an error from `After`,
wrap each such interceptor with `tracing.Wrap`. For example, with your existing `outputPolicy`:

```go
tracing := traceaia2a.NewServerInterceptor(traceaia2a.WithTracerProvider(tp))
handler := traceaia2a.WrapHandler(a2asrv.NewHandler(executor,
	a2asrv.WithCallInterceptors(tracing, tracing.Wrap(outputPolicy))))
```

The SDK stops the remaining `After` callbacks on rejection. The wrapper records the error
and ends the server span before returning the same error; it does not change the response,
run skipped policy callbacks or add another span. Without the wrapper, a rejected call can
leave its server span open until cancellation, which cannot recover the policy error.
Middleware that instead sets `resp.Err`, clears `resp.Payload` and returns `nil` does not
short-circuit the callbacks and needs no wrapper.

The executor receives a context containing the server span. Start any application/model
spans from that context; do not re-extract `traceparent` or add a second A2A server span.
An existing local transport span is kept as the parent. `WithPropagator(nil)` disables incoming
extraction; the default extracts W3C trace context only, not baggage. Session/user context helpers
are process-local: the A2A context ID becomes the server's `session.id` unless set on its context.
Headers, authenticated user details and request metadata are never copied to spans.

Use `WrapClient` and `WrapHandler` for streaming calls. They observe normal iterator exhaustion
and early abandonment because the SDK does not call `After` on iterator close. A persisted
nonterminal task snapshot can reach clean EOF without a final event; its span completes as OK,
not canceled. Breaking out early completes an unfinished span as ERROR/canceled and cancels only
the wrapper's child context. The caller's context is not canceled. Continue passing a deadline
or cancellable context for blocked reads and remote disconnects.

The wrappers complete the interceptor's existing span, without adding spans or changing events.
Wrap the outermost server handler (after any `a2asrv.InterceptedHandler` layers). The bare
interceptor APIs remain available, but without the completion wrappers they can only finish
streams on a terminal event or context cancellation, not on clean EOF.

## What is traced

| Client and server method | Span | `gen_ai.operation.name` |
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
`failed`, `canceled`, `rejected`) or `input-required`, which is when a2a-go closes the stream.
With `WrapClient` and `WrapHandler`, `auth-required` keeps an active stream's span open for later
events and its final result. Clean EOF also ends the span, including a persisted snapshot with no
active executor. The latest snapshot/status message is included in `output.value`, including an
authorization prompt at EOF; a later status replaces it.

Existing clients using only `WithCallInterceptors(NewInterceptor(...))` retain their previous
behavior: an `auth-required` task or status event ends the client span successfully. This keeps
persisted authorization snapshots complete without requiring a client migration, but misses later
events if the same stream continues. Use `WrapClient` to capture those events and to observe clean
EOF and early iterator abandonment.

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
| `output.value` | artifact text, then the reply or status message (current snapshot for unary calls, final status for streams; content) |
| `error.type`, `error.message` | on errors, see below |

Python `traceai-a2a` writes `gen_ai.span.kind=A2A_CLIENT` and lower-case v0.3 state names
(`completed`). This module writes `AGENT`, which Future AGI shows as an agent span, and the a2a-go
v2 state strings. It never writes an LLM span: a model call inside the remote agent is not seen
here, and no token or cost attribute is invented.

## Errors and cancellation

- An error a2a-go returns ends the span with status ERROR and `error.type`: the A2A reason for
  protocol errors (`TASK_NOT_FOUND`, `UNSUPPORTED_OPERATION`, ...), `canceled` or `timeout` for the
  context, and `transport` for everything else (connection, HTTP status, decoding). The remote
  agent's own error message is added as `error.message` unless the explicit or derived
  error-details policy hides it. Hidden errors retain ERROR status and `error.type`, with an
  empty status description. Transport error text (URLs, host names) is never recorded.
- A task the agent reports as `failed` or `rejected` is ERROR with `error.type` set to the state.
  `canceled`, `input-required` and `auth-required` are recorded in `gen_ai.a2a.task.state` and
  are not errors.
- With `WrapClient` / `WrapHandler`, normal EOF completes an unfinished span successfully;
  stopping iteration early ends it as ERROR `canceled`. Without them, early abandonment requires
  caller context cancellation to finish the span, and clean EOF cannot be distinguished.
- The interceptor never changes a request, a response or an error, and never fails a call.

## Privacy

Content is captured by default, as in every traceAI instrumentor. The traceAI `TraceConfig`
switches, with the same names and defaults as Python, turn it off:

| Variable | Effect |
|---|---|
| `FI_HIDE_INPUTS` | `input.value` becomes `__REDACTED__`; derives error-detail hiding |
| `FI_HIDE_OUTPUTS` | `output.value` becomes `__REDACTED__`; derives error-detail hiding |
| `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT` | hide matching message attributes on your own spans through `TraceConfig.SetAttributes`; also derive error-detail hiding |
| `FI_HIDE_ERROR_DETAILS` | `true` drops `error.message`, `exception.message`, `exception.stacktrace` and the ERROR status description; `false` explicitly allows error details, even when content is hidden |
| `FI_HIDE_INPUT_IMAGES`, `FI_HIDE_LLM_INVOCATION_PARAMETERS`, `FI_BASE64_IMAGE_MAX_LENGTH` | act on matching attributes you set through `TraceConfig.SetAttributes`; do not derive error-detail hiding |

Content-hiding switches enable only for `true` (any case). `WithTraceConfig(traceaia2a.TraceConfig{...})`
sets them in code instead. Task ids, states, the agent URL, the session id and the error class are
kept in every mode. When `FI_HIDE_ERROR_DETAILS` is unset, empty or invalid, error details
are hidden if any of the six input/output, message or text switches is true. In code,
`TraceConfig.HideErrorDetails` is a `*bool`: nil derives that policy; a pointer to true/false
overrides it. `WithTraceConfig` replaces environment settings for that interceptor.

In every mode: file parts (raw bytes or file URLs) are never copied, credentials and query strings
in the agent URL are stripped, request headers are never copied, transport error text is never
recorded, values are cut at 32 000 bytes, and the module adds no resource attributes.

## Not supported

- Push notifications and the a2a CLI.

## Test

```sh
go mod tidy -diff
go vet ./...
go test -race ./...
go build -o /tmp/traceai-a2a-example ./example
```

The tests run a real a2a-go server (`a2asrv` behind its JSON-RPC, REST and gRPC handlers) on
127.0.0.1 and the in-memory span recorder; no external network and no key. The example test checks the export request a fake Future AGI
collector receives (path, both headers, `project_name`, no gzip, one stitched trace).
