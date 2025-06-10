# @traceai/fi-core

Core OpenTelemetry instrumentation library for TypeScript applications with advanced evaluation capabilities for AI systems.

## Overview

`@traceai/fi-core` provides a comprehensive tracing solution built on OpenTelemetry that's specifically designed for AI applications. It offers custom span exporters, evaluation tags, and seamless integration with the TraceAI platform for observability and performance monitoring.

## Features

- **OpenTelemetry Integration**: Built on top of OpenTelemetry APIs with custom implementations
- **Custom Span Exporter**: HTTP-based span exporter with configurable endpoints
- **AI Evaluation Tags**: Comprehensive evaluation system for AI applications with 50+ built-in evaluators
- **Project Management**: Support for project versioning, sessions, and metadata
- **UUID-based Trace IDs**: Custom ID generation for better trace identification
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

## Quick Start

### Basic Setup

```typescript
import { register } from '@traceai/fi-core';

// Initialize tracing with minimal configuration
const tracerProvider = register({
  projectName: 'my-ai-project',
  projectType: 'experiment',
  verbose: true
});
```

### Advanced Configuration

```typescript
import { register, ProjectType, EvalTag, EvalName, EvalSpanKind } from '@traceai/fi-core';

// Create evaluation tags for AI model monitoring
const evalTags = [
  await EvalTag.create({
    type: 'OBSERVATION_SPAN_TYPE',
    value: EvalSpanKind.LLM,
    eval_name: EvalName.CONTEXT_ADHERENCE,
    custom_eval_name: 'custom_context_check',
    config: {
      criteria: 'Check if the response adheres to the given context'
    }
  })
];

// Register with comprehensive configuration
const tracerProvider = register({
  projectName: 'advanced-ai-project',
  projectType: ProjectType.EXPERIMENT,
  projectVersionName: 'v1.0.0',
  evalTags: evalTags,
  sessionName: 'experiment-session-1',
  metadata: {
    model: 'gpt-4',
    temperature: 0.7
  },
  batch: true,
  verbose: true,
  endpoint: 'https://your-custom-endpoint.com'
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

### Register Options

```typescript
interface RegisterOptions {
  projectName?: string;
  projectType?: ProjectType;
  projectVersionName?: string;
  evalTags?: EvalTag[];
  sessionName?: string;
  metadata?: Record<string, any>;
  batch?: boolean;
  setGlobalTracerProvider?: boolean;
  headers?: Record<string, string>;
  verbose?: boolean;
  endpoint?: string;
  idGenerator?: IdGenerator;
}
```

### Environment Variables

- `FI_COLLECTOR_BASE_URL`: Base URL for the TraceAI collector
- `FI_AUTH_HEADER`: Authentication header for API requests
- `FI_VERBOSE`: Enable verbose logging

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

## License

See the main TraceAI repository for license information.

## Links

- [GitHub Repository](https://github.com/future-agi/traceAI)
- [Issues](https://github.com/future-agi/traceAI/issues)
- [TraceAI Platform](https://api.futureagi.com)
