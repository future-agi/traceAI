# @traceai/fi-core

Core OpenTelemetry instrumentation library for TypeScript applications with advanced evaluation capabilities for AI systems.

## Overview

`@traceai/fi-core` provides a comprehensive tracing solution built on OpenTelemetry that's specifically designed for AI applications. It offers custom span exporters, evaluation tags, and seamless integration with the TraceAI platform for observability and performance monitoring.

## Features

- **OpenTelemetry Integration**: Built on top of OpenTelemetry APIs with custom implementations
- **Custom Span Exporter**: HTTP-based span exporter with configurable endpoints
- **AI Evaluation Tags**: Comprehensive evaluation system for AI applications with 50+ built-in evaluators
- **Project Management**: Support for project versioning, sessions, and metadata
- **Flexible Configuration**: Environment variable and programmatic configuration support
- **TypeScript Support**: Full TypeScript support with comprehensive type definitions

## Installation

```bash
npm install @traceai/fi-core
# or
pnpm add @traceai/fi-core
# or
yarn add @traceai/fi-core
```

## Module System Support

This package supports both **CommonJS** and **ESM** module systems for maximum compatibility.

### ESM (ES Modules)
```typescript
import { register, ProjectType, EvalTag } from '@traceai/fi-core';
```

### CommonJS
```typescript
const { register, ProjectType, EvalTag } = require('@traceai/fi-core');
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

The `module` setting can be `"commonjs"`, `"esnext"`, or any other module system your project requires.

## Quick Start

### Basic Setup

```typescript
import { register, ProjectType } from '@traceai/fi-core';

// Initialize tracing with minimal configuration
const tracerProvider = register({
  projectName: 'my-ai-project',
  projectType: ProjectType.EXPERIMENT,
});
```

### Advanced Configuration

```typescript
import { register, ProjectType, EvalTag, EvalName, EvalSpanKind } from '@traceai/fi-core';

// Create evaluation tags for AI model monitoring
const evalTags = [
    new EvalTag({
        type: EvalTagType.OBSERVATION_SPAN,
        value: EvalSpanKind.LLM,
        eval_name: EvalName.CONTEXT_ADHERENCE,
        custom_eval_name: 'custom_context_check',
        mapping: {
            "context": "raw.input",
            "output": "raw.output"
        },
        model: ModelChoices.TURING_SMALL
    })
]

// Register with comprehensive configuration
const tracerProvider = register({
  projectName: 'advanced-ai-project',
  projectType: ProjectType.EXPERIMENT,
  projectVersionName: 'v1.0.0',
  evalTags: evalTags,
  sessionName: 'experiment-session-1',
  verbose: true,
});
```

## Core Components

### TraceAI Provider

The `FITracerProvider` extends OpenTelemetry's `BasicTracerProvider` with custom functionality:

- Custom HTTP span exporter
- Automatic resource detection and configuration  
- Built-in UUID generation for trace and span IDs
- Configurable batch or simple span processing

### Evaluation System

The evaluation system provides comprehensive AI model assessment capabilities:

#### Built-in Evaluators

- **Content Quality**: Context adherence, completeness, groundedness
- **Safety & Moderation**: Toxicity, PII detection, content moderation
- **Technical Validation**: JSON validation, regex matching, length checks
- **AI-Specific**: Conversation coherence, prompt injection detection
- **Custom Evaluations**: API calls, custom code evaluation, agent-as-judge

#### Span Types

- `LLM`: Large Language Model operations
- `AGENT`: AI agent executions
- `TOOL`: Tool usage and function calls
- `RETRIEVER`: Information retrieval operations
- `EMBEDDING`: Vector embedding operations
- `RERANKER`: Result reranking operations

### Project Types

- `EXPERIMENT`: For experimental AI development and testing
- `OBSERVE`: For production monitoring and observability

## API Reference


### Environment Variables

- `FI_BASE_URL`: Base URL for the TraceAI collector
- `FI_API_KEY`: API key for authentication
- `FI_SECRET_KEY`: Secret key for authentication

## Examples

### Simple LLM Tracing

```typescript
import { trace } from '@opentelemetry/api';
import { register, ProjectType } from '@traceai/fi-core';

