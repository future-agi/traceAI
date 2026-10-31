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

## Cloudflare AI Gateway and provider attribution

Use the existing `OpenAIInstrumentation` with an OpenAI-compatible client; no
Cloudflare-specific instrumentation package is needed. For new Cloudflare
integrations, use the [REST endpoint](https://developers.cloudflare.com/ai-gateway/usage/rest-api/)
`https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1`. Existing
`https://gateway.ai.cloudflare.com/v1/{account_id}/{gateway_id}/compat` clients
continue to work.

Chat, legacy completion and Responses spans record `server.address` separately
from `gen_ai.provider.name`. For Cloudflare's REST and compatibility endpoints,
explicit request prefixes identify the requested provider: `openai/`,
`anthropic/`, `google/` or `google-ai-studio/`, and `xai/`. Workers AI's `@cf/`
prefix identifies `cloudflare`, not the author of the hosted model.

Dynamic routes, unrecognized prefixes and unprefixed aliases do not establish a
provider, so the provider attribute is omitted. Response model names never
replace this routing decision. Direct OpenAI and Azure OpenAI hosts retain their
respective provider identity; other custom endpoints have no inferred provider.
The endpoint attributes never contain URL credentials, account/gateway path
segments, query strings or fragments.

Application spans describe the requested route, not a gateway's eventual fallback
or routing choice. Prefer [Cloudflare's native OpenTelemetry export](https://developers.cloudflare.com/ai-gateway/observability/otel-integration/)
when you need gateway-side routing details. Choose native gateway export or
application instrumentation for a single view of each call; enabling both adds
two tracing layers, not one automatically deduplicated span. This package does
not configure or replace native gateway export.

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

