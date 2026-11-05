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

## GenAI Attributes

Use the `GEN_AI_*` constants for GenAI attributes. Their names and values match
Python's `SpanAttributes`, and they are available as both named exports and
properties of `SemanticConventions`.

```typescript
import {
    GEN_AI_INPUT_MESSAGES,
    SemanticConventions,
} from '@traceai/fi-semantic-conventions';

// Both are "gen_ai.input.messages".
GEN_AI_INPUT_MESSAGES;
SemanticConventions.GEN_AI_INPUT_MESSAGES;
```

The old `LLM_*` constants remain available as deprecated aliases with the same
literal types and wire values. Existing instrumentation does not need to change.
In particular, both `LLM_PROVIDER` and `LLM_SYSTEM` alias `GEN_AI_PROVIDER_NAME`;
`LLM_SYSTEM` is not an alias for Python's distinct `GEN_AI_SYSTEM` (`gen_ai.system`).

| Canonical constant | Deprecated alias | Attribute key |
|--------------------|------------------|---------------|
| `GEN_AI_INPUT_MESSAGES` | `LLM_INPUT_MESSAGES` | `gen_ai.input.messages` |
| `GEN_AI_PROMPTS` | `LLM_PROMPTS` | `gen_ai.prompts` |
| `GEN_AI_REQUEST_PARAMETERS` | `LLM_INVOCATION_PARAMETERS` | `gen_ai.request.parameters` |
| `GEN_AI_OUTPUT_MESSAGES` | `LLM_OUTPUT_MESSAGES` | `gen_ai.output.messages` |
| `GEN_AI_REQUEST_MODEL` | `LLM_MODEL_NAME` | `gen_ai.request.model` |
| `GEN_AI_PROVIDER_NAME` | `LLM_PROVIDER` | `gen_ai.provider.name` |
| `GEN_AI_PROVIDER_NAME` | `LLM_SYSTEM` | `gen_ai.provider.name` |
| `GEN_AI_USAGE_OUTPUT_TOKENS` | `LLM_TOKEN_COUNT_COMPLETION` | `gen_ai.usage.output_tokens` |
| `GEN_AI_USAGE_OUTPUT_TOKENS_REASONING` | `LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING` | `gen_ai.usage.output_tokens.reasoning` |
| `GEN_AI_USAGE_OUTPUT_TOKENS_AUDIO` | `LLM_TOKEN_COUNT_COMPLETION_DETAILS_AUDIO` | `gen_ai.usage.output_tokens.audio` |
| `GEN_AI_USAGE_INPUT_TOKENS` | `LLM_TOKEN_COUNT_PROMPT` | `gen_ai.usage.input_tokens` |
| `GEN_AI_USAGE_CACHE_WRITE_TOKENS` | `LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE` | `gen_ai.usage.cache_write_tokens` |
| `GEN_AI_USAGE_CACHE_READ_TOKENS` | `LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ` | `gen_ai.usage.cache_read_tokens` |
| `GEN_AI_USAGE_INPUT_TOKENS_AUDIO` | `LLM_TOKEN_COUNT_PROMPT_DETAILS_AUDIO` | `gen_ai.usage.input_tokens.audio` |
| `GEN_AI_USAGE_TOTAL_TOKENS` | `LLM_TOKEN_COUNT_TOTAL` | `gen_ai.usage.total_tokens` |
| `GEN_AI_TOOL_CALL` | `LLM_FUNCTION_CALL` | `gen_ai.tool.call` |
| `GEN_AI_TOOL_DEFINITIONS` | `LLM_TOOLS` | `gen_ai.tool.definitions` |

These names include traceAI extensions such as prompts, request parameters,
aggregated tool calls, and detailed token usage; they are not all upstream OTEL
standard attributes.

`SemanticAttributePrefixes.llm` and `LLMAttributePostfixes` are retained for
compatibility with legacy `llm.*` keys. Do not compose new GenAI keys from them;
use the complete constants above. Prompt template attributes below are unchanged.

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
import { SemanticConventions } from '@traceai/fi-semantic-conventions';

const tracer = trace.getTracer('my-app');

// Create a span with semantic attributes
const span = tracer.startSpan('llm.chat');

// Set attributes using conventions
span.setAttributes({
    [SemanticConventions.GEN_AI_PROVIDER_NAME]: 'openai',
    [SemanticConventions.GEN_AI_REQUEST_MODEL]: 'gpt-4',
    [SemanticConventions.GEN_AI_USAGE_INPUT_TOKENS]: 100,
    [SemanticConventions.GEN_AI_USAGE_OUTPUT_TOKENS]: 50,
    [SemanticConventions.INPUT_VALUE]: 'User input text',
    [SemanticConventions.OUTPUT_VALUE]: 'Model response',
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
