# @traceai/anthropic

OpenTelemetry instrumentation for Anthropic's Claude API. This package provides automatic tracing and monitoring for your Anthropic applications.

## Installation

```bash
npm install @traceai/anthropic
# or
yarn add @traceai/anthropic
# or
pnpm add @traceai/anthropic
```

## Quick Start

```typescript
// export Future AGI API KEYS
// export FI_API_KEY=your_api_key
// export FI_SECRET_KEY=your_secret_key

import { register, ProjectType } from "@traceai/fi-core";
import { AnthropicInstrumentation } from "@traceai/anthropic";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";

// Enable OpenTelemetry internal diagnostics (optional, for debugging)
diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.INFO);

// 1. Register FI Core TracerProvider
const tracerProvider = register({
  projectName: "your-project-name",
  projectType: ProjectType.OBSERVE,
  sessionName: "your-session-name"
});

// 2. Initialize and enable Anthropic Instrumentation
const anthropicInstrumentation = new AnthropicInstrumentation({});
registerInstrumentations({
  instrumentations: [anthropicInstrumentation],
  tracerProvider: tracerProvider,
});

// 3. Dynamically import Anthropic SDK AFTER instrumentation is registered
const Anthropic = (await import("@anthropic-ai/sdk")).default;
const client = new Anthropic();

// 4. Use Anthropic as normal
const response = await client.messages.create({
  model: "claude-3-haiku-20240307",
  max_tokens: 50,
  messages: [{ role: "user", content: "Hello, Claude!" }],
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
# For Anthropic API
ANTHROPIC_API_KEY=your_anthropic_api_key

# For TraceAI telemetry
FI_API_KEY=your_api_key
FI_SECRET_KEY=your_secret_key
```

## Features

- Automatic tracing of Anthropic API calls
- Support for both streaming and non-streaming requests
- Captures token usage and response metadata
- Tracks tool usage in conversations
- No manual instrumentation required
- Integration with TraceAI's observability platform

## Peer Dependencies

This package requires the following peer dependency:
- `@anthropic-ai/sdk`: ^0.27.3

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

