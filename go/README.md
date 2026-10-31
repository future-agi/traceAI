# traceAI for Go

OpenTelemetry instrumentation for Go model clients and agents. Keep your existing SDK, add its middleware or callback, and export traces to Future AGI with the standard OTLP exporter.

## Choose a module

Each directory is an independent Go module. All require **Go 1.26.0 or later**.

| Module | Package | Integration point | Traces |
| --- | --- | --- | --- |
| [OpenAI](openai/README.md) | `traceaiopenai` | `openai-go/v3` middleware | Chat Completions, including streaming and tool calls |
| [Anthropic](anthropic/README.md) | `traceaianthropic` | `anthropic-sdk-go` middleware | Messages, including streaming and tool calls |
| [Google Gen AI](genai/README.md) | `traceaigenai` | HTTP transport | Gemini Developer API content generation and streaming |
| [Eino](eino/README.md) | `traceaieino` | Eino callbacks | Agents, graphs, models, tools, retrieval and embedding components |
| [A2A](a2a/README.md) | `traceaia2a` | `a2a-go/v2` client interceptor | Remote-agent calls over JSON-RPC, REST and gRPC |

The provider modules are not interchangeable: the OpenAI module does not trace the Responses API or embeddings, and the Gen AI module does not claim Vertex AI support. See each module's limitations before choosing it. Instrumentors use the tracer provider you supply (or the OpenTelemetry global provider); none configures an exporter automatically.

## Install

**Before the first release:** these modules live on `dev`, not the default branch. Use `@dev` in place of `@latest` until their first release tags exist. Go records the resolved pseudo-version in your application's `go.mod`; commit that file and `go.sum` to pin it. For example:

```sh
go get github.com/future-agi/traceAI/go/openai@dev
```

**After release**, install only the integrations you use:

```sh
go get github.com/future-agi/traceAI/go/openai@latest
go get github.com/future-agi/traceAI/go/anthropic@latest
go get github.com/future-agi/traceAI/go/genai@latest
go get github.com/future-agi/traceAI/go/eino@latest
go get github.com/future-agi/traceAI/go/a2a@latest
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
| Eino | `go/eino/v0.1.0` |
| Gen AI | `go/genai/v0.1.0` |
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

Tests use local servers and in-memory exporters, not live credentials. The [Go CI workflow](../.github/workflows/go-ci.yml) checks all five modules independently at the minimum Go version and the current stable Go version, checks module paths and rejects local replacements, and builds every example. Package documentation lives in each module's `doc.go` for `go doc` and pkg.go.dev.
