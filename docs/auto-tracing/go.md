# Zero-code tracing for Go

The Go agent traces AI calls at the HTTP layer. You add one import and rebuild.
Each AI request that goes through `http.DefaultTransport`, or an `http.Client`
built on it, becomes one OpenTelemetry CLIENT span with the model, messages,
token usage and timing. Spans go to Future AGI. Only AI spans are sent to
Future AGI; your HTTP server, database and other application spans stay with
your own exporters. Go cannot patch compiled libraries, so this is a transport
agent, not runtime instrumentation. It is an alpha release.

## Requirements

- Go 1.26 (the minimum for its OpenTelemetry 1.47 dependency). With a recent
  Go installation, `GOTOOLCHAIN=auto` downloads the required toolchain.
- A Future AGI API key and secret key.
- The module adds only OpenTelemetry, protobuf and `golang.org/x/net` to your
  build. It does not add or upgrade any AI SDK.

## Install

The module is released as the tag `go/auto/v0.2.0`.

```sh
go get github.com/future-agi/traceAI/go/auto@v0.2.0
```

Use `@latest` instead of `@v0.2.0` to take the newest release.

## Turn it on

Import the package once, anywhere in your program, and rebuild.

For a long-running service, a blank import is enough:

```go
import _ "github.com/future-agi/traceAI/go/auto"
```

Go has no exit hook. A program that returns from `main` right after its last
AI call can exit with that span still queued. For short-lived programs, use
the named import and flush before `main` returns:

```go
package main

import (
    "context"
    "time"

    "github.com/future-agi/traceAI/go/auto"
)

func main() {
    defer func() {
        ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
        defer cancel()
        _ = auto.Shutdown(ctx) // sends queued spans
    }()
    // Your code. AI calls through http.DefaultTransport are traced.
}
```

`auto.ForceFlush(ctx)` flushes without shutting down.

The agent starts on the first AI call, or on an explicit `auto.Init`,
whichever comes first. To pass options, call `auto.Init` before the first AI
call:

```go
shutdown, err := auto.Init(ctx)
```

Options include `WithCredentials`, `WithProject`, `WithSampleRate`,
`WithWireCapture`, `WithTraceConfig`, `WithTracerProvider`,
`WithSpanProcessor` and `WithConfig`. They apply only to the call that starts
the agent. `Init` runs once per process.

If you build a custom transport, wrap it so its calls are traced too:

```go
client := &http.Client{Transport: auto.Transport(customTransport)}
```

## Send traces to Future AGI

Set both keys in the environment, then build and run:

```sh
export FI_API_KEY=... FI_SECRET_KEY=...
export FI_PROJECT_NAME=my-service
go run .
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `FI_API_KEY`, `FI_SECRET_KEY` | none | Future AGI credentials; both are required |
| `FI_BASE_URL` | `https://api.futureagi.com` | Base URL; traces go to `/tracer/v1/traces` |
| `FI_PROJECT_NAME` | `OTEL_SERVICE_NAME`, else the working-directory name, else `DEFAULT_PROJECT_NAME` | Future AGI project |
| `FI_PROJECT_TYPE` | `observe` | `observe` or `experiment` |
| `FI_SAMPLE_RATE` | unset (`1`) | Root sampling ratio for a provider the agent creates |
| `FI_CONFIG_FILE` | `./futureagi.toml` | Optional config file |
| `OTEL_SDK_DISABLED` | `false` | `true` turns the agent off |

Settings resolve in this order: code options, then environment, then the
config file, then defaults. The two keys are read as a pair from one source.
If a source has only one of them, that source is skipped with a warning that
names the missing key.

The config file uses flat `key = value` lines. Each key maps to
`FI_<UPPER_CASE_KEY>`, except `debug`, which maps to `FI_AUTO_DEBUG`. Keep
keys in environment variables, not in committed files.

```toml
project_name = "checkout-agent"
project_type = "observe"
wire_capture = true
sample_rate = 0.25
```

**Your OpenTelemetry setup stays yours.** The agent does not replace the
global tracer provider. If you install an SDK tracer provider before the first
AI call, or pass one with `auto.WithTracerProvider(tp)`, the agent adds its
processor to it, so its spans join your traces. Otherwise it keeps a private
provider for its own spans.

## Hide prompt and response content

Content is captured by default. Set these switches to hide it:

| Variable | Hides |
| --- | --- |
| `FI_HIDE_INPUTS` | Inputs |
| `FI_HIDE_OUTPUTS` | Outputs |
| `FI_HIDE_INPUT_MESSAGES` | Input messages |
| `FI_HIDE_OUTPUT_MESSAGES` | Output messages |
| `FI_HIDE_INPUT_TEXT` | Input text |
| `FI_HIDE_OUTPUT_TEXT` | Output text |
| `FI_HIDE_INPUT_IMAGES` | Input images |
| `FI_HIDE_LLM_INVOCATION_PARAMETERS` | LLM invocation parameters |

The related setting `FI_BASE64_IMAGE_MAX_LENGTH` defaults to `32000`.

A hide switch reads `true`, `1`, `yes` or `on` as hide, and `false`, `0`, `no`
or `off` as show. Case and surrounding spaces are ignored, and a blank value
counts as not set. Any other value hides that content and prints one warning
naming the variable. Hidden inputs and outputs show as `__REDACTED__`.
`FI_WIRE_CAPTURE`, `FI_AUTO_DEBUG` and `OTEL_SDK_DISABLED` are not hide
switches: they are on only for `true` (any case).

