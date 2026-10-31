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

## Message privacy

Wrap your application's existing OpenTelemetry tracer with `FITracer` to mask
message attributes before they reach the sampler or exporter. Keep your existing
SDK and exporter configuration; the wrapper does not create another provider or
duplicate spans.

```typescript
import { trace } from '@opentelemetry/api';
import { FITracer } from '@traceai/fi-core';

// Run after your application's OpenTelemetry SDK has been initialized.
const tracer = new FITracer({
  tracer: trace.getTracer('my-app'),
  traceConfig: {
    hideInputText: true,
    hideOutputText: true,
    hideInputImages: true,
  },
});

const span = tracer.startSpan('chat', {
  attributes: {
    'gen_ai.request.model': 'my-model',
    'gen_ai.input.messages': JSON.stringify([
      { role: 'user', parts: [{ type: 'text', content: 'Explain distributed tracing.' }] },
    ]),
  },
});
span.end();
```

The message retains its `role` and part `type`, but the text `content` becomes
`__REDACTED__`. The same settings apply to message attributes passed to
`startActiveSpan`, set later on an `FISpan`, or attached to span links.

- `hideInputText` / `hideOutputText` support legacy `content` messages and native
  [OpenTelemetry GenAI `parts` messages](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/).
  They redact payloads, including tool arguments/results, reasoning, compaction,
  opaque media and extension fields. Scalar role, type, ID, name, finish reason,
  modality and MIME metadata can remain. Legacy `content` arrays are redacted as
  a whole, as are malformed message blobs.
- `hideInputImages` removes image parts, including native `blob`, `uri` and `file`
  parts identified by image modality or MIME type. Known text and explicitly
  non-image media remain. Unknown parts, unclassified media and opaque tool
  payloads are redacted conservatively. Combining this flag with text hiding
  applies both restrictions; `piiRedaction` still applies to retained text.
- `hideInputs` / `hideOutputs`, or `hideInputMessages` / `hideOutputMessages`,
  remove the corresponding messages attribute entirely. Prefer these broader
  settings when you do not need message metadata.

You can also set `FI_HIDE_INPUT_TEXT`, `FI_HIDE_OUTPUT_TEXT`,
`FI_HIDE_INPUT_IMAGES`, `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`,
`FI_HIDE_INPUT_MESSAGES` and `FI_HIDE_OUTPUT_MESSAGES` to `true` before creating
an `FITracer`. Explicit `traceConfig` options override environment settings.

**Privacy boundary:** these controls protect attributes processed by `FITracer`
and `FISpan`, not every span exported by the SDK. A framework emitting through
its own raw OpenTelemetry tracer bypasses this wrapper, even if you registered
an FI exporter. Configure that framework's content capture or use a trusted
collector policy before sending its native telemetry. Text flags target the
message attributes, not arbitrary copies in `input.value`, `raw.input`, event
payloads, span names or other custom fields. Use the broader flags for supported
raw input/output attributes and avoid recording sensitive content in custom
fields. Exporting or normalizing native GenAI attributes alone is not redaction.

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
