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

### AWS Lambda invocation lifecycle

Wrap your exported handler with `traceLambdaHandler(handler, { provider })`. Create the provider once outside the handler, with a `BatchSpanProcessor` and a finite exporter timeout. Reuse an existing application-owned provider rather than registering a second SDK. The wrapper never installs a provider, changes global propagation or calls `shutdown()`.

```typescript
import { traceLambdaHandler } from '@traceai/fi-core';

// provider is your application's configured, reusable OpenTelemetry provider.
export const handler = traceLambdaHandler(async (event, context) => {
  return { statusCode: 200, body: 'Order accepted' };
}, { provider, maxFlushWaitMillis: 2000, remainingTimeReserveMillis: 250 });
```

The application must install an async context manager; `register()` does so when it successfully registers the global provider. Model and tool instrumentors using that provider then attach their spans to `aws.lambda.invoke`. The invocation is a SERVER span classified as `CHAIN`, with request ID, function name and a first-invocation cold-start flag.

The wrapper ends the invocation span before attempting `forceFlush()`. It waits at most the smaller of `maxFlushWaitMillis` (default 2000) and the remaining invocation time minus `remainingTimeReserveMillis` (default 250). Exhausted or invalid budgets skip flushing. A timed-out wait does **not** cancel export. One pending flush is shared across wrappers using the same provider; additional invocations do not start more flushes until it settles. New spans may remain queued for a later flush. Hard timeouts, process termination and synchronously blocking processors/providers cannot be made safe by an asynchronous timer.

Only W3C `traceparent` is extracted, from API Gateway v1/v2, function URL and ALB HTTP events. Names are case-insensitive and multi-value headers must identify a single parent. Conflicting or malformed parents, queue batches and direct invocations start independent traces. Baggage, tracestate and ambient X-Ray parents are not imported. Queue invocations have an invocation span, not per-record links or propagation.

Invocation data is metadata-only: the wrapper does not copy event bodies, results, URLs, query strings, arbitrary headers or exception messages/stacks. Error metadata is restricted to built-in error categories. Root error details remain hidden even with `FI_HIDE_ERROR_DETAILS=false`; there is no content-capture opt-in. Child instrumentors and application-added attributes have their own privacy policies. `provider: undefined` disables invocation tracing; validate both FI credentials and project configuration before constructing an exporter so partial authentication is never sent.

Use this wrapper **instead of** upstream `AwsLambdaInstrumentation`, an auto-instrumenting ADOT layer or another invocation wrapper. Wrapping the same returned handler again is a no-op. Response-streaming handlers are not instrumented and retain their original function identity/stream markers. Promise handlers work on Node 22/24/26, including async functions annotated with AWS types such as `APIGatewayProxyHandler`. The default completion mode observes the returned value or promise, not the callback; select `completion: 'callback'` for a callback implementation even if it has the same AWS type.

For Node 22 callback handlers, you must set `context.callbackWaitsForEmptyEventLoop = false` **before calling the traced handler** and keep it false until completion. This lets Lambda return without waiting for pending exporter sockets after the flush budget expires. It also stops Lambda waiting for unrelated background work, so finish all required application work before calling back. The wrapper never changes this application-owned flag. If it is true or unset on entry, the wrapper calls the original handler without invocation tracing or flushing.

```typescript
import type { APIGatewayProxyHandler } from 'aws-lambda';

const callbackHandler: APIGatewayProxyHandler = (event, context, callback) => {
  callback(null, { statusCode: 200, body: 'Order accepted' });
};
const tracedHandler = traceLambdaHandler(callbackHandler, { provider, completion: 'callback' });

export const handler: APIGatewayProxyHandler = (event, context, callback) => {
  context.callbackWaitsForEmptyEventLoop = false;
  tracedHandler(event, context, callback);
};
```

Use one completion style per handler. With callback tracing enabled, the wrapper takes the first callback result or synchronous error, ends the span, attempts flushing, then calls the runtime callback once. Use promise handlers on Node 24 and later. Do not use legacy `context.done`, `context.succeed` or `context.fail` completion methods. Enable the OpenTelemetry diagnostic logger at DEBUG to see static telemetry-failure messages; the wrapper never logs caught exception contents.

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
