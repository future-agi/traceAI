# @traceai/fi-core

The core TypeScript library for traceAI instrumentation.

## Installation

```bash
npm install @traceai/fi-core
# or
pnpm add @traceai/fi-core
# or
yarn add @traceai/fi-core
```

## Core Exports

```typescript
import {
    // Main function
    register,

    // Types
    ProjectType,
    Transport,

    // Evaluation
    EvalTag,
    EvalTagType,
    EvalSpanKind,
    EvalName,
    ModelChoices,

    // Classes
    FITracerProvider,
    HTTPSpanExporter,
    GRPCSpanExporter,
    SimpleSpanProcessor,
    BatchSpanProcessor,
} from "@traceai/fi-core";
```

## register()

The main entry point for setting up tracing:

```typescript
const tracerProvider = register({
    projectName: "my_app",
    projectType: ProjectType.OBSERVE,
});
```

### Options

```typescript
interface RegisterOptions {
    // Required
    projectName?: string;              // Or FI_PROJECT_NAME env var

    // Project configuration
    projectType?: ProjectType;         // EXPERIMENT (default) or OBSERVE
    projectVersionName?: string;       // Version (EXPERIMENT only)
    evalTags?: EvalTag[];             // Evaluations (EXPERIMENT only)
    sessionName?: string;             // Session name (OBSERVE only)
    metadata?: Record<string, any>;   // Custom metadata

    // Processing
    batch?: boolean;                  // Use batch processor (default: false)
    setGlobalTracerProvider?: boolean; // Set as global (default: true)

    // Connection
    headers?: FIHeaders;              // Custom auth headers
    endpoint?: string;                // Custom collector endpoint
    transport?: Transport;            // HTTP (default) or GRPC

    // Debug
    verbose?: boolean;                // Debug logging (default: false)

    // Advanced
    idGenerator?: IdGenerator;        // Custom ID generator
}
```

### Example Configurations

**Basic Observation**:
```typescript
const provider = register({
    projectName: "my_app",
    projectType: ProjectType.OBSERVE,
    sessionName: "user-session-123",
});
```

**Experiment with Evaluations**:
```typescript
const provider = register({
    projectName: "my_experiment",
    projectType: ProjectType.EXPERIMENT,
    projectVersionName: "v1.0.0",
    evalTags: [/* ... */],
    verbose: true,
});
```

**Custom Endpoint**:
```typescript
const provider = register({
    projectName: "my_app",
    endpoint: "https://custom-collector.com/v1/traces",
    headers: {
        "x-api-key": "custom-key",
        "x-secret-key": "custom-secret",
    },
});
```

## ProjectType

```typescript
enum ProjectType {
    EXPERIMENT = "experiment",  // For testing with evaluations
    OBSERVE = "observe",        // For production monitoring
}
```

| Type | Use Case | Supports |
|------|----------|----------|
| `EXPERIMENT` | Development, testing | evalTags, projectVersionName |
| `OBSERVE` | Production | sessionName |

## EvalTag

Define automated evaluations:

```typescript
const evalTag = new EvalTag({
    type: EvalTagType.OBSERVATION_SPAN,
    value: EvalSpanKind.LLM,
    eval_name: EvalName.TOXICITY,
    custom_eval_name: "my_toxicity_check",
    mapping: { output: "raw.output" },
    model: ModelChoices.TURING_SMALL,
    config: {},  // Optional evaluator config
});
```

### EvalTagType

```typescript
enum EvalTagType {
    OBSERVATION_SPAN = "OBSERVATION_SPAN_TYPE"
}
```

### EvalSpanKind

```typescript
enum EvalSpanKind {
    TOOL = "TOOL",
    LLM = "LLM",
    RETRIEVER = "RETRIEVER",
    EMBEDDING = "EMBEDDING",
    AGENT = "AGENT",
    RERANKER = "RERANKER",
}
```

### ModelChoices

```typescript
enum ModelChoices {
    TURING_LARGE = "TURING_LARGE",    // Best accuracy
    TURING_SMALL = "TURING_SMALL",    // Balanced
    TURING_FLASH = "TURING_FLASH",    // Fastest
    PROTECT = "PROTECT",               // Safety evals
    PROTECT_FLASH = "PROTECT_FLASH",  // Fast safety
}
```

### EvalName

60+ built-in evaluators. Common ones:

