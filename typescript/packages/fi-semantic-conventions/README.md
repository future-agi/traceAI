# @traceai/fi-semantic-conventions

Semantic conventions for OpenTelemetry instrumentation attributes used in TraceAI prototype and observe projects.

## Installation

```bash
npm install @traceai/fi-semantic-conventions
# or
yarn add @traceai/fi-semantic-conventions
# or
pnpm add @traceai/fi-semantic-conventions
```

## Overview

This package provides standardized attribute names for instrumenting AI/LLM applications with OpenTelemetry. Using consistent semantic conventions ensures traces are correctly parsed and displayed in the TraceAI platform.

## Module System Support

This package supports both **CommonJS** and **ESM** module systems.

### ESM (ES Modules)
```typescript
import {
    SemanticAttributePrefixes,
    LLMAttributePostfixes,
    EmbeddingAttributePostfixes,
} from '@traceai/fi-semantic-conventions';
```

### CommonJS
```typescript
const {
    SemanticAttributePrefixes,
    LLMAttributePostfixes,
} = require('@traceai/fi-semantic-conventions');
```

## Attribute Prefixes

```typescript
import { SemanticAttributePrefixes } from '@traceai/fi-semantic-conventions';
```

| Prefix | Usage |
|--------|-------|
| `input` | Input data attributes |
| `output` | Output data attributes |
| `llm` | LLM-specific attributes |
| `retrieval` | Retrieval/RAG attributes |
| `reranker` | Reranking attributes |
| `messages` | Message list attributes |
| `message` | Single message attributes |
| `document` | Document attributes |
| `embedding` | Embedding attributes |
| `tool` | Tool/function attributes |
| `tool_call` | Tool call attributes |
| `metadata` | Custom metadata |
| `tag` | Tag attributes |
| `session` | Session tracking |
| `user` | User tracking |
| `traceai` | TraceAI-specific |
| `fi` | Future AGI namespace |
| `message_content` | Message content |
| `image` | Image attributes |
| `audio` | Audio attributes |
| `prompt` | Prompt attributes |

## LLM Attributes

```typescript
import { LLMAttributePostfixes } from '@traceai/fi-semantic-conventions';
```

| Postfix | Full Attribute | Description |
|---------|----------------|-------------|
| `provider` | `llm.provider` | LLM provider (aws, azure, google) |
| `system` | `llm.system` | LLM system (openai, anthropic) |
| `model_name` | `llm.model_name` | Model identifier |
| `token_count` | `llm.token_count.*` | Token usage |
| `input_messages` | `llm.input_messages` | Input message array |
| `output_messages` | `llm.output_messages` | Output message array |
| `invocation_parameters` | `llm.invocation_parameters` | Model parameters |
| `prompts` | `llm.prompts` | Prompt strings |
| `prompt_template` | `llm.prompt_template.*` | Template tracking |
| `function_call` | `llm.function_call` | Function call details |
| `tools` | `llm.tools` | Available tools |

### Token Count Attributes

| Attribute | Description |
|-----------|-------------|
| `llm.token_count.prompt` | Input/prompt tokens |
| `llm.token_count.completion` | Output/completion tokens |
| `llm.token_count.total` | Total tokens |
| `llm.token_count.cache_read` | Cached tokens read |
| `llm.token_count.cache_creation` | Cached tokens created |

### Prompt Template Attributes

| Attribute | Description |
|-----------|-------------|
| `llm.prompt_template.template` | Template string |
| `llm.prompt_template.version` | Template version |
| `llm.prompt_template.variables` | Template variables |

## Message Attributes

```typescript
import { MessageAttributePostfixes } from '@traceai/fi-semantic-conventions';
```

| Postfix | Full Attribute | Description |
|---------|----------------|-------------|
| `role` | `message.role` | Message role (user, assistant, system) |
| `content` | `message.content` | Message content |
| `contents` | `message.contents` | Multiple content blocks |
| `name` | `message.name` | Participant name |
| `function_call_name` | `message.function_call.name` | Function name |
| `function_call_arguments` | `message.function_call.arguments` | Function args |
| `tool_calls` | `message.tool_calls` | Tool call array |
| `tool_call_id` | `message.tool_call_id` | Tool call identifier |

## Embedding Attributes

```typescript
import { EmbeddingAttributePostfixes } from '@traceai/fi-semantic-conventions';
```

| Postfix | Full Attribute | Description |
|---------|----------------|-------------|
| `embeddings` | `embedding.embeddings` | Vector array |
| `text` | `embedding.text` | Input text |
| `model_name` | `embedding.model_name` | Embedding model |
| `vector` | `embedding.vector` | Single vector |

## Retrieval Attributes

```typescript
import { RetrievalAttributePostfixes } from '@traceai/fi-semantic-conventions';
```

| Postfix | Full Attribute | Description |
|---------|----------------|-------------|
| `documents` | `retrieval.documents` | Retrieved documents |

## Tool Attributes

```typescript
import { ToolAttributePostfixes } from '@traceai/fi-semantic-conventions';
```

| Postfix | Full Attribute | Description |
|---------|----------------|-------------|
| `name` | `tool.name` | Tool name |
| `description` | `tool.description` | Tool description |
| `parameters` | `tool.parameters` | Tool parameters |
| `json_schema` | `tool.json_schema` | JSON schema |

## Usage Example

```typescript
import { trace } from '@opentelemetry/api';
import {
    SemanticAttributePrefixes,
    LLMAttributePostfixes,
} from '@traceai/fi-semantic-conventions';

const tracer = trace.getTracer('my-app');

// Create a span with semantic attributes
const span = tracer.startSpan('llm.chat');

// Set attributes using conventions
span.setAttributes({
    [`${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.system}`]: 'openai',
    [`${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.model_name}`]: 'gpt-4',
    [`${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.token_count}.prompt`]: 100,
    [`${SemanticAttributePrefixes.llm}.${LLMAttributePostfixes.token_count}.completion`]: 50,
    [`${SemanticAttributePrefixes.input}.value`]: 'User input text',
    [`${SemanticAttributePrefixes.output}.value`]: 'Model response',
});

span.end();
```

## Span Kind Attribute

The `fi.span_kind` attribute indicates the type of operation:

```typescript
// Set span kind
span.setAttribute('fi.span_kind', 'LLM');
```

| Value | Description |
|-------|-------------|
| `LLM` | Language model call |
| `AGENT` | Agent orchestration |
| `TOOL` | Tool/function execution |
| `CHAIN` | Workflow chain |
| `RETRIEVER` | Document retrieval |
| `EMBEDDING` | Embedding generation |
| `RERANKER` | Result reranking |
| `GUARDRAIL` | Safety check |
| `VECTOR_DB` | Vector database operation |

## Resource Attributes

```typescript
import { ResourceAttributes } from '@traceai/fi-semantic-conventions';
```

Standard OpenTelemetry resource attributes for service identification:

| Attribute | Description |
|-----------|-------------|
| `service.name` | Service name |
| `service.version` | Service version |
| `deployment.environment` | Deployment environment |

## TypeScript Configuration

For optimal compatibility:

```json
{
  "compilerOptions": {
    "moduleResolution": "node",
    "esModuleInterop": true,
    "allowSyntheticDefaultImports": true
  }
}
```

## Related Packages

- `@traceai/fi-core` - Core tracing library
- `@traceai/openai` - OpenAI instrumentation
- `@traceai/anthropic` - Anthropic instrumentation
- `@traceai/langchain` - LangChain instrumentation

## License

GPL-3.0
