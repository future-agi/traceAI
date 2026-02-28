# @traceai/redis

OpenTelemetry instrumentation for [Redis Vector Search](https://redis.io/docs/stack/search/reference/vectors/) (RediSearch) in Node.js/TypeScript applications.

## Installation

```bash
npm install @traceai/redis
# or
pnpm add @traceai/redis
# or
yarn add @traceai/redis
```

## Prerequisites

- Node.js >= 18
- Redis client (`redis` >= 4.0.0)
- Redis Stack or Redis with RediSearch module
- OpenTelemetry SDK configured in your application

## Quick Start

```typescript
import { RedisInstrumentation } from "@traceai/redis";
import { createClient } from "redis";

// Initialize instrumentation
const instrumentation = new RedisInstrumentation({
  traceConfig: {
    maskInputs: false,
    maskOutputs: false,
  },
});

// Enable instrumentation
instrumentation.enable();

// Manually instrument the redis module
import * as redis from "redis";
instrumentation.manuallyInstrument(redis);

// Now all Redis operations will be traced
const client = createClient({ url: process.env.REDIS_URL });
await client.connect();

// Vector search with FT.SEARCH (traced)
const results = await client.ft.search("idx:products", "*=>[KNN 10 @embedding $vector AS score]", {
  PARAMS: {
    vector: Buffer.from(new Float32Array([0.1, 0.2, 0.3]).buffer),
  },
  RETURN: ["name", "price", "score"],
  SORTBY: { BY: "score" },
  DIALECT: 2,
});
```

## Configuration Options

```typescript
interface RedisInstrumentationConfig {
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

The instrumentation automatically traces Redis operations with special handling for vector search:

### Vector Search Operations

- `FT.SEARCH` - Vector similarity search
- `FT.AGGREGATE` - Aggregation with vector scoring
- `FT.CREATE` - Create search index

### Hash Operations (for vector storage)

- `HSET` - Store hash with vector field
- `HGET` / `HMGET` - Retrieve hash fields
- `HDEL` - Delete hash

### JSON Operations (for document storage)

- `JSON.SET` - Store JSON document
- `JSON.GET` - Retrieve JSON document

## Span Attributes

Each traced operation includes relevant attributes:

| Attribute                 | Description                                    |
| ------------------------- | ---------------------------------------------- |
| `db.system`               | Always "redis"                                 |
| `db.operation`            | Operation name (e.g., "FT.SEARCH", "HSET")     |
| `db.redis.index_name`     | Search index name                              |
| `db.redis.query`          | Search query                                   |
| `db.redis.vector_field`   | Vector field name                              |
| `db.redis.k`              | Number of neighbors (KNN)                      |
| `db.redis.result_count`   | Number of results returned                     |

## Real-World Use Cases

### 1. E-commerce Product Search

```typescript
import { RedisInstrumentation } from "@traceai/redis";
import { createClient, SchemaFieldTypes, VectorAlgorithms } from "redis";

const instrumentation = new RedisInstrumentation();
instrumentation.enable();

const client = createClient({ url: process.env.REDIS_URL });
await client.connect();

// Create product search index
async function createProductIndex() {
  try {
    await client.ft.create(
      "idx:products",
      {
        "$.name": { type: SchemaFieldTypes.TEXT, AS: "name" },
        "$.description": { type: SchemaFieldTypes.TEXT, AS: "description" },
        "$.category": { type: SchemaFieldTypes.TAG, AS: "category" },
        "$.price": { type: SchemaFieldTypes.NUMERIC, AS: "price" },
        "$.embedding": {
          type: SchemaFieldTypes.VECTOR,
          AS: "embedding",
          ALGORITHM: VectorAlgorithms.HNSW,
          TYPE: "FLOAT32",
          DIM: 384,
          DISTANCE_METRIC: "COSINE",
        },
      },
      { ON: "JSON", PREFIX: "product:" }
    );
  } catch (e: any) {
    if (!e.message.includes("Index already exists")) throw e;
  }
}

// Semantic product search (traced)
async function searchProducts(queryVector: number[], filters?: ProductFilters) {
  const query = buildSearchQuery(filters);

  const results = await client.ft.search(
    "idx:products",
    `${query}=>[KNN 20 @embedding $vector AS score]`,
    {
      PARAMS: {
        vector: Buffer.from(new Float32Array(queryVector).buffer),
      },
      RETURN: ["name", "description", "price", "category", "score"],
      SORTBY: { BY: "score" },
      LIMIT: { from: 0, size: 20 },
      DIALECT: 2,
    }
  );

  return results.documents.map((doc) => ({
    id: doc.id.replace("product:", ""),
    ...doc.value,
  }));
}

function buildSearchQuery(filters?: ProductFilters): string {
  const parts: string[] = ["*"];

  if (filters?.category) {
    parts.push(`@category:{${filters.category}}`);
  }
  if (filters?.minPrice !== undefined) {
    parts.push(`@price:[${filters.minPrice} +inf]`);
  }
  if (filters?.maxPrice !== undefined) {
    parts.push(`@price:[-inf ${filters.maxPrice}]`);
  }

  return parts.length > 1 ? `(${parts.slice(1).join(" ")})` : parts[0];
}
```

### 2. Real-time Recommendation Cache

```typescript
async function cacheUserRecommendations(
  userId: string,
  recommendations: Recommendation[]
) {
  const pipeline = client.multi();

  // Store recommendations as JSON with embeddings
  for (const rec of recommendations) {
    pipeline.json.set(`rec:${userId}:${rec.itemId}`, "$", {
      itemId: rec.itemId,
      name: rec.name,
      score: rec.score,
      embedding: rec.embedding,
      cachedAt: Date.now(),
    });
  }

  // Set expiration
  for (const rec of recommendations) {
    pipeline.expire(`rec:${userId}:${rec.itemId}`, 3600); // 1 hour TTL
  }

  await pipeline.exec();
}

// Find similar to what user is viewing (traced)
async function getSimilarFromCache(userId: string, itemEmbedding: number[]) {
  const results = await client.ft.search(
    "idx:recommendations",
    `@userId:{${userId}}=>[KNN 5 @embedding $vector AS similarity]`,
    {
      PARAMS: {
        vector: Buffer.from(new Float32Array(itemEmbedding).buffer),
      },
      RETURN: ["itemId", "name", "score", "similarity"],
      SORTBY: { BY: "similarity" },
      DIALECT: 2,
    }
  );

  return results.documents;
}
```

### 3. Session-based Semantic Search

```typescript
interface SearchSession {
  sessionId: string;
  queries: { text: string; embedding: number[]; timestamp: number }[];
}

async function sessionAwareSearch(
  sessionId: string,
  queryEmbedding: number[],
  recentQueryWeight = 0.3
) {
  // Get session context
  const sessionData = await client.json.get(`session:${sessionId}`);

  let searchVector = queryEmbedding;

  if (sessionData?.queries?.length > 0) {
    // Blend with recent query embeddings for context
    const recentEmbedding = sessionData.queries[sessionData.queries.length - 1].embedding;
    searchVector = queryEmbedding.map(
      (val, i) =>
        val * (1 - recentQueryWeight) + recentEmbedding[i] * recentQueryWeight
    );
  }

  // Search with blended vector (traced)
  const results = await client.ft.search(
    "idx:content",
    "*=>[KNN 10 @embedding $vector AS score]",
    {
      PARAMS: {
        vector: Buffer.from(new Float32Array(searchVector).buffer),
      },
      RETURN: ["title", "content", "score"],
      DIALECT: 2,
    }
  );

  // Update session
  await client.json.arrAppend(`session:${sessionId}`, "$.queries", {
    text: "",
    embedding: queryEmbedding,
    timestamp: Date.now(),
  });

  return results.documents;
}
```

### 4. Real-time Anomaly Detection

```typescript
async function detectAnomaly(metricVector: number[], threshold = 0.8) {
  // Search for similar historical patterns (traced)
  const similar = await client.ft.search(
    "idx:metrics",
    "*=>[KNN 10 @embedding $vector AS similarity]",
    {
      PARAMS: {
        vector: Buffer.from(new Float32Array(metricVector).buffer),
      },
      RETURN: ["timestamp", "label", "similarity"],
      DIALECT: 2,
    }
  );

  const normalPatterns = similar.documents.filter(
    (doc) => doc.value.label === "normal" && parseFloat(doc.value.similarity) > threshold
  );

  const isAnomaly = normalPatterns.length < 3;

  if (isAnomaly) {
    // Store anomaly for future reference
    await client.json.set(`anomaly:${Date.now()}`, "$", {
      embedding: metricVector,
      timestamp: Date.now(),
      similarPatterns: similar.documents.slice(0, 3),
    });
  }

  return {
    isAnomaly,
    confidence: 1 - (normalPatterns.length / 10),
    similarPatterns: similar.documents,
  };
}
```

### 5. Multi-index Federated Search

```typescript
async function federatedSearch(queryVector: number[]) {
  // Search across multiple indices in parallel (all traced)
  const [products, articles, support] = await Promise.all([
    client.ft.search(
      "idx:products",
      "*=>[KNN 5 @embedding $vector AS score]",
      {
        PARAMS: { vector: Buffer.from(new Float32Array(queryVector).buffer) },
        RETURN: ["name", "price", "score"],
        DIALECT: 2,
      }
    ),
    client.ft.search(
      "idx:articles",
      "*=>[KNN 5 @embedding $vector AS score]",
      {
        PARAMS: { vector: Buffer.from(new Float32Array(queryVector).buffer) },
        RETURN: ["title", "excerpt", "score"],
        DIALECT: 2,
      }
    ),
    client.ft.search(
      "idx:support",
      "*=>[KNN 5 @embedding $vector AS score]",
      {
        PARAMS: { vector: Buffer.from(new Float32Array(queryVector).buffer) },
        RETURN: ["question", "answer", "score"],
        DIALECT: 2,
      }
    ),
  ]);

  return {
    products: products.documents,
    articles: articles.documents,
    supportFAQs: support.documents,
  };
}
```

### 6. Geo + Vector Hybrid Search

```typescript
async function nearbySemanticSearch(
  queryVector: number[],
  location: { lat: number; lon: number },
  radiusKm: number
) {
  // Combine geo filter with vector search (traced)
  const results = await client.ft.search(
    "idx:places",
    `@location:[${location.lon} ${location.lat} ${radiusKm} km]=>[KNN 20 @embedding $vector AS relevance]`,
    {
      PARAMS: {
        vector: Buffer.from(new Float32Array(queryVector).buffer),
      },
      RETURN: ["name", "address", "location", "relevance"],
      SORTBY: { BY: "relevance" },
      DIALECT: 2,
    }
  );

  return results.documents.map((doc) => ({
    ...doc.value,
    id: doc.id,
  }));
}
```

## Index Creation Examples

```typescript
// HNSW index for high-recall scenarios
await client.ft.create(
  "idx:documents",
  {
    embedding: {
      type: SchemaFieldTypes.VECTOR,
      ALGORITHM: VectorAlgorithms.HNSW,
      TYPE: "FLOAT32",
      DIM: 1536,
      DISTANCE_METRIC: "COSINE",
      M: 40,
      EF_CONSTRUCTION: 200,
    },
  },
  { ON: "HASH", PREFIX: "doc:" }
);

// FLAT index for exact search on smaller datasets
await client.ft.create(
  "idx:cache",
  {
    embedding: {
      type: SchemaFieldTypes.VECTOR,
      ALGORITHM: VectorAlgorithms.FLAT,
      TYPE: "FLOAT32",
      DIM: 384,
      DISTANCE_METRIC: "L2",
    },
  },
  { ON: "JSON", PREFIX: "cache:" }
);
```

## Integration with OpenTelemetry

```typescript
import { NodeSDK } from "@opentelemetry/sdk-node";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { RedisInstrumentation } from "@traceai/redis";

const sdk = new NodeSDK({
  traceExporter: new OTLPTraceExporter({
    url: "http://localhost:4318/v1/traces",
  }),
  instrumentations: [new RedisInstrumentation()],
});

sdk.start();
```

## Performance Tips

1. **Use HNSW** for large datasets (>100k vectors)
2. **Tune M and EF_CONSTRUCTION** based on recall requirements
3. **Use connection pooling** for high-throughput scenarios
4. **Leverage Redis pipelining** for batch operations
5. **Set appropriate TTLs** for cached vectors

## License

Apache-2.0
