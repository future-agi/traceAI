# traceAI for Go

OpenTelemetry instrumentation for Go model clients and agents. Keep your existing SDK, add its middleware, callback or client adapter, and export traces to Future AGI with the standard OTLP exporter.

## Choose a module

Each directory is an independent Go module. All require **Go 1.26.0 or later**.

| Module | Package | Integration point | Traces |
| --- | --- | --- | --- |
| [OpenAI](openai/README.md) | `traceaiopenai` | `openai-go/v3` middleware | Chat Completions, including streaming and tool calls |
| [Anthropic](anthropic/README.md) | `traceaianthropic` | `anthropic-sdk-go` middleware | Messages, including streaming and tool calls |
| [Amazon Bedrock](bedrock/README.md) | `traceaibedrock` | AWS SDK v2 typed client adapter | Converse sync/stream; Anthropic Messages InvokeModel sync/stream |
| [Google Gen AI](genai/README.md) | `traceaigenai` | HTTP transport | Gemini Developer API content generation and streaming |
| [LangChainGo](langchaingo/README.md) | `traceailangchaingo` | LangChainGo callbacks and embedder wrapper | Chains, chat models, tools, retrievers and embeddings |
| [Eino](eino/README.md) | `traceaieino` | Eino callbacks | Agents, graphs, models, tools, retrieval and embedding components |
| [A2A](a2a/README.md) | `traceaia2a` | `a2a-go/v2` client interceptor | Remote-agent calls over JSON-RPC, REST and gRPC |
| [AWS Lambda](lambda/README.md) | `traceailambda` | Maintained `otellambda` adapter | Invocation spans, HTTP-v2 parents and bounded flushing |

The provider modules are not interchangeable: the OpenAI module does not trace the Responses API or embeddings, and the Gen AI module does not claim Vertex AI support. See each module's limitations before choosing it. Instrumentors use the tracer provider you supply (or the OpenTelemetry global provider); none configures an exporter automatically.

## Zero-code tracing with one import

[`go/auto`](auto/README.md) traces AI calls without per-SDK code. Import it once and rebuild; it wraps `http.DefaultTransport`, so calls made by any client built on the default transport (including the default OpenAI and Anthropic Go clients) become wire-layer spans, exported to Future AGI under the same shared privacy catalog (version 3) as traceAI's other SDKs, with [a few differences](auto/README.md#privacy-differences-from-other-traceai-sdks). It configures its own exporter from `FI_API_KEY` and `FI_SECRET_KEY`. Only AI spans are sent to Future AGI: the wire spans, and spans from the modules above when they run on an SDK tracer provider the agent attaches to. It never replaces your OpenTelemetry tracer provider; if you install one, do it before the first AI call, or pass it to `auto.Init(ctx, auto.WithTracerProvider(tp))` before that call. When the agent attaches to your provider, the wire spans, with their captured content, also go to that provider's own exporters; `FI_HIDE_*` applies to both copies, and known-secret replacement only to the copy sent to Future AGI.

```sh
go get github.com/future-agi/traceAI/go/auto@dev
```

```go
import (
    "context"
    "time"

    "github.com/future-agi/traceAI/go/auto"
)

func main() {
    // Go has no exit hook: flush queued spans before main returns.
    defer func() {
        ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
        defer cancel()
        _ = auto.Shutdown(ctx)
    }()
    // Your code. AI calls through http.DefaultTransport are traced.
}
```

**`http.DefaultTransport` changes type.** With FI keys set and wire capture on, `http.DefaultTransport` is the agent's wrapper, not an `*http.Transport`, from package initialization on. An unchecked `http.DefaultTransport.(*http.Transport)` assertion, such as `http.DefaultTransport.(*http.Transport).Clone()`, then panics; a checked assertion takes its fallback. Build custom transports from `&http.Transport{...}` and wrap them with `auto.Transport`, or set `FI_WIRE_CAPTURE=false` if a dependency makes the unchecked assertion.

