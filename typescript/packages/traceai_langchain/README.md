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
import { awaitAllCallbacks } from "@langchain/core/callbacks/promises";

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

try {
  const response = await chatModel.invoke([new HumanMessage("What is OpenTelemetry?")]);
  console.log(response.content);
} finally {
  // LangChain runs tracing callbacks in the background. Finish them before
  // shutting down the provider so the root span is not lost.
  await awaitAllCallbacks();
  await tracerProvider.shutdown();
}
```

For a long-running service, leave the provider running between requests. When the
service stops, finish in-flight chains, await `awaitAllCallbacks()`, then shut down
the provider. Before an explicit `forceFlush()`, drain callbacks in the same order.

## Environment Variables

The following environment variables are required for telemetry:

```bash
FI_API_KEY=your_api_key
FI_SECRET_KEY=your_secret_key
```



## Features

- Automatic tracing of LangChain operations
- Support for both ESM and CommonJS modules
- Compatible with `@langchain/core` 0.2.x, 0.3.x, and 1.x
- Integration with TraceAI's observability platform

## Peer Dependencies

This package requires the following peer dependencies:
- `@langchain/core`: ^0.2.0 || ^0.3.0 || ^1.0.0

Keep your LangChain adapters on a release compatible with your core version. For
example, use `@langchain/openai` 1.x with core 1.x. On core 0.3.80 and 1.x,
CommonJS applications can use the automatic require hook when instrumentation is
initialized before importing LangChain. For ESM, an already-loaded module, core
0.2.x, or earlier 0.3.x, use `manuallyInstrument()` as above.

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

