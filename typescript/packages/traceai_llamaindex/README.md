# @traceai/llamaindex

## Deprecated: LlamaIndex.TS

[LlamaIndex.TS is deprecated](https://github.com/run-llama/LlamaIndexTS#deprecation-notice) and no longer maintained upstream. The `@traceai/llamaindex` TypeScript integration is deprecated as well. Do not use this integration for new applications.

Existing releases and source remain available; this notice does not remove the package or change its runtime behavior. For existing applications, keep your tested dependency versions locked while planning a migration. The legacy setup below is retained for reference, not as a recommendation for new installs or a guarantee of compatibility with current LlamaIndex releases.

The LlamaIndex **Python integration is not deprecated**. If you are moving your application to Python, follow the [Python LlamaIndex guide](https://docs.futureagi.com/docs/integrations/traceai/llamaindex). If you need to stay on TypeScript, choose a maintained framework and its tracing route from the [integration directory](https://docs.futureagi.com/docs/integrations). Changing frameworks requires application changes; this is not a drop-in replacement.

<details>
<summary>Legacy package reference</summary>

## Installation

```bash
npm install @traceai/llamaindex
# or
yarn add @traceai/llamaindex
# or
pnpm add @traceai/llamaindex
```

## Quick Start

```typescript
// Set up environment variables
process.env["FI_API_KEY"] = "your-api-key";
process.env["FI_SECRET_KEY"] = "your-secret-key";
process.env["OPENAI_API_KEY"] = "your-openai-api-key";

import { register, ProjectType } from "@traceai/fi-core";
import { diag, DiagConsoleLogger, DiagLogLevel } from "@opentelemetry/api";
import { OpenAI, OpenAIEmbedding } from "@llamaindex/openai";
import * as LlamaIndex from "llamaindex";
import { LlamaIndexInstrumentation } from "@traceai/llamaindex";

// Enable OpenTelemetry internal diagnostics (optional, for debugging)
diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.INFO);

async function main() {
  // 1. Configure LlamaIndex Settings
  const { Settings } = LlamaIndex;
  Settings.llm = new OpenAI({ model: "gpt-3.5-turbo" });
  Settings.embedModel = new OpenAIEmbedding({ model: "text-embedding-ada-002" });

  // 2. Register FI Core TracerProvider
  const tracerProvider = register({
    projectName: "your-project-name",
    projectType: ProjectType.OBSERVE,
    setGlobalTracerProvider: true,
  });

  // 3. Initialize and register LlamaIndex Instrumentation
  const instrumentation = new LlamaIndexInstrumentation({});
  instrumentation.manuallyInstrument(LlamaIndex);

  // 4. Use LlamaIndex as normal
  const { Document, VectorStoreIndex } = LlamaIndex;
  
  // Create documents
  const documents = [
    new Document({ text: "LlamaIndex is a data framework for LLM applications." }),
    new Document({ text: "OpenTelemetry provides observability for applications." }),
  ];

  // Build vector store index
  const index = await VectorStoreIndex.fromDocuments(documents);

  // Query the index
  const queryEngine = index.asQueryEngine();
  const response = await queryEngine.query({
    query: "What is LlamaIndex?"
  });

  console.log("Response:", response.toString());

  // 5. Don't forget to shutdown the tracer provider when done
  try {
    await tracerProvider.shutdown();
    console.log("Tracer provider shut down successfully.");
  } catch (error) {
    console.error("Error shutting down tracer provider:", error);
  }
}

main().catch(console.error);
```

## Environment Variables

The following environment variables are required for telemetry:

```bash
FI_API_KEY=your_api_key
FI_SECRET_KEY=your_secret_key
OPENAI_API_KEY=your_openai_api_key
```

## Features

- Automatic tracing of LlamaIndex operations including:
  - Vector store operations
  - Query engine execution
  - Retrieval operations
  - Embedding generation
  - LLM chat completions
- Support for both ESM and CommonJS modules
- Compatible with LlamaIndex.js
- Integration with TraceAI's observability platform

## Instrumented Operations

This instrumentation automatically traces:

- **Vector Store Operations**: Document indexing, vector storage
- **Query Engine**: Query execution and response generation
- **Retrieval**: Document retrieval and similarity search
- **Embeddings**: Text embedding generation
- **LLM Operations**: Chat completions and text generation


## Peer Dependencies

This package requires the following peer dependencies:
- `llamaindex`: >=0.1.0

## Development

```bash
# Install dependencies
pnpm install

# Build the package
pnpm build

# Run tests
pnpm test

# Type checking
pnpm lint
```

## Support

For support, please open an issue in our [GitHub repository](https://github.com/future-agi/traceAI/issues).

Check your TraceAI dashboard at https://app.futureagi.com/dashboard/observe for traces!

</details>
