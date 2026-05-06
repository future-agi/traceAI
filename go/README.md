# traceAI Go SDK

OpenTelemetry instrumentation for AI/LLM frameworks in Go, following the [OTel GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/).

## Packages

| Package | Description |
|---------|-------------|
| `traceai` | Core setup — TracerProvider, config, semantic conventions |
| `traceai_openai` | OpenAI Go client instrumentation |

## Quick Start

```go
package main

import (
    "context"
    "fmt"
    "log"

    "github.com/future-agi/traceAI/go/traceai"
    "github.com/future-agi/traceAI/go/traceai_openai"
    "github.com/openai/openai-go"
    "github.com/openai/openai-go/option"
)

func main() {
    provider, err := traceai.Register(
        traceai.WithProjectName("my-go-service"),
    )
    if err != nil {
        log.Fatal(err)
    }
    defer provider.Shutdown(context.Background())

    client := openai.NewClient(
        option.WithMiddleware(traceai_openai.Middleware()),
    )

    resp, err := client.Chat.Completions.New(context.Background(), openai.ChatCompletionNewParams{
        Model: "gpt-4",
        Messages: []openai.ChatCompletionMessageParamUnion{
            openai.UserMessage("What is OpenTelemetry?"),
        },
    })
    if err != nil {
        log.Fatal(err)
    }
    fmt.Println(resp.Choices[0].Message.Content)
}
```

## Span Attributes

Spans follow the [OTel GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/) — `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.*` tokens, `gen_ai.response.*`, etc. Prompt and completion content is captured by default; disable with `WithContentCapture(false)` for PII-sensitive workloads.

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `FI_API_KEY` | Future AGI API key | — |
| `FI_SECRET_KEY` | Future AGI secret key | — |
| `FI_BASE_URL` | OTLP HTTP collector endpoint | `https://api.futureagi.com` |
| `FI_GRPC_URL` | OTLP gRPC collector endpoint | `https://grpc.futureagi.com` |
| `FI_PROJECT_NAME` | Project name for trace grouping | `DEFAULT_PROJECT_NAME` |

### Options

```go
traceai.Register(traceai.WithTransport(traceai.TransportGRPC))
traceai.Register(traceai.WithShutdownTimeout(30 * time.Second))

// disable content capture for PII-sensitive workloads
traceai_openai.Middleware(traceai_openai.WithContentCapture(false))
traceai_openai.Middleware(traceai_openai.WithTracerProvider(myTP))
```

## Custom Backend

Standard OTLP, point it at any collector:

```go
provider, _ := traceai.Register(
    traceai.WithBaseURL("https://your-otel-collector:4318"),
    traceai.WithSetGlobal(true),
)
```

## Development

```bash
cd go/traceai_openai
go test ./...
```

## Adding a Framework

See `traceai_openai/` for the pattern — create `traceai_<name>/`, implement an HTTP middleware or client wrapper, wire up the semconv attributes.

## License

Apache 2.0 — same as the traceAI project.
