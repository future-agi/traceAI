# @traceai/anthropic

OpenTelemetry instrumentation for Anthropic SDKs without built-in tracing. The supported peer dependency is `@anthropic-ai/sdk@^0.27.3`.

## SDKs with native OpenTelemetry

Do not add this legacy instrumentor to SDKs with built-in OpenTelemetry, such as `@anthropic-ai/sdk@1.3.0`. Those SDKs already own their request spans, promise helpers and stream lifecycle. `manuallyInstrument()` leaves them unmodified and logs a diagnostic warning. It does not enable native tracing if you have disabled it.

Configure native SDK tracing through the SDK's `openTelemetry` option and your OpenTelemetry tracer provider. The legacy instrumentor's `traceConfig` and `FI_HIDE_*` controls do **not** apply to native SDK spans. Use the SDK's content controls and your export pipeline's privacy policy instead. See [Anthropic's SDK tracing configuration](https://www.npmjs.com/package/@anthropic-ai/sdk/v/1.3.0#opentelemetry).

The setup below is for the legacy SDK peer dependency only.

## Installation

```bash
npm install @traceai/anthropic
# or
yarn add @traceai/anthropic
# or
pnpm add @traceai/anthropic
```

## Module System Support

This package supports both **CommonJS** and **ESM** module systems for maximum compatibility.

### ESM (ES Modules)
```typescript
import { AnthropicInstrumentation } from '@traceai/anthropic';
import { register, ProjectType } from '@traceai/fi-core';
```

### CommonJS
```javascript
const { AnthropicInstrumentation } = require('@traceai/anthropic');
const { register, ProjectType } = require('@traceai/fi-core');
```

### TypeScript Configuration

For optimal compatibility, ensure your `tsconfig.json` includes:

```json
{
  "compilerOptions": {
    "moduleResolution": "node",
    "esModuleInterop": true,
    "allowSyntheticDefaultImports": true
  }
}
```

The `module` setting can be `"commonjs"`, `"esnext"`, or any other module system based on your project needs.

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

