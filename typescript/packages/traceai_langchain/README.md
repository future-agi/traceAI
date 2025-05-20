# @traceai/langchain

OpenTelemetry instrumentation for LangChain.js. This package provides automatic tracing and monitoring for your LangChain applications.

## Installation

```bash
npm install @traceai/langchain
# or
yarn add @traceai/langchain
# or
pnpm add @traceai/langchain
```

## Quick Start

```typescript
// export Future AGI API KEYS
// export FI_API_KEY=your_api_key
// export FI_SECRET_KEY=your_secret_key

import { register, ProjectType } from "@traceai/fi-core";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { LangChainInstrumentation } from "@traceai/langchain";
import { ChatOpenAI } from "@langchain/openai";
import { HumanMessage } from "@langchain/core/messages";
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";
import * as CallbackManagerModule from "@langchain/core/callbacks/manager";

// Enable OpenTelemetry internal diagnostics (optional, for debugging)
diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.DEBUG);

// 1. Register FI Core TracerProvider
const tracerProvider = register({
  projectName: "your-project-name",
  projectType: ProjectType.OBSERVE,
  sessionName: "your-session-name"
});

// 2. Register LangChain Instrumentation
const lcInstrumentation = new LangChainInstrumentation();
registerInstrumentations({
  tracerProvider: tracerProvider,
  instrumentations: [lcInstrumentation],
});

// 3. Manually instrument LangChain (required as it doesn't have a traditional module structure)
lcInstrumentation.manuallyInstrument(CallbackManagerModule);

// 4. Use LangChain as normal
const chatModel = new ChatOpenAI({
  openAIApiKey: process.env.OPENAI_API_KEY,
  metadata: {
    session_id: "your-session-id",
  },
});

// Your LangChain code here...

// 5. Don't forget to shutdown the tracer provider when done
try {
  await tracerProvider.shutdown();
  console.log("Tracer provider shut down successfully.");
} catch (error) {
  console.error("Error shutting down tracer provider:", error);
}
```

## Environment Variables

The following environment variables are required for telemetry:

```bash
FI_API_KEY=your_api_key
FI_SECRET_KEY=your_secret_key
```



## Features

- Automatic tracing of LangChain operations
- Support for both ESM and CommonJS modules
- Compatible with LangChain.js v0.2.0 and v0.3.0
- Integration with TraceAI's observability platform

## Peer Dependencies

This package requires the following peer dependencies:
- `@langchain/core`: ^0.2.0 || ^0.3.0

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

