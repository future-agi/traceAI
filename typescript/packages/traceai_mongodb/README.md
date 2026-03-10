# @traceai/mongodb

OpenTelemetry instrumentation for [MongoDB Atlas Vector Search](https://www.mongodb.com/atlas/vector-search) in Node.js/TypeScript applications.

## Installation

```bash
npm install @traceai/mongodb
# or
pnpm add @traceai/mongodb
# or
yarn add @traceai/mongodb
```

## Prerequisites

- Node.js >= 18
- MongoDB driver (`mongodb` >= 5.0.0)
- MongoDB Atlas with Vector Search enabled (or MongoDB 7.0+ with Atlas Search)
- OpenTelemetry SDK configured in your application

## Quick Start

```typescript
import { MongoDBInstrumentation } from "@traceai/mongodb";
import { MongoClient } from "mongodb";

// Initialize instrumentation
const instrumentation = new MongoDBInstrumentation({
  traceConfig: {
    maskInputs: false,
    maskOutputs: false,
  },
});

// Enable instrumentation
instrumentation.enable();

// Manually instrument the mongodb module
import * as mongodb from "mongodb";
instrumentation.manuallyInstrument(mongodb);

// Now all MongoDB operations will be traced
const client = new MongoClient(process.env.MONGODB_URI!);
await client.connect();

const collection = client.db("mydb").collection("documents");

// Vector search with $vectorSearch (traced)
const results = await collection
  .aggregate([
    {
      $vectorSearch: {
        index: "vector_index",
        path: "embedding",
        queryVector: [0.1, 0.2, 0.3, ...],
        numCandidates: 100,
        limit: 10,
      },
    },
  ])
  .toArray();
```

## Configuration Options

```typescript
interface MongoDBInstrumentationConfig {
  // Enable/disable the instrumentation
  enabled?: boolean;

  // Capture query vectors in span attributes
  captureQueryVectors?: boolean;

  // Capture result documents
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

The instrumentation automatically traces MongoDB operations with special handling for vector search:

### Vector Search Operations

- `$vectorSearch` - Atlas Vector Search aggregation stage
- `$search` - Atlas Search (including vector queries)

### Standard Operations (also traced)

- `aggregate` - Aggregation pipelines
- `find` - Document queries
- `insertOne` / `insertMany` - Insert operations
- `updateOne` / `updateMany` - Update operations
- `deleteOne` / `deleteMany` - Delete operations

## Span Attributes

Each traced operation includes relevant attributes:

| Attribute                  | Description                                    |
| -------------------------- | ---------------------------------------------- |
| `db.system`                | Always "mongodb"                               |
| `db.operation`             | Operation name (e.g., "aggregate", "find")     |
| `db.collection.name`       | Collection name                                |
| `db.mongodb.is_vector_search` | true if $vectorSearch is detected           |
| `db.mongodb.vector_index`  | Vector index name                              |
| `db.mongodb.num_candidates`| Number of candidates for ANN search            |
| `db.mongodb.limit`         | Result limit                                   |

## Real-World Use Cases

### 1. Semantic Search with MongoDB Atlas

```typescript
import { MongoDBInstrumentation } from "@traceai/mongodb";
import { MongoClient } from "mongodb";
import OpenAI from "openai";

const instrumentation = new MongoDBInstrumentation();
instrumentation.enable();

const client = new MongoClient(process.env.MONGODB_URI!);
const openai = new OpenAI();

async function semanticSearch(query: string, limit = 10) {
  // Generate embedding for query
  const embeddingResponse = await openai.embeddings.create({
    model: "text-embedding-3-small",
    input: query,
  });
  const queryVector = embeddingResponse.data[0].embedding;

  const collection = client.db("app").collection("articles");

  // Vector search (traced)
  const results = await collection
    .aggregate([
      {
        $vectorSearch: {
          index: "article_embeddings",
          path: "embedding",
          queryVector,
          numCandidates: 150,
          limit,
        },
      },
      {
        $project: {
          title: 1,
          content: 1,
          author: 1,
          score: { $meta: "vectorSearchScore" },
        },
      },
    ])
    .toArray();

  return results;
}
```

### 2. Hybrid Search (Vector + Text + Filters)

```typescript
async function hybridSearch(
  queryVector: number[],
  textQuery: string,
  filters: { category?: string; dateRange?: { start: Date; end: Date } }
) {
  const collection = client.db("app").collection("products");

  const pipeline: any[] = [
    {
      $vectorSearch: {
        index: "product_vectors",
        path: "embedding",
        queryVector,
        numCandidates: 200,
        limit: 100,
        filter: filters.category
          ? { category: { $eq: filters.category } }
          : undefined,
      },
    },
    {
      $addFields: {
        vectorScore: { $meta: "vectorSearchScore" },
      },
    },
  ];

  // Add text search scoring
  if (textQuery) {
    pipeline.push({
      $addFields: {
        textScore: {
          $cond: {
            if: {
              $regexMatch: {
                input: "$name",
                regex: textQuery,
                options: "i",
              },
            },
            then: 0.3,
            else: 0,
          },
        },
      },
    });
  }

  // Combine scores and sort
  pipeline.push(
    {
      $addFields: {
        combinedScore: {
          $add: ["$vectorScore", { $ifNull: ["$textScore", 0] }],
        },
      },
    },
    { $sort: { combinedScore: -1 } },
    { $limit: 20 }
  );

  const results = await collection.aggregate(pipeline).toArray();
  return results;
}
```

### 3. RAG with Conversation Context

```typescript
interface Message {
  role: "user" | "assistant";
  content: string;
}

async function ragWithContext(
  userQuery: string,
  conversationHistory: Message[]
) {
  const collection = client.db("app").collection("knowledge");

  // Create context-aware query
  const contextQuery =
    conversationHistory.slice(-3).map((m) => m.content).join(" ") +
    " " +
    userQuery;

  const embedding = await generateEmbedding(contextQuery);

  // Retrieve relevant context (traced)
  const context = await collection
    .aggregate([
      {
        $vectorSearch: {
          index: "knowledge_index",
          path: "embedding",
          queryVector: embedding,
          numCandidates: 100,
          limit: 5,
        },
      },
      {
        $project: {
          content: 1,
          source: 1,
          score: { $meta: "vectorSearchScore" },
        },
      },
    ])
    .toArray();

  // Generate response with context
  const response = await openai.chat.completions.create({
    model: "gpt-4",
    messages: [
      {
        role: "system",
        content: `Use this context to answer:\n${context.map((c) => c.content).join("\n")}`,
      },
      ...conversationHistory,
      { role: "user", content: userQuery },
    ],
  });

  return {
    answer: response.choices[0].message.content,
    sources: context.map((c) => c.source),
  };
}
```

### 4. Real-time Recommendation Engine

```typescript
async function getRecommendations(
  userId: string,
  currentItemId: string,
  limit = 10
) {
  const db = client.db("ecommerce");

  // Get current item's embedding
  const currentItem = await db
    .collection("products")
    .findOne({ _id: currentItemId });

  if (!currentItem?.embedding) {
    return [];
  }

  // Get user's purchase history for filtering
  const userHistory = await db
    .collection("orders")
    .distinct("productId", { userId });

  // Find similar products excluding already purchased (traced)
  const recommendations = await db
    .collection("products")
    .aggregate([
      {
        $vectorSearch: {
          index: "product_similarity",
          path: "embedding",
          queryVector: currentItem.embedding,
          numCandidates: 150,
          limit: limit + userHistory.length,
          filter: {
            _id: { $nin: userHistory },
            inStock: true,
          },
        },
      },
      {
        $project: {
          name: 1,
          price: 1,
          imageUrl: 1,
          category: 1,
          score: { $meta: "vectorSearchScore" },
        },
      },
      { $limit: limit },
    ])
    .toArray();

  return recommendations;
}
```

### 5. Document Clustering and Deduplication

```typescript
async function findDuplicates(documentId: string, threshold = 0.95) {
  const collection = client.db("docs").collection("documents");

  const document = await collection.findOne({ _id: documentId });
  if (!document?.embedding) return [];

  // Find near-duplicates (traced)
  const similar = await collection
    .aggregate([
      {
        $vectorSearch: {
          index: "doc_embeddings",
          path: "embedding",
          queryVector: document.embedding,
          numCandidates: 50,
          limit: 20,
        },
      },
      {
        $match: {
          _id: { $ne: documentId },
        },
      },
      {
        $addFields: {
          similarity: { $meta: "vectorSearchScore" },
        },
      },
      {
        $match: {
          similarity: { $gte: threshold },
        },
      },
    ])
    .toArray();

  return similar;
}
```

### 6. Multi-tenant Vector Search

```typescript
async function tenantSearch(
  tenantId: string,
  queryVector: number[],
  filters?: { category?: string }
) {
  const collection = client.db("saas").collection("tenant_data");

  // Pre-filter by tenant for security and performance
  const searchFilter: any = { tenantId: { $eq: tenantId } };

  if (filters?.category) {
    searchFilter.category = { $eq: filters.category };
  }

  const results = await collection
    .aggregate([
      {
        $vectorSearch: {
          index: "tenant_vectors",
          path: "embedding",
          queryVector,
          numCandidates: 100,
          limit: 20,
          filter: searchFilter,
        },
      },
      {
        $project: {
          embedding: 0, // Don't return embeddings
          tenantId: 0, // Don't expose tenant info
        },
      },
    ])
    .toArray();

  return results;
}
```

## Setting Up Vector Search Index

```javascript
// Create vector search index in MongoDB Atlas
db.collection.createSearchIndex({
  name: "vector_index",
  type: "vectorSearch",
  definition: {
    fields: [
      {
        type: "vector",
        path: "embedding",
        numDimensions: 1536,
        similarity: "cosine",
      },
      {
        type: "filter",
        path: "category",
      },
      {
        type: "filter",
        path: "tenantId",
      },
    ],
  },
});
```

## Integration with OpenTelemetry

```typescript
import { NodeSDK } from "@opentelemetry/sdk-node";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { MongoDBInstrumentation } from "@traceai/mongodb";

const sdk = new NodeSDK({
  traceExporter: new OTLPTraceExporter({
    url: "http://localhost:4318/v1/traces",
  }),
  instrumentations: [new MongoDBInstrumentation()],
});

sdk.start();
```

## License

Apache-2.0
