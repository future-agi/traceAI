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

## Module System Support

This package supports both **CommonJS** and **ESM** module systems for maximum compatibility.

### ESM (ES Modules)
```typescript
import { OpenAIInstrumentation } from '@traceai/openai';
import { register, ProjectType } from '@traceai/fi-core';
```

### CommonJS
```javascript
const { OpenAIInstrumentation } = require('@traceai/openai');
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

```javascript
// export Future AGI API KEYS
// export FI_API_KEY=your_api_key
// export FI_SECRET_KEY=your_secret_key

const { register, ProjectType } = require("@traceai/fi-core");
const { OpenAIInstrumentation } = require("@traceai/openai");
const { registerInstrumentations } = require("@opentelemetry/instrumentation");
const { diag, DiagConsoleLogger, DiagLogLevel } = require("@opentelemetry/api");

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
const OpenAI = require("openai");
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

## Vercel AI Gateway

Use the same instrumentor for the gateway's OpenAI-compatible client; do not add a second gateway instrumentor. This non-streaming Chat Completions recipe uses `openai@5.23.2`. The declared peer and automatic-hook range remains OpenAI 4.x/5.x, not 6.x/7.x. For applications using `ai` instead of `openai`, use the [Vercel AI SDK integration](https://docs.futureagi.com/docs/integrations/traceai/vercel).

```bash
npm install @traceai/openai @traceai/fi-core@1.1.0 openai@5.23.2
export AI_GATEWAY_API_KEY="your-vercel-gateway-key"
export AI_GATEWAY_MODEL="provider/model"
export FI_API_KEY="your-futureagi-api-key"
export FI_SECRET_KEY="your-futureagi-secret-key"
# Keep prompt, answer, invocation and error content out of telemetry.
export FI_HIDE_INPUTS=true
export FI_HIDE_OUTPUTS=true
export FI_HIDE_LLM_INVOCATION_PARAMETERS=true
export FI_HIDE_ERROR_DETAILS=true
```

Choose `AI_GATEWAY_MODEL` from [Vercel's model list](https://vercel.com/docs/ai-gateway/models-and-providers). With Node.js 22.23.4 or later, save as `gateway.mts` and run `node gateway.mts`:

```typescript
import OpenAI from "openai";
import { register, ProjectType } from "@traceai/fi-core";
import { OpenAIInstrumentation } from "@traceai/openai";

const model = process.env.AI_GATEWAY_MODEL;
if (!model) throw new Error("Set AI_GATEWAY_MODEL to a Vercel model ID.");

const provider = register({
  projectName: "my-gateway-app",
  projectType: ProjectType.OBSERVE,
});
const instrumentation = new OpenAIInstrumentation();
instrumentation.setTracerProvider(provider);
instrumentation.manuallyInstrument(OpenAI);

const client = new OpenAI({
  baseURL: "https://ai-gateway.vercel.sh/v1",
  apiKey: process.env.AI_GATEWAY_API_KEY,
});
try {
  const response = await client.chat.completions.create({
    model,
    messages: [{ role: "user", content: "Say hello in five words." }],
  });
  console.log(response.choices[0].message.content);
} finally {
  await provider.shutdown();
}
```

`gen_ai.provider.name=vercel` identifies the receiving gateway at `ai-gateway.vercel.sh`, not a model's author or the gateway's eventual upstream provider. Attribution is set before the request, including for failed calls, and is shared by Chat Completions, Completions, Responses and Embeddings. This is instrumentor coverage, not a claim that Vercel implements every OpenAI endpoint. Other endpoints retain their existing attribution behavior.

Open **Observe → my-gateway-app** to see the client span. Successful non-streaming Chat Completions include the response model and reported usage; absent usage is not zero. Content hiding does not remove model names or token counts and does not hide requests from Vercel. Tool definitions and custom attributes need their own privacy review.

This route does not expose gateway routing, upstream retries or billing. [Vercel Trace Drains](https://vercel.com/docs/ai-gateway/observability-and-spend/trace-drains) are a separate server-side source without prompts or completions. Do not sum their usage with application spans or layer multiple instrumentors on the same client call. This recipe does not configure a drain destination.

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

