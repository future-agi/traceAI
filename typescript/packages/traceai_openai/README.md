# @traceai/openai

OpenTelemetry instrumentation for OpenAI's API. This package provides automatic tracing and monitoring for your OpenAI applications.

## Installation

```bash
npm install @traceai/openai
# or
yarn add @traceai/openai
# or
pnpm add @traceai/openai
```

## Quick Start

```typescript
// export Future AGI API KEYS
// export FI_API_KEY=your_api_key
// export FI_SECRET_KEY=your_secret_key

import { register, ProjectType } from "@traceai/fi-core";
import { OpenAIInstrumentation } from "@traceai/openai";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";

// Enable OpenTelemetry internal diagnostics (optional, for debugging)
diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.DEBUG);

// 1. Register FI Core TracerProvider
const tracerProvider = register({
  projectName: "your-project-name",
  projectType: ProjectType.OBSERVE,
  sessionName: "your-session-name"
});

// 2. Register OpenAI Instrumentation BEFORE importing/using OpenAI client
registerInstrumentations({
  tracerProvider: tracerProvider,
  instrumentations: [new OpenAIInstrumentation()],
});

// 3. Import and Initialize OpenAI Client
const OpenAI = (await import("openai")).default;
const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY,
});

// 4. Use OpenAI as normal
const chatCompletion = await openai.chat.completions.create({
  messages: [{ role: "user", content: "Hello!" }],
  model: "gpt-4o",
});

// 5. Don't forget to shutdown the tracer provider when done
try {
  await tracerProvider.shutdown();
  console.log("Tracer provider shut down successfully.");
} catch (error) {
  console.error("Error shutting down tracer provider:", error);
}
```

## Environment Variables

The following environment variables are required:

```bash
# For OpenAI API
OPENAI_API_KEY=your_openai_api_key

# For TraceAI telemetry
FI_API_KEY=your_api_key
FI_SECRET_KEY=your_secret_key
```

## Features

- Automatic tracing of OpenAI API calls
- Support for all OpenAI API endpoints:
  - Chat Completions
  - Completions
  - Embeddings
  - Responses
- Streaming support for chat completions
- No manual instrumentation required
- Integration with TraceAI's observability platform

## Peer Dependencies

This package requires the following peer dependency:
- `openai`: ^4.0.0

## Development

```bash
# Install dependencies
pnpm install

# Build the package
pnpm build

# Run tests
pnpm test

# Type checking
pnpm type:check
```

## Support

For support, please open an issue in our [GitHub repository](https://github.com/future-agi/traceAI/issues).

