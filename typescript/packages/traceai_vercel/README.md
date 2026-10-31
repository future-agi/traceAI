# @traceai/vercel

Future Instrumentation (FI) integration for the **Vercel AI SDK**.

This package provides a drop-in `SpanProcessor` that converts Vercel AI SDK spans into the FI OpenTelemetry semantic conventions, ready to be exported to TraceAI or any OpenTelemetry backend.

## Installation

```bash
npm install @traceai/vercel
# or
yarn add @traceai/vercel
# or
pnpm add @traceai/vercel
```

## Quick-start (Next.js)

### 1. Create a one-time `instrumentation.ts`

```typescript
// instrumentation.ts – import this **once** on the server (e.g. in _app.tsx)
// eslint-disable-next-line @typescript-eslint/ban-ts-comment
// @ts-ignore   @vercel/otel ships without types
import { registerOTel } from "@vercel/otel";
import type { SpanExporter } from "@opentelemetry/sdk-trace-base";

import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";
import { FISimpleSpanProcessor, isFISpan } from "@traceai/vercel";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-grpc";
import { Metadata } from "@grpc/grpc-js";

diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.DEBUG);

export function register() {
  registerOTel({
    attributes: {
      "service.name": "vercel-project",
      project_name: "vercel-project",
      project_type: "observe",
    },
    spanProcessors: [
      new FISimpleSpanProcessor({
        exporter: (() => {
          const metadata = new Metadata();
          metadata.set("x-api-key", process.env.FI_API_KEY as string);
          metadata.set("x-secret-key", process.env.FI_SECRET_KEY as string);

          return new OTLPTraceExporter({
            url: "grpc://grpc.futureagi.com",
            metadata,
          }) as unknown as SpanExporter;
        })(),

        // Export only FI spans produced by the Vercel AI SDK
        spanFilter: isFISpan,
      }),
    ],
  });

  diag.info("OTLP gRPC span processor registered via @vercel/otel");
}
```

### 2. Use the tracer in your API / Route handlers

```typescript
// pages/api/story.ts
import type { NextApiRequest, NextApiResponse } from "next";
import { register as registerTracing } from "../../instrumentation";

import { generateText } from "ai";
import { openai } from "@ai-sdk/openai";

export default async function handler(
  req: NextApiRequest,
  res: NextApiResponse,
) {
  // Safe – subsequent calls are a no-op inside the same runtime
  registerTracing();

  const result = await generateText({
    model: openai("gpt-4o-mini"),
    prompt: "Say hello from a traced function!",
    experimental_telemetry: { isEnabled: true },
  });

  res.status(200).json({ text: result.text });
}
```

That’s it! Every invocation will emit FI-compliant OpenTelemetry spans you can inspect in the TraceAI dashboard.

## Eve native destination

For Eve 0.80.0, keep Eve's own `otel()` and `otelIntegration()` files. Do not use the Next.js setup above, register another provider, or wrap Eve's model calls with another instrumentor.

Install the standard export processor and exporter alongside this package:

```bash
npm install @traceai/vercel @opentelemetry/sdk-trace-base @opentelemetry/exporter-trace-otlp-http
```

Set `FI_API_KEY`, `FI_SECRET_KEY` and `FI_PROJECT_NAME`. Add the project to the resource in your single `agent/instrumentation/otel.ts`:

```ts
import { otel } from 'eve/instrumentation/otel';

export default otel({
  resource: {
    project_name: process.env.FI_PROJECT_NAME,
    project_type: 'observe',
  },
});
```

Add `agent/instrumentation/futureagi.ts`:

```ts
import { otelIntegration } from 'eve/instrumentation/otel';
import { BatchSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { createEveDestination } from '@traceai/vercel';

export default otelIntegration(createEveDestination({
  processor: new BatchSpanProcessor(new OTLPTraceExporter({
    url: `${(process.env.FI_BASE_URL || 'https://api.futureagi.com').replace(/\/+$/, '')}/tracer/v1/traces`,
    headers: {
      'X-Api-Key': process.env.FI_API_KEY!,
      'X-Secret-Key': process.env.FI_SECRET_KEY!,
    },
  })),
  captureContent: process.env.FI_CAPTURE_CONTENT === '1',
}));
```

`createEveDestination()` returns native destination options, not a provider. It:

- Applies Eve's destination-local redaction by default, including prompts, responses, tool payloads, errors and session titles, and removes principal IDs. Set `FI_CAPTURE_CONTENT=1` to opt in; Eve's global capture ceiling still applies.
- Maps native agent, step, model and tool kinds, adds `session.id` from `gen_ai.conversation.id`, and maps retained content to input/output attributes.
- Keeps model token usage and moves turn/step totals to `eve.aggregate.usage.*` so summing model usage does not count aggregates twice.
- Filters `workflow` and `@vercel/otel/fetch` scopes and reparents their children through known filtered ancestors. Ancestry is bounded to 4,096 filtered spans; active children keep their already-resolved parent.
- Leaves other destinations' spans unchanged and delegates flushing/shutdown to the supplied export processor.

Pass a `BatchSpanProcessor` or `SimpleSpanProcessor`. The destination forwards ended spans only, so processors that require `onStart` are not supported. Eve manages the provider lifecycle. Native content capture and Workflow retention remain Eve's responsibility.

## Environment variables

```bash
# TraceAI telemetry credentials
FI_API_KEY=your_api_key
FI_SECRET_KEY=your_secret_key
```

## API

### `FISimpleSpanProcessor`

```ts
new FISimpleSpanProcessor({ exporter, spanFilter? })
```

Wraps the underlying Vercel AI SDK spans with FI semantic conventions and forwards them to the provided exporter.

**Parameters**

• `exporter` **(required)** – any OpenTelemetry `SpanExporter`  
• `spanFilter?` – predicate `(span) => boolean` to decide which spans to export. Use the exported helper `isFISpan` to export only FI spans.

### `isFISpan(span)`

Utility predicate that returns `true` if the span already contains FI semantic conventions.


## Support

For support, please open an issue in our [GitHub repository](https://github.com/future-agi/traceAI/issues).