From code, pass the same settings with `auto.WithTraceConfig`. In
`futureagi.toml`, use the key without the `FI_` prefix in lower case, for
example `hide_inputs = true`.

## What gets traced

The wire layer recognizes these APIs by their request format:

- OpenAI-compatible chat, completions, Responses, embeddings, images,
  moderation and rerank
- Anthropic Messages
- Gemini generation and embedding
- Cohere chat and embedding
- Bedrock Converse and invoke
- Ollama chat, generate and embed
- HTTP speech-to-text and text-to-speech for OpenAI, Groq, Deepgram,
  ElevenLabs, AssemblyAI and Cartesia

JSON, SSE and NDJSON streams are parsed. Streaming spans include
`gen_ai.response.time_to_first_chunk` and, when the model returns output,
`gen_ai.response.time_to_first_text`.

The default clients of openai-go/v3 and anthropic-sdk-go are traced through
the wire layer with no middleware.

Agent, chain, tool and retriever steps need the dedicated traceAI Go modules.
Install each from `github.com/future-agi/traceAI/go/<module>`. Each is a
one-line integration, listed as `manual` in the report:

| Module | One-line integration |
| --- | --- |
| `openai` | `option.WithMiddleware(traceaiopenai.Middleware())` |
| `anthropic` | `option.WithMiddleware(traceaianthropic.Middleware())` |
| `genai` | `genai.ClientConfig{HTTPClient: traceaigenai.NewHTTPClient()}` |
| `eino` | `callbacks.AppendGlobalHandlers(traceaieino.NewHandler())` |
| `a2a` | `a2aclient.WithCallInterceptors(traceaia2a.NewInterceptor())` |
| `bedrock` | `traceaibedrock.NewClient(bedrockruntimeClient)` |
| `langchaingo` | `openai.WithCallback(traceailangchaingo.NewHandler())` |
| `lambda` | `lambda.StartHandler(traceailambda.WrapHandler(handler, provider))` |

Module spans reach Future AGI only through an SDK tracer provider the agent
attaches to. Install one with `otel.SetTracerProvider(tp)` before the first AI
call, or pass it to `auto.Init` with `auto.WithTracerProvider(tp)` and to the
module's own `WithTracerProvider` option.

## Turn integrations off or choose them

| Variable | Default | Meaning |
| --- | --- | --- |
| `FI_INSTRUMENTATIONS` | all | Comma-separated allowlist of `wire`, `openai`, `anthropic`, `genai`, `eino`, `a2a`, `bedrock`, `langchaingo`, `lambda` |
| `FI_DISABLED_INSTRUMENTATIONS` | none | Comma-separated denylist; `wire` turns off all transport capture |
| `FI_WIRE_CAPTURE` | `true` | Wire-layer switch |
| `FI_WIRE_MAX_BODY_BYTES` | `4194304` | Per-direction capture cap; larger spans keep `traceai.wire.truncated=true` |

The same settings exist in `futureagi.toml` as `instrumentations`,
`disabled_instrumentations`, `wire_capture` and `wire_max_body_bytes`.
Provider names in the allowlist do not filter wire requests; `wire` controls
that layer. The lists do not remove middleware you added yourself.

## Check that it works

Run the doctor from your module:

```sh
go run github.com/future-agi/traceAI/go/auto/cmd/traceai-doctor
```

It prints the resolved configuration as JSON with keys masked, each
integration's status and the wire coverage. It exits with status 1 if an
integration has failed. The doctor works offline: it does not send a test
export, so it does not confirm that Future AGI accepted your traces.

Set `FI_AUTO_DEBUG=true` to print the same masked report at startup. In code,
`auto.Report()` returns it. For a complete program, see
[`go/auto/example/main.go`](../../go/auto/example/main.go).

## Troubleshooting

- **Panic on `http.DefaultTransport.(*http.Transport)`.** With keys set and
  wire capture on, `http.DefaultTransport` becomes the agent's wrapper. An
  unchecked type assertion then panics. Build custom transports from
  `&http.Transport{...}` and wrap them with `auto.Transport`, or set
  `FI_WIRE_CAPTURE=false`.
- **The last span is missing.** The program exited before the 200 ms batch
  export. Call `auto.Shutdown(ctx)` before `main` returns, and call it in
  your graceful-shutdown path for services.
- **No span for a call.** Read each response to the end or close its body;
  the span completes at that point.
- **Module spans do not appear.** Install an SDK tracer provider before the
  first AI call, or pass it with `auto.WithTracerProvider(tp)`. Call
  `auto.Init` at startup if a step can finish before the first AI call.
- **Options or a new provider are ignored.** The agent starts once. Options
  passed after the first AI call, and a global provider installed after it,
  are not used.
- **Nothing is exported.** Check that both `FI_API_KEY` and `FI_SECRET_KEY`
  come from the same source, and run the doctor.

## Limits

- Not traced: incoming HTTP server spans, framework patching, gRPC,
  WebSocket, HTTP/3 and realtime audio.
- Bedrock binary event streams and `br` or `zstd` encoded responses record
  the request and status, not parsed output.
- Custom proxy paths must match a known API format to be traced.
- Abrupt exit or SIGTERM without `auto.Shutdown` can lose queued spans. The
  agent installs no signal handlers.

## More detail

See the [Go auto package README](../../go/auto/README.md) for the full
configuration, OpenTelemetry coexistence rules and privacy reference.