```typescript
enum EvalName {
    // Quality
    CONTEXT_ADHERENCE = "Context Adherence",
    COMPLETENESS = "Completeness",
    GROUNDEDNESS = "Groundedness",

    // Safety
    TOXICITY = "Toxicity",
    PII = "PII",
    CONTENT_MODERATION = "Content Moderation",
    PROMPT_INJECTION = "Prompt Injection",

    // Accuracy
    FACTUAL_ACCURACY = "Factual Accuracy",
    DETECT_HALLUCINATION = "Detect Hallucination",

    // Format
    IS_JSON = "Is Json",
    IS_CODE = "Is Code",

    // Similarity
    BLEU_SCORE = "Bleu Score",
    EMBEDDING_SIMILARITY = "Embedding Similarity",
}
```

## Transport

```typescript
enum Transport {
    HTTP = "http",   // Default, uses HTTPSpanExporter
    GRPC = "grpc",   // Uses GRPCSpanExporter
}
```

## FITracerProvider

Extended OpenTelemetry TracerProvider:

```typescript
const provider = new FITracerProvider({
    endpoint: "https://api.futureagi.com/...",
    headers: { "x-api-key": "..." },
    verbose: true,
});

// Add span processor
provider.addSpanProcessor(new BatchSpanProcessor(exporter));

// Get tracer
const tracer = provider.getTracer("my-app", "1.0.0");

// Shutdown when done
await provider.shutdown();
```

## Span Exporters

### HTTPSpanExporter

```typescript
const exporter = new HTTPSpanExporter({
    endpoint: "https://api.futureagi.com/...",
    headers: {
        "x-api-key": process.env.FI_API_KEY!,
        "x-secret-key": process.env.FI_SECRET_KEY!,
    },
    verbose: true,
});
```

### GRPCSpanExporter

```typescript
const exporter = new GRPCSpanExporter({
    endpoint: "https://grpc.futureagi.com",
    headers: { /* ... */ },
    verbose: true,
});
```

## Span Processors

### SimpleSpanProcessor

Exports spans immediately:

```typescript
import { SimpleSpanProcessor } from "@traceai/fi-core";

provider.addSpanProcessor(new SimpleSpanProcessor(exporter));
```

### BatchSpanProcessor

Batches spans before export:

```typescript
import { BatchSpanProcessor } from "@traceai/fi-core";

provider.addSpanProcessor(new BatchSpanProcessor(exporter, {
    maxQueueSize: 2048,
    maxExportBatchSize: 512,
    scheduledDelayMillis: 5000,
}));
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `FI_API_KEY` | API key |
| `FI_SECRET_KEY` | Secret key |
| `FI_PROJECT_NAME` | Default project name |
| `FI_BASE_URL` | HTTP collector URL |
| `FI_GRPC_URL` | gRPC collector URL |
| `FI_VERBOSE_EXPORTER` | Enable exporter logging |
| `FI_VERBOSE_PROVIDER` | Enable provider logging |

## Usage with Instrumentations

```typescript
import { register, ProjectType } from "@traceai/fi-core";
import { OpenAIInstrumentation } from "@traceai/openai";
import { registerInstrumentations } from "@opentelemetry/instrumentation";

// 1. Register provider first
const provider = register({
    projectName: "my_app",
    projectType: ProjectType.OBSERVE,
});

// 2. Register instrumentations
registerInstrumentations({
    tracerProvider: provider,
    instrumentations: [new OpenAIInstrumentation()],
});

// 3. Use your frameworks
import OpenAI from "openai";
const openai = new OpenAI();
```

## TraceConfig

Configure data capture per instrumentation:

```typescript
import { OpenAIInstrumentation } from "@traceai/openai";

const instrumentation = new OpenAIInstrumentation({
    traceConfig: {
        hideInputs: true,
        hideOutputs: true,
        hideInputMessages: false,
        hideOutputMessages: false,
        hideInputImages: true,
        hideInputText: false,
        hideOutputText: false,
        hideEmbeddingVectors: true,
        base64ImageMaxLength: 16000,
    }
});
```

## Error Handling

```typescript
try {
    const provider = register({
        projectName: "my_app",
        projectType: ProjectType.EXPERIMENT,
        evalTags: evalTags,
    });
} catch (error) {
    // Validation errors:
    // - "Duplicate custom_eval_name"
    // - "Cannot use evalTags with OBSERVE"
    // - "Cannot use sessionName with EXPERIMENT"
    console.error("Registration failed:", error);
}
```

## Graceful Shutdown

Always shutdown the provider:

```typescript
// In your main function
const provider = register({ ... });

try {
    // App logic
    await runApp();
} finally {
    await provider.shutdown();
}

// Or handle process signals
process.on("SIGTERM", async () => {
    await provider.shutdown();
    process.exit(0);
});
```

## Related

- [TypeScript Quickstart](../getting-started/quickstart-typescript.md)
- [Instrumentations](instrumentations.md)
- [Evaluation Tags](../configuration/eval-tags.md)
