# @traceai/typesafe

OpenTelemetry instrumentation for the [TypeSafe AI](https://typesafe.ai) TypeScript SDK (`@typesafe-ai/sdk`), the client for the System One decision API. It matches the Python package `traceAI-typesafe`.

Every `TypeSafeClient.systemOne(...)` call becomes one `EVALUATOR` span named `typesafe.system_one`, however many questions it asks. Each question becomes `typesafe.question.<name>.*` attributes:

| Answer type | Attributes |
| --- | --- |
| `noul` | `type`, `noul` (probability of yes, a number) |
| `choice` | `type`, `choice` (the label), `confidence` |
| `score` | `type`, `score` (expected score, a number), `confidence` |
| `box` | `type`, `box.top`, `box.bottom`, `box.left`, `box.right`, `confidence` when not null |

The span also carries `gen_ai.provider.name=typesafe`, `gen_ai.request.model` (as you asked for it, or the client default), `gen_ai.response.model` plus `typesafe.response.model` when the API resolved an alias, `gen_ai.usage.input_tokens` / `output_tokens` / `total_tokens`, `typesafe.request_id`, `server.address` and `typesafe.question_count`. Probability maps, score legends and image bytes are never recorded.

## Install

```bash
npm install @traceai/typesafe @typesafe-ai/sdk
```

Supported: `@typesafe-ai/sdk` 0.5.7 up to (not including) 1.0.0, Node.js 22 or newer (the SDK's own requirement).

## Use

Zero code: `@traceai/auto` enables it when `@typesafe-ai/sdk` is installed.

Or explicitly:

```ts
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor, ConsoleSpanExporter } from "@opentelemetry/sdk-trace-base";
import { TypeSafeInstrumentation } from "@traceai/typesafe";
import * as typesafe from "@typesafe-ai/sdk";

const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(new ConsoleSpanExporter())] });
provider.register();

const instrumentation = new TypeSafeInstrumentation();
instrumentation.setTracerProvider(provider);
instrumentation.manuallyInstrument(typesafe);

const client = new typesafe.TypeSafeClient(); // reads TYPESAFE_API_KEY
const { answers } = await client.systemOne({
  state: "I was charged twice. Please help.",
  questions: { billing: typesafe.noul("Is this about billing?") },
});
```

The returned value is still the SDK's `APIPromise`: `withResponse()`, `asResponse()` and `map()` work as before. The instrumentation never sets `baseURL` or a model.

## Privacy

Content is captured by default and goes through the same `TraceConfig` as every traceAI package (`new TypeSafeInstrumentation({ traceConfig })`, or the `FI_HIDE_*` environment variables).

| Setting | Effect |
| --- | --- |
| default | `input.value` = the state and the questions (instructions, criteria); `output.value` and `typesafe.question.<name>.choice` = the decisions |
| `hideInputs` / `hideInputText` | no `input.value`, no `typesafe.idempotency_key` |
| `hideOutputs` / `hideOutputText` | no `output.value`, no `choice` labels; numeric answers stay |

Always recorded: question names and types (they are attribute keys, so keep secrets out of question names), numeric answers, model, usage, request id. Never recorded: the API key, stack traces, the text of client-side errors, image bytes. The API's own error message is added to the span status only while neither inputs nor outputs are hidden.

## Errors and retries

An HTTP error or a connection error ends the span with `ERROR`, `error.type`, `http.response.status_code`, the server's `typesafe.error_type`, `typesafe.retry_after_ms` and the request id; the error reaches your code unchanged. A call cancelled through an `AbortSignal` is marked `typesafe.cancelled`. The SDK's own retries run inside the one span.

## Differences from the Python package

- The span kind is written under the TypeScript key `fi.span.kind`; Python writes `gen_ai.span.kind`. Both mean `EVALUATOR`.
- The TypeScript SDK exposes the request id only on the response headers, so a plain `await` reads it from the (already buffered) response; Python reads it from the result object.
- The TypeScript SDK keeps its API key private and has no Python-style `typesafe.response.field_path` error field.

A call whose promise is never awaited and never read through `asResponse()` or `withResponse()` is not ended.
