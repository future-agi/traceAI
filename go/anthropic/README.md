# traceAI for anthropic-sdk-go

Traces the Messages API calls of the official Anthropic Go SDK,
[`github.com/anthropics/anthropic-sdk-go`](https://github.com/anthropics/anthropic-sdk-go),
for [Future AGI](https://futureagi.com). It is a client middleware: install it
once with `option.WithMiddleware` and every `Messages.New` and
`Messages.NewStreaming` call of that client becomes one LLM span with the
request, the response, the model, the token counts and the finish reason.

```go
client := anthropic.NewClient(option.WithMiddleware(traceaianthropic.Middleware()))
```

| | |
| --- | --- |
| Module | `github.com/future-agi/traceAI/go/anthropic`, package `traceaianthropic` |
| Tested with | anthropic-sdk-go v1.82.0 (latest on 2026-10-16), Go 1.24.3 and Go 1.27.2, OpenTelemetry Go v1.33.0 (module minimum) and v1.47.0 |
| Requires | anthropic-sdk-go v1.82.0 or later and Go 1.24 or later. `go get` raises an older SDK to v1.82.0 |
| Depends on | anthropic-sdk-go and the OpenTelemetry API. The example also uses the OpenTelemetry SDK and the OTLP/HTTP exporter |
| Status | Alpha, pre-1.0 |

## Install

Until the first `go/anthropic/v*` release tag, install from the `dev` branch:

```bash
go get github.com/future-agi/traceAI/go/anthropic@dev
```

## Set up

The package uses the OpenTelemetry API only. Your program sets up the tracer
provider and its exporter. To send spans to Future AGI, use the stock OTLP/HTTP
exporter with both Future AGI keys as headers and `project_name` as a resource
attribute, register the provider, and shut it down before the process exits:

```go
exporter, err := otlptracehttp.New(ctx,
	otlptracehttp.WithEndpointURL("https://api.futureagi.com/tracer/v1/traces"),
	otlptracehttp.WithHeaders(map[string]string{
		"X-Api-Key":    os.Getenv("FI_API_KEY"),
		"X-Secret-Key": os.Getenv("FI_SECRET_KEY"),
	}),
	otlptracehttp.WithCompression(otlptracehttp.NoCompression),
)
if err != nil {
	log.Fatal(err)
}
tp := sdktrace.NewTracerProvider(
	sdktrace.WithBatcher(exporter),
	sdktrace.WithResource(resource.NewSchemaless(
		attribute.String("project_name", "my-go-service"),
		attribute.String("project_type", "observe"),
		attribute.String("service.name", "my-go-service"),
	)),
)
defer tp.Shutdown(context.Background()) // flushes the last batch
otel.SetTracerProvider(tp)

client := anthropic.NewClient(option.WithMiddleware(traceaianthropic.Middleware()))
ctx = traceaianthropic.ContextWithSessionID(ctx, "session-123") // optional
msg, err := client.Messages.New(ctx, params)
```

[`example/main.go`](example/main.go) is a complete program: a weather agent
with one tool, an agent span and a tool span of its own, an optional streamed
answer and an optional failing call. Run it with:

```bash
export ANTHROPIC_API_KEY=... FI_API_KEY=... FI_SECRET_KEY=... FI_PROJECT_NAME=anthropic-go
go run ./example -stream -fail -session demo-1
```

## The span

anthropic-sdk-go v1.81.0 and later open a span for each API call themselves,
`anthropic.messages.create`, through the registered tracer provider. When that
span is recording, the middleware adds the Future AGI attributes to it, so one
call is still one span. When the SDK's spans are off (`option.WithoutOpenTelemetry()`
or `ANTHROPIC_OPEN_TELEMETRY=false`), the middleware opens a CLIENT span of its
own with the same name and the same `gen_ai.*` keys, one per HTTP attempt.

| Attribute | Value |
| --- | --- |
| `gen_ai.span.kind` | `LLM` |
| `gen_ai.operation.name`, `gen_ai.provider.name` | `chat`, `anthropic` |
| `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.response.id` | from the request and the response |
| `gen_ai.usage.input_tokens` | every input token: the API's `input_tokens` plus its cache read and cache creation tokens, as the [GenAI conventions for Anthropic](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/anthropic.md) require and as the SDK's own span does |
| `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_write.input_tokens` | the two cache counts, also on their own keys. Uncached input is `input_tokens` minus both |
| `gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons` | from the response, or the stream's final `message_delta` |
| `input.value`, `output.value` | the request body and the response message (for a stream, the message the events assemble), JSON |
| `gen_ai.input.messages`, `gen_ai.system_instructions`, `gen_ai.output.messages` | the messages in the GenAI JSON shape, including `tool_call` and `tool_call_response` parts |
| `gen_ai.tool.definitions`, `gen_ai.request.parameters` | the request's tools, and its settings (`max_tokens`, `temperature`, `top_p`, `top_k`, `stream`, `tool_choice`, `thinking`, `service_tier`) |
| `session.id`, `user.id` | only from `ContextWithSessionID` and `ContextWithUserID`. A bare context gets neither |
| `error.type`, span status `ERROR` | on an API error (the API's error type, such as `rate_limit_error`), a stream `error` event, a stream that ends early, or a canceled context. Never the error message. With content on, `output.value` holds the API's error body; `FI_HIDE_OUTPUTS` hides it |

A stream is one span. It ends when the stream ends, fails, is closed or its
context is canceled; no span is opened per event. Tool calls the model asks for
are recorded on the LLM span; the middleware does not span your tool code (the
example shows how to add a TOOL span yourself). An application-level retry is a
second call and a second span.

Other endpoints (`count_tokens`, batches, files, models) are not given LLM
attributes. Amazon Bedrock and Google Vertex clients use the same middleware
but are not tested.

## Content

Content is captured by default, as in every traceAI instrumentor. The same
variables as traceAI's Python `TraceConfig` turn it off:

| Variable | Effect |
| --- | --- |
| `FI_HIDE_INPUTS` | `input.value` becomes `__REDACTED__`; no input messages, system instructions, tool definitions or stop sequences |
| `FI_HIDE_OUTPUTS` | `output.value` becomes `__REDACTED__`; no output messages |
| `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_OUTPUT_MESSAGES` | no `gen_ai.input.messages` and `gen_ai.system_instructions`, or no `gen_ai.output.messages` |
| `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT` | the text of every message part becomes `__REDACTED__` |
| `FI_HIDE_INPUT_IMAGES` | image data and image parts are left out |
| `FI_BASE64_IMAGE_MAX_LENGTH` | longest base64 image kept (default 32000) |
| `FI_HIDE_LLM_INVOCATION_PARAMETERS` | no `gen_ai.request.parameters` |

Values are `true` or `false`, read once when `Middleware` is called; pass
`WithTraceConfig(TraceConfig{...})` to set them in code. The model, token
counts, finish reason and error type stay in every mode.

These settings cover what this package adds. anthropic-sdk-go's own span carries
no content unless you set `ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE=content`
(or `option.WithOpenTelemetryTracesContentMode`); leave that unset, or the SDK
exports content that `FI_HIDE_*` does not remove. The middleware logs a warning
when the variable is set together with a hide setting.

Never exported, in any mode: the Anthropic API key and other request headers,
the Future AGI keys, the `authorization_token` of an MCP server in a request
body, error message text in the span status or `error.type`, and stack traces.

## Tests

```bash
go test -race ./...
```

The tests run the real SDK against `httptest` servers with recorded Messages
JSON and server-sent-event bodies (`testdata/`), in both modes (the SDK's span
enriched, and the middleware's own span), and cover streaming, tool use, API
errors, stream errors, cancellation, retries, the `FI_HIDE_*` settings and
image limits.
