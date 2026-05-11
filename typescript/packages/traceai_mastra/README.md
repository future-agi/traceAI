# TraceAI Mastra

## Installation

```shell
npm install --save @traceai/mastra @mastra/observability @mastra/otel-exporter
```

## Usage

`@traceai/mastra` exposes `FITraceExporter`, an OpenTelemetry `SpanExporter`
that decorates each span with Future AGI semantic attributes and ships it to
the FI ingest endpoint.

In Mastra 1.32+, custom telemetry is wired through the `observability` field
on `Mastra`, and a custom OTel `SpanExporter` is plugged into the official
`@mastra/otel-exporter`. The Mastra runtime converts its internal spans to
OpenTelemetry `ReadableSpan`s first; `FITraceExporter` then runs its FI
attribute decoration on those spans before the OTLP export.

```typescript
import { Mastra } from "@mastra/core";
import { Observability } from "@mastra/observability";
import { OtelExporter } from "@mastra/otel-exporter";
import { FITraceExporter, isFISpan } from "@traceai/mastra";

export const mastra = new Mastra({
  // ...other config
  observability: new Observability({
    configs: {
      default: {
        serviceName: "traceai-mastra-agent", // appears as the project name in Future AGI
        exporters: [
          new OtelExporter({
            exporter: new FITraceExporter({
              url: "https://api.futureagi.com/tracer/v1/traces",
              headers: {
                "x-api-key": process.env.FI_API_KEY,
                "x-secret-key": process.env.FI_SECRET_KEY,
              },
              // optional: drop spans that did not receive an FI span kind
              // (e.g. raw HTTP / framework spans). Spans are still emitted
              // to other Mastra exporters, just not to Future AGI.
              spanFilter: isFISpan,
            }),
          }),
        ],
      },
    },
  }),
});
```

## Examples

### Weather Agent

To run the canonical Mastra weather agent example and ingest the spans into
Future AGI (or any other OpenInference-compatible platform):

- Create a new Mastra project.
- Wire `FITraceExporter` into the project's `observability` config:

```typescript
// chosen-project-name/src/index.ts
import { Mastra } from "@mastra/core/mastra";
import { PinoLogger } from "@mastra/loggers";
import { LibSQLStore } from "@mastra/libsql";
import { Observability } from "@mastra/observability";
import { OtelExporter } from "@mastra/otel-exporter";
import { FITraceExporter, isFISpan } from "@traceai/mastra";

import { weatherAgent } from "./agents";

export const mastra = new Mastra({
  agents: { weatherAgent },
  storage: new LibSQLStore({ url: ":memory:" }),
  logger: new PinoLogger({ name: "Mastra", level: "info" }),
  observability: new Observability({
    configs: {
      default: {
        serviceName: "weather-agent",
        exporters: [
          new OtelExporter({
            exporter: new FITraceExporter({
              url: "https://api.futureagi.com/tracer/v1/traces",
              headers: {
                "x-api-key": process.env.FI_API_KEY!,
                "x-secret-key": process.env.FI_SECRET_KEY!,
              },
              spanFilter: isFISpan,
            }),
          }),
        ],
      },
    },
  }),
});
```

- Run the agent:

```shell
npm run dev
```
