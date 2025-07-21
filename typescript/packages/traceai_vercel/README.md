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

