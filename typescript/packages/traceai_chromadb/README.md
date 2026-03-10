# @traceai/chromadb

OpenTelemetry instrumentation for [ChromaDB](https://www.trychroma.com/) - the AI-native open-source vector database.

## Overview

This package provides automatic tracing for ChromaDB operations in Node.js applications, enabling full observability of your vector database interactions in RAG pipelines, semantic search, and AI applications.

## Installation

```bash
npm install @traceai/chromadb
# or
yarn add @traceai/chromadb
# or
pnpm add @traceai/chromadb
```

## Quick Start

### Basic Setup

```typescript
import { ChromaDBInstrumentation } from "@traceai/chromadb";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { registerInstrumentations } from "@opentelemetry/instrumentation";

// Set up OpenTelemetry
const provider = new NodeTracerProvider();
provider.addSpanProcessor(
  new SimpleSpanProcessor(
    new OTLPTraceExporter({
      url: "https://api.futureagi.com/v1/traces",
      headers: {
        "Authorization": `Bearer ${process.env.FI_API_KEY}`,
        "FI-Project-Name": process.env.FI_PROJECT_NAME,
      },
    })
  )
);
provider.register();

// Register ChromaDB instrumentation
registerInstrumentations({
  instrumentations: [
    new ChromaDBInstrumentation({
      instrumentationConfig: {
        captureDocuments: true,
      },
    }),
  ],
});

// Now use ChromaDB as normal - all operations are traced
import { ChromaClient } from "chromadb";

const client = new ChromaClient();
const collection = await client.getOrCreateCollection({ name: "my_collection" });

// This operation will be automatically traced
await collection.add({
  ids: ["doc1", "doc2"],
  documents: ["Hello world", "Goodbye world"],
  embeddings: [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]],
});
```

### Manual Instrumentation

If you need more control over when instrumentation is applied:

```typescript
import { ChromaDBInstrumentation } from "@traceai/chromadb";
import * as chromadb from "chromadb";

const instrumentation = new ChromaDBInstrumentation();
instrumentation.manuallyInstrument(chromadb);
```

## Configuration Options

```typescript
interface ChromaDBInstrumentationConfig {
  // Whether to capture query vectors in spans (may be large)
  captureQueryVectors?: boolean;

  // Whether to capture result vectors in spans (may be large)
  captureResultVectors?: boolean;

  // Whether to capture document content in spans
  captureDocuments?: boolean;
}

// Example with all options
const instrumentation = new ChromaDBInstrumentation({
  instrumentationConfig: {
    captureQueryVectors: false,  // Disable to reduce span size
    captureResultVectors: false,
    captureDocuments: true,
  },
  traceConfig: {
    maskInputs: false,
    maskOutputs: false,
  },
});
```

## Traced Operations

| Operation | Span Name | Description |
|-----------|-----------|-------------|
| `add` | `chroma add` | Add documents/embeddings to collection |
| `query` | `chroma query` | Semantic similarity search |
| `get` | `chroma get` | Retrieve documents by ID |
| `update` | `chroma update` | Update existing documents |
| `upsert` | `chroma upsert` | Insert or update documents |
| `delete` | `chroma delete` | Remove documents from collection |
| `count` | `chroma count` | Count documents in collection |
| `peek` | `chroma peek` | Preview documents in collection |

## Span Attributes

Each span includes these semantic convention attributes:

| Attribute | Description | Example |
|-----------|-------------|---------|
| `db.system` | Database system identifier | `chromadb` |
| `db.operation.name` | Operation being performed | `query`, `add` |
| `db.namespace` | Collection name | `my_collection` |
| `db.vector.collection.name` | Collection name | `my_collection` |
| `db.vector.query.top_k` | Number of results requested | `10` |
| `db.vector.query.filter` | Query filter (JSON) | `{"field": "value"}` |
| `db.vector.results.count` | Number of results returned | `5` |
| `db.vector.upsert.count` | Number of vectors upserted | `100` |
| `db.vector.upsert.dimensions` | Vector dimensions | `1536` |
| `db.vector.delete.count` | Number of vectors deleted | `10` |
| `fi.span.kind` | TraceAI span kind | `VECTOR_DB` |

## Real-World Use Cases

### 1. RAG (Retrieval-Augmented Generation) Pipeline

Build a knowledge base and retrieve relevant context for LLM responses:

```typescript
import { ChromaClient } from "chromadb";
import OpenAI from "openai";

const chroma = new ChromaClient();
const openai = new OpenAI();

// Create a knowledge base collection
const knowledgeBase = await chroma.getOrCreateCollection({
  name: "company_docs",
  metadata: { "hnsw:space": "cosine" },
});

// Ingest documents (traced automatically)
async function ingestDocuments(documents: string[]) {
  const embeddings = await Promise.all(
    documents.map(async (doc) => {
      const response = await openai.embeddings.create({
        model: "text-embedding-3-small",
        input: doc,
      });
      return response.data[0].embedding;
    })
  );

  await knowledgeBase.add({
    ids: documents.map((_, i) => `doc_${i}`),
    documents,
    embeddings,
    metadatas: documents.map((_, i) => ({
      source: "internal_docs",
      ingested_at: new Date().toISOString(),
    })),
  });
}

// RAG query function (traced automatically)
async function ragQuery(question: string): Promise<string> {
  // Get embedding for the question
  const questionEmbedding = await openai.embeddings.create({
    model: "text-embedding-3-small",
    input: question,
  });

  // Retrieve relevant context (this creates a traced span)
  const results = await knowledgeBase.query({
    queryEmbeddings: [questionEmbedding.data[0].embedding],
    nResults: 3,
    include: ["documents", "metadatas", "distances"],
  });

  // Generate response with context
  const context = results.documents[0].join("\n\n");
  const response = await openai.chat.completions.create({
    model: "gpt-4",
    messages: [
      {
        role: "system",
        content: `Answer based on this context:\n${context}`,
      },
      { role: "user", content: question },
    ],
  });

  return response.choices[0].message.content;
}
```

### 2. Semantic Search with Filters

Build a product search with category filtering:

```typescript
const productCollection = await chroma.getOrCreateCollection({
  name: "products",
});

// Add products with metadata
await productCollection.add({
  ids: ["prod_001", "prod_002", "prod_003"],
  documents: [
    "Wireless Bluetooth headphones with noise cancellation",
    "Premium leather wallet with RFID protection",
    "Ergonomic wireless mouse for gaming",
  ],
  embeddings: [/* embeddings */],
  metadatas: [
    { category: "electronics", price: 149.99, in_stock: true },
    { category: "accessories", price: 49.99, in_stock: true },
    { category: "electronics", price: 79.99, in_stock: false },
  ],
});

// Search with filters (traced with filter attributes)
async function searchProducts(query: string, category?: string, maxPrice?: number) {
  const queryEmbedding = await getEmbedding(query);

  const whereFilter: any = {};
  if (category) whereFilter.category = category;
  if (maxPrice) whereFilter.price = { $lte: maxPrice };

  const results = await productCollection.query({
    queryEmbeddings: [queryEmbedding],
    nResults: 10,
    where: Object.keys(whereFilter).length > 0 ? whereFilter : undefined,
    include: ["documents", "metadatas", "distances"],
  });

  return results;
}

// Example: Search for electronics under $100
const results = await searchProducts("wireless audio device", "electronics", 100);
```

### 3. Chatbot with Long-Term Memory

Implement persistent conversation memory:

```typescript
const memoryCollection = await chroma.getOrCreateCollection({
  name: "conversation_memory",
});

interface ConversationTurn {
  conversationId: string;
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
}

// Store conversation turn (traced)
async function storeMemory(turn: ConversationTurn) {
  const embedding = await getEmbedding(turn.content);

  await memoryCollection.upsert({
    ids: [`${turn.conversationId}_${turn.timestamp.getTime()}`],
    documents: [turn.content],
    embeddings: [embedding],
    metadatas: [{
      conversation_id: turn.conversationId,
      role: turn.role,
      timestamp: turn.timestamp.toISOString(),
    }],
  });
}

// Retrieve relevant memory (traced)
async function recallMemory(conversationId: string, currentMessage: string, limit = 5) {
  const queryEmbedding = await getEmbedding(currentMessage);

  const results = await memoryCollection.query({
    queryEmbeddings: [queryEmbedding],
    nResults: limit,
    where: { conversation_id: conversationId },
    include: ["documents", "metadatas"],
  });

  return results.documents[0].map((doc, i) => ({
    content: doc,
    role: results.metadatas[0][i].role,
    timestamp: results.metadatas[0][i].timestamp,
  }));
}

// Cleanup old memories (traced)
async function cleanupOldMemories(conversationId: string, keepLast = 100) {
  const allMemories = await memoryCollection.get({
    where: { conversation_id: conversationId },
    include: ["metadatas"],
  });

  if (allMemories.ids.length > keepLast) {
    const sortedIds = allMemories.ids
      .map((id, i) => ({ id, timestamp: allMemories.metadatas[i].timestamp }))
      .sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime());

    const idsToDelete = sortedIds.slice(keepLast).map(m => m.id);

    await memoryCollection.delete({ ids: idsToDelete });
  }
}
```

### 4. Document Deduplication

Find and remove duplicate documents:

```typescript
async function findDuplicates(collection: any, threshold = 0.95) {
  const allDocs = await collection.peek({ limit: 1000 });
  const duplicates: string[][] = [];

  for (let i = 0; i < allDocs.ids.length; i++) {
    // Query for similar documents (traced)
    const results = await collection.query({
      queryEmbeddings: [allDocs.embeddings[i]],
      nResults: 5,
      include: ["distances"],
    });

    // Find near-duplicates (distance < 1 - threshold for cosine)
    const similarIds = results.ids[0]
      .filter((id, j) => {
        const similarity = 1 - results.distances[0][j];
        return id !== allDocs.ids[i] && similarity >= threshold;
      });

    if (similarIds.length > 0) {
      duplicates.push([allDocs.ids[i], ...similarIds]);
    }
  }

  return duplicates;
}

// Remove duplicates, keeping the first one
async function removeDuplicates(collection: any, duplicateGroups: string[][]) {
  const idsToRemove = duplicateGroups.flatMap(group => group.slice(1));

  if (idsToRemove.length > 0) {
    await collection.delete({ ids: idsToRemove });
  }

  return idsToRemove.length;
}
```

## Viewing Traces

After setting up the instrumentation, you can view your traces in the Future AGI dashboard:

1. Go to [app.futureagi.com](https://app.futureagi.com)
2. Navigate to your project
3. View the Traces section to see all ChromaDB operations
4. Each span shows:
   - Operation type (add, query, etc.)
   - Collection name
   - Query parameters
   - Number of results
   - Timing information
   - Any errors that occurred

## Environment Variables

| Variable | Description |
|----------|-------------|
| `FI_API_KEY` | Your Future AGI API key |
| `FI_PROJECT_NAME` | Your project name |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP endpoint URL |

## Troubleshooting

### Spans Not Appearing

1. Ensure instrumentation is registered before importing ChromaDB
2. Verify your OTLP exporter is configured correctly
3. Check that your API key and project name are set

### Large Span Sizes

If spans are too large, disable vector capture:

```typescript
new ChromaDBInstrumentation({
  instrumentationConfig: {
    captureQueryVectors: false,
    captureResultVectors: false,
  },
});
```

## Development

```bash
# Build
pnpm build

# Run tests
pnpm test

# Type check
pnpm tsc --noEmit
```

## License

Apache-2.0

## Links

- [Future AGI Documentation](https://docs.futureagi.com)
- [ChromaDB Documentation](https://docs.trychroma.com)
- [OpenTelemetry](https://opentelemetry.io)
- [GitHub Issues](https://github.com/future-agi/traceAI/issues)
