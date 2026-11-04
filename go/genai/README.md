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
`input.value`, `output.value`, `session.id`, `user.id`). `Models.EmbedContent` uses the same
client and creates an `embeddings <model>` span with `gen_ai.span.kind=EMBEDDING`, text inputs,
embedding count and vector dimensions. Vector values are never captured.

| | |
|---|---|
| Module | `github.com/future-agi/traceAI/go/genai` (package `traceaigenai`) |
| Gemini SDK | `google.golang.org/genai` v1.71.0 or later (the `go.mod` minimum) |
| Backend | Gemini Developer API (`genai.BackendGeminiAPI`). Vertex AI is not tested and not claimed. |
| Go | 1.26.0 or later, the common minimum across traceAI Go modules |
| Dependencies | the OpenTelemetry API (`go.opentelemetry.io/otel`, `.../trace`), Google Gen AI SDK response types (`google.golang.org/genai`) and the standard library. No exporter is built in. The OTel SDK is used by the tests and the example only. |

The retired module `github.com/google/generative-ai-go` (support ended 2025-11-30) is not
supported and not wrapped.

## Install

Install both traceAI and the Google Gen AI SDK in your Go module:

**Release availability:** the traceAI command below is for tagged releases. Until the first
`go/genai/v0.1.0` tag exists, use `@dev` instead of `@latest` for the traceAI module. See the
[Go SDK index](../README.md) for all modules and the release scheme.

