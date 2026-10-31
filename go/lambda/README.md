# AWS Lambda for Go

Trace each Lambda invocation with OpenTelemetry's maintained [`otellambda`](https://pkg.go.dev/go.opentelemetry.io/contrib/instrumentation/github.com/aws/aws-lambda-go/otellambda) instrumentor and send ended spans before the runtime freezes. This adapter adds metadata-only errors, HTTP-v2 trace context, cold-start markers and bounded flushing to the native instrumentor's single server span. It does not create a second invocation span or configure an exporter.

## Use your existing provider

Requires Go 1.26.0+, `aws-lambda-go` v1.55.1+, `otellambda` v0.72.0 and OpenTelemetry v1.47.0. The dependencies are resolved by the module.

```go
import (
    "github.com/aws/aws-lambda-go/lambda"
    traceailambda "github.com/future-agi/traceAI/go/lambda"
)

// provider is your application-owned OTel SDK tracer provider.
// Build it and wrap the handler once, outside the invocation.
lambda.StartHandler(traceailambda.WrapHandler(lambda.NewHandler(handle), provider))
```

For a complete application with standard OTLP/HTTP exporter setup, see [example/main.go](example/main.go). Set `FI_API_KEY`, `FI_SECRET_KEY` and `FI_PROJECT_NAME` on the function; the example sends the two keys only to the trace endpoint at `{FI_BASE_URL}/tracer/v1/traces` (base defaults to `https://api.futureagi.com`). Missing either key disables exporting without failing the handler. The resource carries `project_name`, `project_type=observe` and `service.name`.

Keep the provider alive across warm invocations. Pass the invocation context to model clients and tools so their spans are children of `lambda.handler`. Use the relevant traceAI model-client integration separately: Lambda invocation instrumentation does not trace model internals. Do not add another `otellambda.WrapHandler`, a second invocation span, or a second deferred flush around this adapter.

## Lifecycle and privacy

- The native `otellambda` server span is named `lambda.handler`, classified with `fi.span.kind=CHAIN`, and carries `faas.coldstart`, `faas.trigger` and the runtime's invocation ID. Native AWS function metadata is retained.
- The first call to the wrapper is cold; later calls are warm. Constructing the wrapper inside the handler would reset this state.
- API Gateway HTTP API payload v2 and Lambda function URL events can supply a W3C `traceparent`. Invalid parents are ignored. Other event formats and X-Ray headers are not extracted. Event headers, baggage and tracestate are not captured or propagated by this adapter.
- No event bodies, results, error messages, exception events or stack traces are recorded by the adapter. Error and panic spans contain only the Go type in `error.type` and their ERROR status. Application errors and panics still reach the runtime unchanged. Other instrumentors retain their own privacy settings.
- The root span ends before each flush attempt, including the error and panic paths. Flushing ignores invocation cancellation but honors the earlier of two seconds and the invocation deadline minus 250 ms. If that reserve is exhausted, flushing is skipped. Standard SDK providers honor this deadline; custom providers must do so too.
- Export failures do not replace the application result. Flush diagnostics are generic and do not include exporter error text. A hard runtime timeout or process kill can still lose spans; flushing is not a delivery guarantee.
- A nil provider returns the original handler without instrumentation. The adapter never shuts down your provider.

## Migrate from the copied docs helper

Keep your application handler and standard exporter configuration. Replace the helper's invocation-span wrapper and deferred `Flush` with the single `WrapHandler` call above. Do not run both paths together. The new adapter always captures invocation metadata only: the old example's `LAMBDA_TRACE_CAPTURE_CONTENT` switch does not enable event, result or error-text capture here. Use explicit, reviewed application spans if you need selected content.

## Build

From this module directory:

```sh
go test -race ./...
go build -o bootstrap ./example
```

For deployment, cross-compile `bootstrap` for your Lambda architecture and use the AWS `provided.al2023` runtime. Build Go 1.26+ code with a Go 1.26+ toolchain; the OS-only Lambda runtime does not provide the compiler.
