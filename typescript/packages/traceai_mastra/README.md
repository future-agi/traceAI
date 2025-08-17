# TraceAI Mastra
## Installation

```shell
npm install --save @traceai/mastra
```

## Usage

```typescript
import { Mastra } from "@mastra/core";
import {
  FiOTLPTraceExporter,
  isFiSpan,
} from "@traceai/mastra";

export const mastra = new Mastra({
  // ... other config
  telemetry: {
    serviceName: "traceai-mastra-agent", // you can rename this to whatever you want to appear in the Phoenix UI
    enabled: true,
    export: {
      type: "custom",
      exporter: new FiOTLPTraceExporter({
        url: process.env.PHOENIX_COLLECTOR_ENDPOINT,
        headers: {
          Authorization: `Bearer ${process.env.PHOENIX_API_KEY}`,
        },
        // optional: filter out http, and other node service specific spans
        // they will still be exported to Mastra, but not to the target of
        // this exporter
        spanFilter: isFiSpan,
      }),
    },
  },
});
```

## Examples

### Weather Agent

To setup the canonical Mastra weather agent example, and then ingest the spans into TraceAI (or any other OpenInference-compatible platform), follow the steps below.

- Create a new Mastra project

- Add the FiOTLPTraceExporter to your Mastra project

```typescript
// chosen-project-name/src/index.ts
import { Mastra } from "@mastra/core/mastra";
import { createLogger } from "@mastra/core/logger";
import { LibSQLStore } from "@mastra/libsql";
import {
  isFiSpan,
  FiOTLPTraceExporter,
} from "@traceai/mastra";

import { weatherAgent } from "./agents";

export const mastra = new Mastra({
  agents: { weatherAgent },
  storage: new LibSQLStore({
    url: ":memory:",
  }),
  logger: createLogger({
    name: "Mastra",
    level: "info",
  }),
  telemetry: {
    enabled: true,
    serviceName: "weather-agent",
    export: {
      type: "custom",
      exporter: new FiOTLPTraceExporter({
        url: "https://api.futureagi.com/tracer/v1/traces",
        headers: {
          "x-api-key": process.env.FI_API_KEY,
          "x-secret-key": process.env.FI_SECRET_KEY,
        },
        spanFilter: isFiSpan,
      }),
    },
  },
});
```

- Run the agent

```shell
npm run dev
```