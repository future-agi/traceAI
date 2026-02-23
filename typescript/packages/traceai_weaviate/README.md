# @traceai/weaviate

OpenTelemetry instrumentation for [Weaviate](https://weaviate.io/) vector database client in Node.js/TypeScript applications.

## Installation

```bash
npm install @traceai/weaviate
# or
pnpm add @traceai/weaviate
# or
yarn add @traceai/weaviate
```

## Prerequisites

- Node.js >= 18
- Weaviate client (`weaviate-client` >= 3.0.0)
- OpenTelemetry SDK configured in your application

## Quick Start

```typescript
import { WeaviateInstrumentation } from "@traceai/weaviate";
import weaviate from "weaviate-client";

// Initialize instrumentation
const instrumentation = new WeaviateInstrumentation({
  traceConfig: {
    maskInputs: false,
    maskOutputs: false,
  },
});

// Enable instrumentation
instrumentation.enable();

// Manually instrument the weaviate module
instrumentation.manuallyInstrument(weaviate);

// Now all Weaviate operations will be traced
const client = await weaviate.connectToLocal();
const collection = client.collections.get("Articles");

// This query will be automatically traced
const results = await collection.query.nearText("machine learning", {
  limit: 10,
});
```

## Configuration Options

```typescript
interface WeaviateInstrumentationConfig {
  // Enable/disable the instrumentation
  enabled?: boolean;

  // Capture query vectors in span attributes
  captureQueryVectors?: boolean;

  // Capture result vectors in span attributes
  captureResultVectors?: boolean;

  // Capture document content
  captureDocuments?: boolean;
}

interface TraceConfigOptions {
  // Mask sensitive input data
  maskInputs?: boolean;

  // Mask sensitive output data
  maskOutputs?: boolean;
}
```

## Traced Operations

The instrumentation automatically traces the following Weaviate operations:

### Query Operations
- `nearVector` - Vector similarity search
- `nearText` - Text-based semantic search
- `hybrid` - Hybrid search (vector + keyword)
- `bm25` - BM25 keyword search
- `fetchObjects` - Fetch objects by criteria

### Data Operations
- `insert` - Insert single object
- `insertMany` - Batch insert objects
- `deleteById` - Delete object by ID
- `deleteMany` - Batch delete objects
- `update` - Update object properties

### Aggregation
- `aggregate` - Aggregation queries

## Span Attributes

Each traced operation includes relevant attributes:

| Attribute | Description |
|-----------|-------------|
| `db.system` | Always "weaviate" |
| `db.operation` | Operation name (e.g., "nearText", "insert") |
| `db.collection.name` | Collection name |
| `db.weaviate.limit` | Query limit |
| `db.weaviate.offset` | Query offset |
| `db.weaviate.vector_dimensions` | Vector dimensions (when applicable) |

## Real-World Use Cases

### 1. RAG (Retrieval Augmented Generation) Pipeline

```typescript
import { WeaviateInstrumentation } from "@traceai/weaviate";
import weaviate from "weaviate-client";
import OpenAI from "openai";

const instrumentation = new WeaviateInstrumentation();
instrumentation.enable();
instrumentation.manuallyInstrument(weaviate);

const client = await weaviate.connectToLocal();
const openai = new OpenAI();

async function ragQuery(userQuestion: string) {
  // Step 1: Semantic search for relevant context (traced)
  const articles = client.collections.get("Articles");
  const searchResults = await articles.query.nearText(userQuestion, {
    limit: 5,
    returnProperties: ["title", "content", "source"],
  });

  // Step 2: Build context from results
  const context = searchResults.objects
    .map((obj) => `${obj.properties.title}: ${obj.properties.content}`)
    .join("\n\n");

  // Step 3: Generate response with context
  const response = await openai.chat.completions.create({
    model: "gpt-4",
    messages: [
      {
        role: "system",
        content: `Answer based on this context:\n${context}`,
      },
      { role: "user", content: userQuestion },
    ],
  });

  return response.choices[0].message.content;
}
```

### 2. E-commerce Product Search

```typescript
async function searchProducts(query: string, filters?: ProductFilters) {
  const products = client.collections.get("Products");

  // Hybrid search combining semantic + keyword matching
  const results = await products.query.hybrid(query, {
    limit: 20,
    alpha: 0.7, // 70% vector, 30% keyword
    returnProperties: ["name", "description", "price", "category", "imageUrl"],
    where: filters?.category
      ? {
          path: ["category"],
          operator: "Equal",
          valueText: filters.category,
        }
      : undefined,
  });

  return results.objects.map((obj) => ({
    id: obj.uuid,
    ...obj.properties,
    score: obj.metadata?.score,
  }));
}
```

### 3. Document Similarity & Deduplication

```typescript
async function findDuplicates(documentVector: number[], threshold = 0.95) {
  const documents = client.collections.get("Documents");

  // Find highly similar documents
  const results = await documents.query.nearVector(documentVector, {
    limit: 10,
    certainty: threshold,
    returnProperties: ["title", "hash", "createdAt"],
  });

  return results.objects.filter(
    (obj) => obj.metadata?.certainty && obj.metadata.certainty >= threshold
  );
}
```

### 4. Multi-tenant Knowledge Base

```typescript
async function searchKnowledgeBase(
  tenantId: string,
  query: string,
  options?: SearchOptions
) {
  const knowledge = client.collections.get("KnowledgeBase");

  // BM25 search with tenant isolation
  const results = await knowledge.query.bm25(query, {
    limit: options?.limit ?? 10,
    returnProperties: ["title", "content", "tags", "updatedAt"],
    where: {
      path: ["tenantId"],
      operator: "Equal",
      valueText: tenantId,
    },
  });

  return results;
}
```

### 5. Real-time Content Recommendation

```typescript
async function getRecommendations(userId: string, limit = 10) {
  const users = client.collections.get("Users");
  const content = client.collections.get("Content");

  // Get user's preference vector
  const user = await users.query.fetchObjectById(userId, {
    returnProperties: ["preferenceVector"],
  });

  if (!user?.properties.preferenceVector) {
    return [];
  }

  // Find similar content
  const recommendations = await content.query.nearVector(
    user.properties.preferenceVector as number[],
    {
      limit,
      returnProperties: ["title", "type", "thumbnailUrl", "duration"],
      where: {
        path: ["status"],
        operator: "Equal",
        valueText: "published",
      },
    }
  );

  return recommendations.objects;
}
```

## Integration with OpenTelemetry

```typescript
import { NodeSDK } from "@opentelemetry/sdk-node";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { WeaviateInstrumentation } from "@traceai/weaviate";

const sdk = new NodeSDK({
  traceExporter: new OTLPTraceExporter({
    url: "http://localhost:4318/v1/traces",
  }),
  instrumentations: [new WeaviateInstrumentation()],
});

sdk.start();
```

## Error Handling

The instrumentation automatically captures errors and exceptions:

```typescript
try {
  await collection.query.nearText("search query", { limit: 10 });
} catch (error) {
  // Error is automatically recorded in the span with:
  // - exception.type
  // - exception.message
  // - exception.stacktrace
  // - span status set to ERROR
}
```

## License

Apache-2.0
