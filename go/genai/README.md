# traceAI Gemini Go

OpenTelemetry tracing for the Google Gen AI Go SDK,
[`google.golang.org/genai`](https://github.com/googleapis/go-genai), sending spans to
[Future AGI](https://futureagi.com).

The SDK has no middleware option. Its hook is `ClientConfig.HTTPClient`, so this module is an
`http.RoundTripper`: pass `traceaigenai.NewHTTPClient(...)` as the client's HTTP client and every
`Models.GenerateContent` and `Models.GenerateContentStream` call (and the `Chats` helpers built on
them) becomes one client span named `generate_content <model>` with the same attribute names
`traceai-google-genai` writes in Python (`gen_ai.span.kind=LLM`, `gen_ai.provider.name=google`,
`gen_ai.request.model`, `gen_ai.usage.*`, `gen_ai.input.messages.*`, `gen_ai.output.messages.*`,
`input.value`, `output.value`, `session.id`, `user.id`).

| | |
|---|---|
| Module | `github.com/future-agi/traceAI/go/genai` (package `traceaigenai`) |
| Gemini SDK | `google.golang.org/genai` v1.71.0 or later (the `go.mod` minimum) |
| Backend | Gemini Developer API (`genai.BackendGeminiAPI`). Vertex AI is not tested and not claimed. |
| Go | 1.26.0 or later, the common minimum across traceAI Go modules |
| Dependencies | the OpenTelemetry API (`go.opentelemetry.io/otel`, `.../trace`) and the standard library. No exporter is built in. `google.golang.org/genai` and the OTel SDK are used by the tests and the example only. |

The retired module `github.com/google/generative-ai-go` (support ended 2025-11-30) is not
supported and not wrapped.

## Install

**Release availability:** the command below is for tagged releases. Until the first
`go/genai/v0.1.0` tag exists, use `@dev` instead of `@latest`. See the
[Go SDK index](../README.md) for all modules and the release scheme.

```sh
go get github.com/future-agi/traceAI/go/genai@latest
```

## Use

```go
// tp is an *sdktrace.TracerProvider that exports to Future AGI (see below).
client, err := genai.NewClient(ctx, &genai.ClientConfig{
	Backend:    genai.BackendGeminiAPI,
	HTTPClient: traceaigenai.NewHTTPClient(traceaigenai.WithTracerProvider(tp)),
})
ctx = traceaigenai.ContextWithSessionID(ctx, "session-123") // optional
resp, err := client.Models.GenerateContent(ctx, "gemini-3.8-flash", genai.Text("Hello"), nil)
```

`NewHTTPClient` behaves like the client the SDK builds when `HTTPClient` is nil (default transport,
no client timeout). To keep your own `*http.Client` (a proxy, a timeout), use
`traceaigenai.WrapClient(hc, ...)`; `NewTransport(base, ...)` wraps any `http.RoundTripper`.
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

[`example/main.go`](example/main.go) is a complete program: an agent span, a `get_weather`
function call, the tool span, a streamed answer and a failing call.

```sh
export FI_API_KEY=... FI_SECRET_KEY=... FI_PROJECT_NAME=my-service GEMINI_API_KEY=...
go run ./example -stream -fail
```

## Privacy

Content is captured by default, as in every traceAI instrumentor. The traceAI `TraceConfig`
switches, with the same names and defaults as Python, turn it off:

| Variable | Effect |
|---|---|
| `FI_HIDE_INPUTS` | `input.value` becomes `__REDACTED__`; input messages (system instruction, prompts, function calls and function responses in the history) and the API error message are dropped |
| `FI_HIDE_OUTPUTS` | `output.value` becomes `__REDACTED__`; output messages (answer text, function-call arguments) and the API error message are dropped |
| `FI_HIDE_INPUT_MESSAGES` / `FI_HIDE_OUTPUT_MESSAGES` | drop `gen_ai.input.messages.*` / `gen_ai.output.messages.*` only |
| `FI_HIDE_INPUT_TEXT` / `FI_HIDE_OUTPUT_TEXT` | redact the message text, keep roles and function calls |
| `FI_HIDE_INPUT_IMAGES` | drop image parts |
| `FI_HIDE_LLM_INVOCATION_PARAMETERS` | drop `gen_ai.request.parameters` |
| `FI_BASE64_IMAGE_MAX_LENGTH` | redact `data:` images longer than this (default 32000) |

A value counts only when it is `true` (any case). `WithTraceConfig(traceaigenai.TraceConfig{...})`
sets them in code instead. `TraceConfig.SetAttributes` applies the same rules to spans your own
code starts.

In every mode: the Gemini API key (`x-goog-api-key`), every other request header and the request
URL are never copied, and the key is scrubbed from any API error text; transport errors are
recorded as a class (`transport`, `canceled`, `timeout`) and never as error text (which carries
URLs and host names); `thoughtSignature`, thought summaries, audio, video and file bytes and
generated (inline) images are never copied; the transport adds no resource attributes.

## What is captured

- `POST .../models/{model}:generateContent` and `:streamGenerateContent`: request model (from the
  URL), sampling parameters (`temperature`, `top_p`, `top_k`, `max_tokens`, `seed`, penalties,
  stop sequences, and an allow-listed `gen_ai.request.parameters`), function declarations
  (`gen_ai.tool.definitions.*`), the system instruction and turns, response id and model
  (`modelVersion`), finish reasons, function calls (id, name, arguments), and token usage when the
  API returns it: input = `promptTokenCount`, output = `candidatesTokenCount` +
  `thoughtsTokenCount` (as in Python), `total_tokens`, `output_tokens.reasoning`,
  `cache_read_tokens`. Usage is never written as zero; a stream takes the last chunk's running
  total.
- A stream is one span: it ends when the body is read to the end, on a read error, or when the SDK
  closes the body (your `range` loop finished or broke).
- HTTP 4xx/5xx: status ERROR, `http.response.status_code`, `error.type` (the Google status such as
  `NOT_FOUND`), and the API error message unless content is hidden. An error block inside a stream
  ends the span with ERROR.
- Cancellation: a context cancelled before the response completes ends the span with ERROR and
  `error.type=canceled`.
- Other requests (countTokens, embeddings, files, caches, batches, models, interactions, live)
  pass through without a span.

## Limitations

- The SDK retries at the HTTP layer only when you set `HTTPOptions.RetryOptions`; each attempt is
  then its own span. Automatic continuation (v1.74.0+, when the server returns a continuation
  token for a long answer) also sends several requests for one call; each hop is its own span.
  Start a span around your call, as the example does, so they share one trace.
- Vertex AI (`genai.BackendVertexAI`) is not tested. A Vertex call made through a wrapped client
  may still produce a span; that is not a support claim.
- Live API (websocket), video generation and embeddings are not traced.
- Cost is not computed here; Future AGI prices the model from the token counts.

## Test

```sh
go mod tidy -diff
go vet ./...
go test -race ./...
go build -o /tmp/traceai-genai-example ./example
```

The tests drive the real SDK against an `httptest` Gemini server that replays recorded Gemini
Developer API bodies (`testdata/`), with the in-memory span recorder; no network and no key.
