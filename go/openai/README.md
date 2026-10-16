# traceAI OpenAI Go

OpenTelemetry tracing for the official OpenAI Go SDK,
[`github.com/openai/openai-go/v3`](https://github.com/openai/openai-go), sending spans to
[Future AGI](https://futureagi.com).

It is an `option.Middleware`: the SDK keeps its own HTTP client and transport policy, and every
Chat Completions call (streaming or not) becomes one client span named `chat <model>` with the
same attribute names `traceai-openai` writes in Python (`gen_ai.span.kind=LLM`,
`gen_ai.request.model`, `gen_ai.usage.*`, `gen_ai.input.messages.*`, `gen_ai.output.messages.*`,
`input.value`, `output.value`, `session.id`, `user.id`).

| | |
|---|---|
| Module | `github.com/future-agi/traceAI/go/openai` (package `traceaiopenai`) |
| OpenAI SDK | `github.com/openai/openai-go/v3`, tested on v3.78.0 and v3.71.1 |
| Go | 1.26 or later (openai-go v3.78.0 and OpenTelemetry Go v1.47.0 require it); tested on 1.26.9 and 1.27.2 |
| Dependencies | the OpenTelemetry API (`go.opentelemetry.io/otel`, `.../trace`) and openai-go. No exporter is built in. |

This is not `openinference-instrumentation-openai-go` from Arize. That module targets
`github.com/openai/openai-go` v1 (a different import path) and writes OpenInference names.

## Install

```sh
go get github.com/future-agi/traceAI/go/openai@latest
```

## Use

```go
// tp is an *sdktrace.TracerProvider that exports to Future AGI (see below).
client := openai.NewClient(
	option.WithMiddleware(traceaiopenai.Middleware(traceaiopenai.WithTracerProvider(tp))),
)
ctx = traceaiopenai.ContextWithSessionID(ctx, "session-123") // optional
resp, err := client.Chat.Completions.New(ctx, params)
```

Without `WithTracerProvider` the OpenTelemetry global provider is used.

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
defer tp.Shutdown(context.Background()) // flush before exit
```

[`example/main.go`](example/main.go) is a complete program: an agent span, a tool call, a
streamed answer and a failing call.

```sh
export FI_API_KEY=... FI_SECRET_KEY=... FI_PROJECT_NAME=my-service OPENAI_API_KEY=...
go run ./example -stream -fail
```

## Privacy

Content is captured by default, as in every traceAI instrumentor. The traceAI `TraceConfig`
switches, with the same names and defaults as Python, turn it off:

| Variable | Effect |
|---|---|
| `FI_HIDE_INPUTS` | `input.value` becomes `__REDACTED__`; input messages, system instructions, tool-call arguments and the API error message are dropped |
| `FI_HIDE_OUTPUTS` | `output.value` becomes `__REDACTED__`; output messages, tool-call results and the API error message are dropped |
| `FI_HIDE_INPUT_MESSAGES` / `FI_HIDE_OUTPUT_MESSAGES` | drop `gen_ai.input.messages.*` / `gen_ai.output.messages.*` only |
| `FI_HIDE_INPUT_TEXT` / `FI_HIDE_OUTPUT_TEXT` | redact the message text, keep roles and tool calls |
| `FI_HIDE_INPUT_IMAGES` | drop image parts |
| `FI_HIDE_LLM_INVOCATION_PARAMETERS` | drop `gen_ai.request.parameters` |
| `FI_BASE64_IMAGE_MAX_LENGTH` | redact `data:` images longer than this (default 32000) |

A value counts only when it is `true` (any case). `WithTraceConfig(traceaiopenai.TraceConfig{...})`
sets them in code instead. `TraceConfig.SetAttributes` applies the same rules to spans your own
code starts.

In every mode: no request header (`Authorization`, the OpenAI key, the Future AGI keys) is copied,
transport errors are recorded as a class (`transport`, `canceled`, `timeout`) and never as error
text (which carries URLs and host names), audio bytes are never copied, the free-form request
fields `user`, `metadata`, `safety_identifier` and `prompt_cache_key` are not copied, and the
middleware adds no resource attributes.

## What is captured

- Chat Completions (`POST .../chat/completions`), streaming and not: request model, sampling
  parameters, tool definitions, messages, response id and model, finish reasons, tool calls
  (id, name, arguments), and token usage when the API returns it. A stream carries usage only
  when the request sets `stream_options.include_usage`; usage is never written as zero.
- HTTP 4xx/5xx: status ERROR, `http.response.status_code`, `error.type`, and the API error
  message unless content is hidden. A streamed `error` event ends the span with ERROR.
- Cancellation: a context cancelled before the response completes ends the span with ERROR and
  `error.type=canceled`. A stream closed early by the caller ends without an error.
- Other endpoints (Responses, Embeddings, Images, Audio, ...) pass through without a span.

## Limitations

- openai-go runs middleware once per HTTP attempt, so a retried call is one span per attempt
  (retries carry `http.request.resend_count`). Start a span around your call, as the example
  does, so the attempts share one trace, or set `option.WithMaxRetries(0)`.
- The Responses API, Realtime and the Assistants API are not traced.
- Cost is not computed here; Future AGI prices the model from the token counts.

## Test

```sh
go test -race ./...
```

The tests use an `httptest` OpenAI server and the in-memory span recorder; no network and no key.