**Module spans need an SDK tracer provider.** The modules above use the global tracer provider unless you pass them one, and their spans reach Future AGI only through an SDK tracer provider the agent attaches to. With only the import and no provider of your own, module spans go nowhere. Install one as the global provider before the first AI call, or pass it to `auto.Init` with `auto.WithTracerProvider(tp)` (and to each module's own `WithTracerProvider` option):

```go
tp := sdktrace.NewTracerProvider() // sdktrace "go.opentelemetry.io/otel/sdk/trace"
otel.SetTracerProvider(tp)         // before the first AI call
```

Module spans that end before the agent activates are not forwarded, so call `auto.Init` at startup if a step can finish before the first AI call.

Use `@dev` until its first go/auto tag exists. A long-running service can use `import _ "github.com/future-agi/traceAI/go/auto"` and skip the `Shutdown` call. Wire spans cover the HTTP call (model, messages, usage, timing); agent, chain and tool steps still need the modules above, on an SDK tracer provider as shown. Custom transports need `auto.Transport(base)`, and gRPC, WebSocket and realtime audio are not covered. See the [go/auto README](auto/README.md) and its [example](auto/example/main.go).

## Install

**Before the first release:** these modules live on `dev`, not the default branch. Use `@dev` in place of `@latest` until their first release tags exist. Go records the resolved pseudo-version in your application's `go.mod`; commit that file and `go.sum` to pin it. For example:

```sh
go get github.com/future-agi/traceAI/go/openai@dev
```

**After release**, install only the integrations you use:

```sh
go get github.com/future-agi/traceAI/go/openai@latest
go get github.com/future-agi/traceAI/go/anthropic@latest
go get github.com/future-agi/traceAI/go/bedrock@latest
go get github.com/future-agi/traceAI/go/genai@latest
go get github.com/future-agi/traceAI/go/eino@latest
go get github.com/future-agi/traceAI/go/langchaingo@latest
go get github.com/future-agi/traceAI/go/a2a@latest
go get github.com/future-agi/traceAI/go/lambda@latest
```

Run `go get` inside your application's Go module (`go mod init example.com/my-app` if it has none). There is no umbrella `github.com/future-agi/traceAI/go` module and no local `replace` directive is needed.

## Quickstart: one traced OpenAI call

Set `OPENAI_API_KEY`, `OPENAI_MODEL`, `FI_API_KEY`, `FI_SECRET_KEY` and `FI_PROJECT_NAME` in your environment. The model key belongs to your model provider; the two Future AGI keys authenticate only the trace exporter. `OPENAI_BASE_URL` is optional for an OpenAI-compatible endpoint.

After installing the OpenAI module, save this as `main.go`, run `go mod tidy` to resolve the example's exporter dependencies, then run `go run .`:

```go
package main

import (
    "context"
    "fmt"
    "log"
    "os"
    "time"

    traceaiopenai "github.com/future-agi/traceAI/go/openai"
    "github.com/openai/openai-go/v3"
    "github.com/openai/openai-go/v3/option"
    "go.opentelemetry.io/otel/attribute"
    "go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
    "go.opentelemetry.io/otel/sdk/resource"
    sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

func main() {
    if err := run(); err != nil {
        log.Fatal(err)
    }
}

func run() error {
    for _, key := range []string{"OPENAI_API_KEY", "OPENAI_MODEL", "FI_API_KEY", "FI_SECRET_KEY", "FI_PROJECT_NAME"} {
        if os.Getenv(key) == "" {
            return fmt.Errorf("set %s", key)
        }
    }
    ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
    defer cancel()
    exporter, err := otlptracehttp.New(ctx,
        otlptracehttp.WithEndpointURL("https://api.futureagi.com/tracer/v1/traces"),
        otlptracehttp.WithHeaders(map[string]string{
            "X-Api-Key": os.Getenv("FI_API_KEY"),
            "X-Secret-Key": os.Getenv("FI_SECRET_KEY"),
        }),
        otlptracehttp.WithCompression(otlptracehttp.NoCompression),
    )
    if err != nil {
        return err
    }
    tp := sdktrace.NewTracerProvider(
        sdktrace.WithBatcher(exporter),
        sdktrace.WithResource(resource.NewSchemaless(
            attribute.String("project_name", os.Getenv("FI_PROJECT_NAME")),
            attribute.String("project_type", "observe"),
        )),
    )
    defer func() {
        flushCtx, stop := context.WithTimeout(context.Background(), 10*time.Second)
        defer stop()
        if err := tp.Shutdown(flushCtx); err != nil {
            log.Printf("trace export: %v", err)
        }
    }()
    client := openai.NewClient(option.WithMiddleware(
        traceaiopenai.Middleware(traceaiopenai.WithTracerProvider(tp)),
    ))
    reply, err := client.Chat.Completions.New(ctx, openai.ChatCompletionNewParams{
        Model: os.Getenv("OPENAI_MODEL"),
        Messages: []openai.ChatCompletionMessageParamUnion{
            openai.UserMessage("Say hello in one sentence."),
        },
    })
    if err != nil {
        return err
    }
    if len(reply.Choices) == 0 {
        return fmt.Errorf("model returned no choices")
    }
    fmt.Println(reply.Choices[0].Message.Content)
    return nil
}
```

For a complete agent with a local weather tool and a streamed answer, run the [OpenAI example](openai/example/main.go) from a checkout of `dev`:

```sh
cd go/openai
go run ./example -model "$OPENAI_MODEL" -stream
```

The other module READMEs include their own runnable examples. A2A and Eino examples also accept `OPENAI_BASE_URL`; Anthropic uses its Messages API, and Gen AI uses the Gemini API, not OpenAI's wire format.

## Privacy and context

Content is captured by default. Set `FI_HIDE_INPUTS=true` and `FI_HIDE_OUTPUTS=true` before constructing the instrumentor to hide conversation content while retaining model and usage information. Each module documents its finer-grained `TraceConfig` options and limitations. For Anthropic, keep the SDK's independent content capture disabled as described in its README.

Session and user helpers are currently module-specific: use that instrumentor's `ContextWithSessionID` and `ContextWithUserID`. Their private context keys do **not** bridge different modules automatically. When composing multiple instrumentors, set the context through each module's helpers. There is not yet a shared core/context or semantic-conventions module.

## Versions and releases

Each module has its own semantic version. The first planned release is **v0.1.0**, with repository tags that include the module directory:

| Module | First planned tag |
| --- | --- |
| A2A | `go/a2a/v0.1.0` |
| Anthropic | `go/anthropic/v0.1.0` |
| Bedrock | `go/bedrock/v0.1.0` |
| Eino | `go/eino/v0.1.0` |
| LangChainGo | `go/langchaingo/v0.1.0` |
| Gen AI | `go/genai/v0.1.0` |
| Lambda | `go/lambda/v0.1.0` |
| OpenAI | `go/openai/v0.1.0` |

The general tag scheme is `go/<module>/vX.Y.Z`; a repository-wide `vX.Y.Z` tag does not version these nested modules. Tags, not a separate binary upload, make Go modules discoverable to the Go proxy and pkg.go.dev. Release automation is managed separately; this directory does not create tags or publish releases. Pre-1.0 APIs may change between minor versions.

## Develop and test

From an individual module directory:

```sh
go mod tidy -diff
go vet ./...
go test -race ./...
go build -o /tmp/traceai-example ./example
go doc .
```

Tests use local servers and in-memory exporters, not live credentials. The [Go CI workflow](../.github/workflows/go-ci.yml) checks all eight modules independently at the minimum Go version and the current stable Go version, checks module paths and rejects local replacements, and builds every example. Package documentation lives in each module's `doc.go` for `go doc` and pkg.go.dev.
