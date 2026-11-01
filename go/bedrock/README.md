# Bedrock Runtime for Go

Trace Bedrock Runtime calls in Future AGI with messages, tools, token usage and full response-stream timing.

## Install

Requires Go 1.26+ and AWS SDK for Go v2 Bedrock Runtime 1.63.9+.

```sh
go get github.com/future-agi/traceAI/go/bedrock
```

## Wrap your AWS client

Keep your existing AWS client configuration and OpenTelemetry provider. The adapter does not replace providers, exporters, AWS middleware, retry policies or credentials.

```go
import bedrock "github.com/future-agi/traceAI/go/bedrock"

client := bedrock.NewClient(awsClient, bedrock.WithTracerProvider(provider))
ctx = bedrock.ContextWithSessionID(ctx, "weather-session")
response, err := client.Converse(ctx, request)
```

Without `WithTracerProvider`, the adapter uses the current global OpenTelemetry provider. Configure a provider before making calls. See the [complete runnable example](example/main.go) for standard OTLP export, AWS configuration, and both synchronous and streaming calls.

Set `FI_API_KEY` and `FI_SECRET_KEY` in your environment. Configure the standard OTLP HTTP exporter with:

- Endpoint: `https://api.futureagi.com/tracer/v1/traces`
- Headers: `X-Api-Key` from `FI_API_KEY`, `X-Secret-Key` from `FI_SECRET_KEY`
- Compression: none
- Resource attributes: `project_name` and `project_type=observe`

The example also uses `AWS_REGION`, the standard AWS credential chain, and `BEDROCK_MODEL_ID` for a model or inference profile you can access. It accepts `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` for a different trace destination.

## Read streams to completion

```go
response, err := client.ConverseStream(ctx, request)
if err != nil {
    return err
}
stream := response.GetStream()
defer stream.Close()
for event := range stream.Events() {
    // Handle the original AWS event.
    _ = event
}
return stream.Err()
```

Stream outputs have adapter-specific Go types. Their `GetStream()` returns the AWS reader interface with `Events()`, `Err()` and `Close()`. Read from that method, not from the embedded native output. SDK event objects and ordering are preserved. Always close streams, including when stopping early; use a cancelable context for requests whose consumers may disappear. An unread, unclosed stream without a canceled context remains open, just as an AWS stream does.

Spans end after EOF, an SDK error, cancellation or close, rather than at response headers. Early closes carry `traceai.stream.closed_early=true`; usage is only present if the service sent it. Time to first token is measured at the first nonempty text or tool-argument delta, not at a role/header event, and exported as `gen_ai.server.time_to_first_token` in milliseconds.

The adapter isolates the standard AWS `restJson1` protocol for each streaming call, so you can use one AWS client for overlapping streams. Route streaming calls through the adapter: direct native streaming calls on a shared client can race in the current AWS SDK. This isolation also applies when tracing is suppressed or unsampled. Custom protocol implementations and custom `Runtime` implementations remain responsible for their own concurrency safety.

## Supported calls

| Call | Captured content |
| --- | --- |
| `Converse`, `ConverseStream` | Text, system instructions, tool definitions, tool calls, and synchronous tool results |
| `InvokeModel`, `InvokeModelWithResponseStream` | Anthropic Messages requests with `anthropic_version=bedrock-2023-05-31` and an identifiable `anthropic.*` model ID or regional inference profile |

Unknown InvokeModel families, opaque provisioned-model IDs and unsupported schemas pass through without semantic spans. Call the original AWS client for other operations, including bidirectional streams. Streaming tool-result, reasoning, citation and media blocks are forwarded but not captured.

Converse token counters retain the service's input, output, total and cache values, including zero. InvokeModel keeps Anthropic's input count separate from cache reads/writes and does not invent totals or absent counters. Request model IDs, including inference-profile ARNs, are retained without rewriting them into guessed model names.

## Control captured data

Set these variables before constructing the adapter, or pass `WithTraceConfig`:

| Variable | Behavior when `true` |
| --- | --- |
| `FI_HIDE_INPUTS` | Redacts the input summary and omits input content |
| `FI_HIDE_OUTPUTS` | Redacts the output summary and omits output content |
| `FI_HIDE_INPUT_MESSAGES`, `FI_HIDE_INPUT_TEXT` | Omits all input content, including system prompts, tool definitions, arguments and results |
| `FI_HIDE_OUTPUT_MESSAGES`, `FI_HIDE_OUTPUT_TEXT` | Omits all output content, including tool arguments |
| `FI_HIDE_LLM_INVOCATION_PARAMETERS` | Omits inference parameters |

Images, binary data, documents, reasoning, guardrail details and raw SDK error text are never recorded. `FI_HIDE_INPUT_IMAGES` and `FI_HIDE_ERROR_DETAILS` are accepted for consistency; setting them to false does not enable those fields. Do not put sensitive data in model, session or user identifiers. The adapter does not control separately configured AWS logging or native instrumentation.

Content capture has separate 32 KiB input/output budgets, at most 40 content attributes per side, 32 input messages, 64 blocks per message and 16 tool definitions. Truncation is marked with `traceai.capture.truncated=true`. These limits affect telemetry only, never application responses. InvokeModel request bodies larger than 64 KiB are not normalized. For supported requests, responses larger than 64 KiB omit captured content but retain service-reported usage and finish reasons, including when output capture is disabled. Binary and unknown blocks are not serialized as a fallback.

Use `ContextWithUserID` for an opaque end-user identifier and `ContextWithSuppression` to skip this adapter for a call. Native AWS diagnostic spans may coexist with the single semantic LLM span; avoid adding another Bedrock semantic instrumentor around the same call.
