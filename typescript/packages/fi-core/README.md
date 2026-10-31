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

### Async spans and shutdown

`register()` installs the global tracer provider and a Node.js async context manager when you do not already have one. Parent spans, session context and tracing suppression then survive `await`. An existing application context manager is reused.

```typescript
import { register, ProjectType } from '@traceai/fi-core';

const provider = register({
  projectName: 'support-assistant',
  projectType: ProjectType.OBSERVE,
  batch: true,
});
const tracer = provider.getTracer('support-assistant');

await tracer.startActiveSpan('request', async parent => {
  try {
    await Promise.resolve();
    tracer.startSpan('prepare-response').end();
  } finally {
    parent.end();
  }
});

await provider.forceFlush(); // Deliver queued spans at a serverless invocation boundary.
await provider.shutdown(); // Call once when the application exits, not after each request.
```

`batch: true` queues spans for the standard OpenTelemetry `BatchSpanProcessor`. The default, `batch: false`, uses `SimpleSpanProcessor`. Both HTTP and gRPC support batching. Shutdown drains pending spans; it does not disable the global context manager that other providers may still use.

### Configure your own processors

Pass `spanProcessors` to `register()` or `new FITracerProvider()` when you want to own exporter configuration. This list **replaces** the default Future AGI exporter; an empty list disables export. Configure every destination you need in the list. Do not attach the same exporter twice.

```typescript
import { register, ProjectType } from '@traceai/fi-core';
import { BatchSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';

const provider = register({
  projectName: 'support-assistant',
  projectType: ProjectType.OBSERVE,
  spanProcessors: [new BatchSpanProcessor(new OTLPTraceExporter({
    url: 'https://api.futureagi.com/tracer/v1/traces',
    headers: {
      'x-api-key': process.env.FI_API_KEY!,
      'x-secret-key': process.env.FI_SECRET_KEY!,
    },
  }))],
});
```

Install the OpenTelemetry packages you import directly in your application. With explicit `spanProcessors`, `batch`, `endpoint`, `headers` and `transport` do not configure those processors; configure each exporter and processor yourself.

The existing `provider.addSpanProcessor(processor)` entry point remains supported. Its first call replaces the implicit Future AGI processor, and later calls append. If you supplied `spanProcessors` at construction, calls only append. Already-queued spans in a replaced processor are still flushed and shut down with the provider. Prefer constructor configuration before creating spans.

### Use an existing OpenTelemetry SDK

Keep one global provider. If your application already owns a `NodeSDK` or `NodeTracerProvider`, configure its processors and exporters there instead of calling `register()` again. Wrap its tracer with `FITracer` if you need TraceAI's span privacy helpers. Do not add another instrumentation layer for a framework that already emits the spans you need.

`register()` never replaces an existing global provider. It warns and returns a separate provider; global `trace.getTracer()` calls still use the application's original provider. Use `setGlobalTracerProvider: false` to intentionally create an isolated provider, obtain tracers from that returned provider and retain your application's context manager. This option does not install or change global context or propagation. Constructing `FITracerProvider` directly also leaves globals untouched.

### Advanced Configuration

```typescript
import { register, ProjectType, EvalTag, EvalTagType, EvalName, EvalSpanKind, ModelChoices } from '@traceai/fi-core';

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