// Initialize
register({
  projectName: 'llm-chat-bot',
  projectType: ProjectType.EXPERIMENT,
  evalTags: [
new EvalTag({
            type: EvalTagType.OBSERVATION_SPAN,
            value: EvalSpanKind.LLM,
            eval_name: EvalName.CHUNK_ATTRIBUTION,
            config: {},
            custom_eval_name: "Chunk_Attribution_5",
            mapping: {
            "context": "raw.input",
            "output": "raw.output"
            },
            model: ModelChoices.TURING_SMALL
        })
  ]
});

// Create traces
const tracer = trace.getTracer('my-app');
const span = tracer.startSpan('llm-completion');

span.setAttributes({
  'llm.model': 'gpt-4o-mini',
  'llm.prompt': 'What is the capital of France?',
  'llm.response': 'The capital of France is Paris.'
});

span.end();
```

## TanStack AI

TanStack AI creates the spans with its own `otelMiddleware`. The `@traceai/fi-core/tanstack` subpath supplies the options for that middleware and an isolated Future AGI provider, so you don't add a second instrumentation:

```ts
import { chat } from "@tanstack/ai";
import { otelMiddleware } from "@tanstack/ai/middlewares/otel";
import { flushTraces, registerTanStackTracing, tanStackOtelOptions } from "@traceai/fi-core/tanstack";

const provider = registerTanStackTracing({ projectName: "my-chatbot" });
const tracer = provider.getTracer("tanstack-ai");

const stream = chat({
  adapter,
  messages,
  threadId, // optional: an opaque conversation id
  middleware: [otelMiddleware(tanStackOtelOptions(tracer, { threadIdAsSession: Boolean(threadId) }))],
});
```

- **Privacy.** Messages, tool arguments and results, error text and stack traces are not exported by default. `captureContent: true` exports content and error text through your `redact` function; a redactor that throws yields `[redaction_failed]`. Stack traces are never exported.
- **Tokens are counted once.** TanStack also writes the sum of every model call's usage onto the chat span. In the Future AGI export only, that sum moves to `tanstack.ai.root_usage.*` when the model-call spans carry usage. Other exporters still receive TanStack's original spans.
- **Sessions.** `threadIdAsSession: true` exports the `threadId` you pass to `chat()` as `session.id`. Use an opaque id, never an email address or user name.
- **Existing OpenTelemetry apps.** `registerTanStackTracing()` never replaces the global provider, and each chat starts a new trace unless you pass `joinActiveTrace: true`. To export through a provider you already own instead, wrap your exporter: `new TransformingSpanExporter(exporter, normalizeTanStackSpan)`.
- **Serverless.** `await flushTraces(provider)` waits at most 2 seconds and never throws. In a long-lived server, call `shutdownTraces(provider)` when the process stops.

To test against packed builds (floor `@tanstack/ai` 0.69.0 with OpenTelemetry SDK 2.0.1, and current 1.0.1 with SDK 2.12.0), prepare a fixture with `node test/install-tanstack-fixture.cjs DIR floor|current CORE_TGZ SEMCONV_TGZ`, then run `TANSTACK_FIXTURE=DIR pnpm test:tanstack`.

## Development

### Building

```bash
pnpm build
```

### Testing

```bash
pnpm test
```

### Linting

```bash
pnpm lint
```

## Contributing

This package is part of the TraceAI project. Please refer to the main repository for contribution guidelines.

## Links

- [GitHub Repository](https://github.com/future-agi/traceAI)
- [Issues](https://github.com/future-agi/traceAI/issues)
- [TraceAI Platform](https://api.futureagi.com)