```sh
go get github.com/future-agi/traceAI/go/genai@latest
go get google.golang.org/genai@latest
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

## Trace embeddings

Use the same traced client for one or more input documents:

```go
embeddings, err := client.Models.EmbedContent(ctx, "gemini-embedding-001", []*genai.Content{
	genai.NewContentFromText("How do I reset my password?", genai.RoleUser),
	genai.NewContentFromText("Open Settings and choose Reset password.", genai.RoleUser),
}, &genai.EmbedContentConfig{TaskType: "RETRIEVAL_DOCUMENT"})
if err != nil {
	return err
}
fmt.Printf("Embedded %d documents\n", len(embeddings.Embeddings))
```

The SDK sends this call to `batchEmbedContents`, including when there is only one input.
Direct `embedContent` requests through the wrapped transport are also traced. This is
synchronous embedding, not the asynchronous Batch API. In Future AGI, open your project
and look for `embeddings gemini-embedding-001`.

## Privacy

Content is captured by default, as in every traceAI instrumentor. The traceAI `TraceConfig`
switches, with the same names and defaults as Python, turn it off:

| Variable | Effect |
|---|---|
| `FI_HIDE_INPUTS` | `input.value` becomes `__REDACTED__`; input messages (system instruction, prompts, function calls and function responses in the history) are dropped |
| `FI_HIDE_OUTPUTS` | `output.value` becomes `__REDACTED__`; output messages (answer text, function-call arguments) are dropped |
| `FI_HIDE_INPUT_MESSAGES` / `FI_HIDE_OUTPUT_MESSAGES` | drop `gen_ai.input.messages.*` / `gen_ai.output.messages.*` only |
| `FI_HIDE_INPUT_TEXT` / `FI_HIDE_OUTPUT_TEXT` | redact text in messages and aggregate `input.value` / `output.value`; keep roles and tool data |
| `FI_HIDE_INPUT_IMAGES` | drop images from input messages and aggregate `input.value` |
| `FI_HIDE_LLM_INVOCATION_PARAMETERS` | drop `gen_ai.request.parameters` |
| `FI_HIDE_ERROR_DETAILS` | drop API error messages from attributes and status descriptions; keep ERROR status and error classification |
| `FI_BASE64_IMAGE_MAX_LENGTH` | redact `data:` images longer than this (default 32000) |

A hide switch is enabled by `true` (any case). `WithTraceConfig(traceaigenai.TraceConfig{...})`
sets them in code instead. `TraceConfig.SetAttributes` applies the same rules to spans your own
code starts.

When `FI_HIDE_ERROR_DETAILS` is unset or invalid, error details are hidden if any input/output,
message or text hide switch is enabled. Explicit `true` or `false` overrides that default.
Setting it to `false` can expose content quoted in API errors even when content capture is hidden.
In code, leave `TraceConfig.HideErrorDetails` nil to derive it from the content switches, or use
`genai.Ptr(true)` / `genai.Ptr(false)` for an explicit override.

Rewrapping a traced client with `WrapClient` preserves its settings and still creates one span
per HTTP attempt. Supplied options replace the corresponding settings on the new client;
`WithTraceConfig` replaces the whole privacy configuration. The original client is unchanged.

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
  API returns it: input = `promptTokenCount` + `toolUsePromptTokenCount`, output =
  `candidatesTokenCount` + `thoughtsTokenCount`, `total_tokens`, `output_tokens.reasoning`,
  `cache_read_tokens`. Cache reads are already included in prompt tokens and are not added
  again. The tool-use subset is also recorded as `gen_ai.usage.input_tokens.tool_use`;
  `gen_ai.usage.details` keeps allow-listed per-modality counts as JSON for pricing.
  Absent counts are not invented; explicitly reported input/output/total zeros are retained.
  A stream uses the last reported usage object, not a sum of chunks. The provider total is
  preserved rather than recomputed.
  Flattened input/output previews each use at most 32 attributes, and tool definitions are
  limited to the first 16 declarations. This reserves room for context, usage, aggregate
  content and status under the default OpenTelemetry 128-attribute limit, even with many
  turns, parts, calls or candidates. Aggregate `input.value` / `output.value` retain the full
  conversation subject to privacy settings.
- `POST .../models/{model}:embedContent` and `:batchEmbedContents`: one `EMBEDDING` span
  with operation `embeddings`, request model, `embedding.model_name`, text inputs using the
  same input attributes as generation, and allow-listed task type/output dimensionality in
  `gen_ai.request.parameters`. The flattened `gen_ai.input.messages.*` preview contains at most
  the first 32 documents, leaving room for context and response/error metadata under the default
  OpenTelemetry attribute limit. `input.value` retains the full batch, subject to privacy settings.
  `embedding.count` records returned vectors;
  `gen_ai.embeddings.dimension.count` records their dimension when uniform. Vector values,
  titles, media and opaque fields are never captured. `FI_HIDE_INPUTS` redacts the input
  summary and drops messages; `FI_HIDE_INPUT_TEXT` redacts text in both. Gemini Developer
  API embedding responses do not include token usage, so no token counts are invented.
- A stream is one span: it ends when the body is read to the end, on a read error, or when the SDK
  closes the body (your `range` loop finished or broke).
- HTTP 4xx/5xx: status ERROR, `http.response.status_code`, `error.type` (the Google status such as
  `NOT_FOUND`), and the API error message when error details are allowed. An error block inside a stream
  ends the span with ERROR.
- Invalid generation JSON/chunks or embedding JSON/vector values: status ERROR and `error.type=decode`, without
  recording the response body or decoder error text. The SDK still receives the original response.
- Cancellation: a context cancelled before the response completes ends the span with ERROR and
  `error.type=canceled`.
- Other requests (countTokens, files, caches, asynchronous batches, models, interactions, live)
  pass through without a span.

## Limitations

- The SDK retries at the HTTP layer only when you set `HTTPOptions.RetryOptions`; each attempt is
  then its own span. Automatic continuation (v1.74.0+, when the server returns a continuation
  token for a long answer) also sends several requests for one call; each hop is its own span.
  Start a span around your call, as the example does, so they share one trace.
- Vertex AI (`genai.BackendVertexAI`) is not tested. A Vertex call made through a wrapped client
  may still produce a span; that is not a support claim.
- Live API (websocket) and video generation are not traced.
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
