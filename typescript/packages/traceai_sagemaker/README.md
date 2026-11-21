# @traceai/sagemaker

OpenTelemetry instrumentation for Amazon SageMaker Runtime endpoint calls made with `@aws-sdk/client-sagemaker-runtime` (AWS SDK for JavaScript v3). It matches the Python package `traceAI-sagemaker`.

Each `InvokeEndpointCommand`, `InvokeEndpointAsyncCommand` and `InvokeEndpointWithResponseStreamCommand` sent through a `SageMakerRuntimeClient` becomes one `LLM` span named `sagemaker.invoke_endpoint`, `sagemaker.invoke_endpoint_async` or `sagemaker.invoke_endpoint_with_response_stream`.

| Attribute | Value |
| --- | --- |
| `gen_ai.provider.name` | `aws.sagemaker` |
| `gen_ai.operation.name` | `invoke_endpoint`, `invoke_endpoint_async`, `invoke_endpoint_with_response_stream` |
| `gen_ai.request.model`, `aws.sagemaker.endpoint_name` | the endpoint name |
| `aws.sagemaker.inference_component`, `aws.sagemaker.target_variant`, `aws.sagemaker.inference_id` | the request's `InferenceComponentName`, `TargetVariant`, `InferenceId` (async: also the returned `InferenceId`) |
| `aws.sagemaker.invoked_variant`, `aws.request_id` | the response's `InvokedProductionVariant` and request id |
| `input.value`, `input.mime_type` | the request body (JSON or text by `ContentType`); a binary body is noted as `[binary content: <type>, <n> bytes]` |
| `gen_ai.request.parameters` | the common sampling parameters found in a JSON body (`max_tokens`, `temperature`, ...) |
| `output.value`, `output.mime_type` | the generated text of a recognised response shape, else the whole text/JSON body; binary output is noted by type and size |
| `gen_ai.response.model`, `gen_ai.usage.input_tokens` / `output_tokens` / `total_tokens` | read from the common response shapes (OpenAI-style, Anthropic-style, Bedrock-style, Hugging Face TGI); a zero count is kept |
| `gen_ai.server.time_to_first_token` | streaming calls: milliseconds from the call to the first `PayloadPart` |
| `aws.sagemaker.stream.incomplete` | `true` when the consumer stopped reading a stream early |
| `error.type` | the error class, on a failed call or stream |

An async call has no body to record (its payload is in S3).

## Install

```bash
npm install @traceai/sagemaker @aws-sdk/client-sagemaker-runtime
```

Supported: `@aws-sdk/client-sagemaker-runtime` 3.693.0 up to (not including) 4.0.0. Tested against 3.693.0 and 3.1127.0.

## Use

Zero code: `@traceai/auto` enables it when `@aws-sdk/client-sagemaker-runtime` is installed.

Or explicitly:

```ts
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { SageMakerInstrumentation } from "@traceai/sagemaker";
import * as sagemaker from "@aws-sdk/client-sagemaker-runtime";

const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(new ConsoleSpanExporter())] });
provider.register();

const instrumentation = new SageMakerInstrumentation();
instrumentation.setTracerProvider(provider);
instrumentation.manuallyInstrument(sagemaker);

const client = new sagemaker.SageMakerRuntimeClient({ region: "us-east-1" });
const response = await client.send(new sagemaker.InvokeEndpointCommand({
  EndpointName: "my-endpoint",
  ContentType: "application/json",
  Body: JSON.stringify({ inputs: "Hello" }),
}));
```

The value `send` returns is the SDK's own. A blocking response's `Body` is not replaced. A streaming response's `Body` is wrapped in an async iterable that forwards every event unchanged, in order; the span ends once, when the stream is exhausted, fails, or the consumer stops reading. A stream that is neither read nor closed ends its span when it is garbage collected.

## Privacy

Content is captured by default and goes through the same `TraceConfig` as every traceAI package (`new SageMakerInstrumentation({ traceConfig })`, or the `FI_HIDE_*` environment variables).

| Setting | Effect |
| --- | --- |
| default | request body, parameters, generated text and the whole response body when no text field is recognised |
| `hideInputs`, `hideInputText`, `hideInputMessages`, `hideInputImages` | no `input.value`, no `gen_ai.request.parameters` |
| `hideOutputs`, `hideOutputText`, `hideOutputMessages` | no `output.value` (blocking or streamed) |
| any hide setting | the response is never written as a whole object: only the generated text is recorded, and only when its own direction is not hidden. No raw copy of either side is recorded. |

Token counts, model, endpoint, variant and request id are always recorded. Error text can carry request or response content, so only the error class is recorded; the SDK's error message is never added to the span.

## Differences from the Python package

- The span kind is written under the TypeScript key `fi.span.kind`; Python writes `gen_ai.span.kind`. Both mean `LLM`.
- Streaming spans also carry `gen_ai.server.time_to_first_token`.
- The SDK reads a blocking response body before `send` resolves, so a body read error surfaces from `send` itself (the span records the error class) and the instrumentation never swaps the body.
- Python leaves `gen_ai.request.parameters` to the masker; here it is also left out under the input hide settings the masker does not cover (`hideInputText`, `hideInputMessages`, `hideInputImages`).
- Python records a whole JSON response body when no text is recognised, whichever settings are on; here that body is only recorded when no hide setting is on.
