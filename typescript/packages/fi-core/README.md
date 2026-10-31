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
- Bounded batch span processing by default; explicitly supplied processors remain application-owned

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

## Export privacy and application identity

Every traceAI-owned HTTP/gRPC exporter, including the legacy constructors, applies
the same detached S1 privacy snapshot immediately before serialization. Recognition
comes from the byte-identical shared `privacy.json` catalog. Environment credentials
and FI headers, including ignored halves of incomplete pairs, seed the process-wide
bounded registry. `traceConfig` and `FI_HIDE_*` controls also apply on manual routes.
Application-supplied processors/exporters remain application-owned and are not
silently scrubbed or replaced. Provider defaults use batching; `batch: true` remains
compatible, and `spanProcessors` selects an explicit SDK policy. Manual SDK owners
must await `provider.forceFlush()` or `provider.shutdown()` before explicitly
terminating the process; the auto agent manages its own bounded exit/signal drain.

`register({ projectName })` sets FI `project_name`, not `service.name`. Default OTel
resource identity, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` and an optional
application `resource` are preserved.

`FITracer` applies the shared privacy policy before SDK attribute-value truncation.
Its library spans use the same deterministic count-limit projection as wire spans,
including late output/usage updates. Only canonical metadata enters the overflow
capsule; the export boundary reapplies hiding by each capsule member's original key.
Image hiding (explicit or `FI_HIDE_INPUT_IMAGES`) also implies error-detail hiding
unless `hideErrorDetails`/`FI_HIDE_ERROR_DETAILS` explicitly overrides it.

## Known limitations

- **2026-10-30 (manual credential discovery):** Without the auto agent's transport
  hooks, the registry knows FI/environment credentials, structurally marked
  captured fields and explicit `registerSecret` values. It cannot infer unmarked,
  never-observed credentials or arbitrary application transformations. Use
  `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT` and
  `FI_HIDE_ERROR_DETAILS` or explicitly register sensitive exact values. The shared
  LRU retains at most 1024 values; re-register credentials when reused.
- **2026-10-30 (recognition bounds):** Catalog depth/node/text exhaustion fails closed
  per span, preserving identity/timing and dropping content. Recognition is
  structural, not semantic inference; use the hide controls for stronger protection.
  Raw SDK resource arrays above 4096 pairs fail closed rather than cropping away
  MIME siblings. An `FITracer` library span exhausting its bounded admission view
  remains content-free through end. Opaque custom Span implementations without the
  SDK's writable attribute storage cannot support late attribute replacement; use
  a standard OTel SDK span or configure sufficient attribute capacity.

## Contributing

This package is part of the TraceAI project. Please refer to the main repository for contribution guidelines.

## Links

- [GitHub Repository](https://github.com/future-agi/traceAI)
- [Issues](https://github.com/future-agi/traceAI/issues)
- [TraceAI Platform](https://api.futureagi.com)
