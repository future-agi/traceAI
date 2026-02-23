# @traceai/milvus

OpenTelemetry instrumentation for [Milvus](https://milvus.io/) vector database client in Node.js/TypeScript applications.

## Installation

```bash
npm install @traceai/milvus
# or
pnpm add @traceai/milvus
# or
yarn add @traceai/milvus
```

## Prerequisites

- Node.js >= 18
- Milvus SDK (`@zilliz/milvus2-sdk-node` >= 2.0.0)
- OpenTelemetry SDK configured in your application

## Quick Start

```typescript
import { MilvusInstrumentation } from "@traceai/milvus";
import { MilvusClient } from "@zilliz/milvus2-sdk-node";

// Initialize instrumentation
const instrumentation = new MilvusInstrumentation({
  traceConfig: {
    maskInputs: false,
    maskOutputs: false,
  },
});

// Enable instrumentation
instrumentation.enable();

// Manually instrument the Milvus module
import * as milvusModule from "@zilliz/milvus2-sdk-node";
instrumentation.manuallyInstrument(milvusModule);

// Now all Milvus operations will be traced
const client = new MilvusClient({
  address: "localhost:19530",
});

// This search will be automatically traced
const results = await client.search({
  collection_name: "articles",
  vector: [0.1, 0.2, 0.3, ...],
  limit: 10,
});
```

## Configuration Options

```typescript
interface MilvusInstrumentationConfig {
  // Enable/disable the instrumentation
  enabled?: boolean;

  // Capture query vectors in span attributes
  captureQueryVectors?: boolean;

  // Capture result vectors in span attributes
  captureResultVectors?: boolean;
}

interface TraceConfigOptions {
  // Mask sensitive input data
  maskInputs?: boolean;

  // Mask sensitive output data
  maskOutputs?: boolean;
}
```

## Traced Operations

The instrumentation automatically traces the following Milvus operations:

### Search & Query
- `search` - Vector similarity search
- `query` - Scalar query with filters
- `get` - Get vectors by IDs

### Data Operations
- `insert` - Insert vectors
- `upsert` - Upsert vectors
- `delete` - Delete vectors

### Collection Management
- `createCollection` - Create new collection
- `dropCollection` - Drop collection
- `loadCollection` - Load collection into memory
- `releaseCollection` - Release collection from memory
- `describeCollection` - Get collection info

### Index Operations
- `createIndex` - Create vector index
- `dropIndex` - Drop index

## Span Attributes

Each traced operation includes relevant attributes:

| Attribute | Description |
|-----------|-------------|
| `db.system` | Always "milvus" |
| `db.operation` | Operation name (e.g., "search", "insert") |
| `db.collection.name` | Collection name |
| `db.milvus.top_k` | Number of results requested |
| `db.milvus.nprobe` | Search parameter |
| `db.milvus.metric_type` | Distance metric (L2, IP, COSINE) |

## Real-World Use Cases

### 1. High-Performance Image Search

```typescript
import { MilvusInstrumentation } from "@traceai/milvus";
import { MilvusClient, DataType } from "@zilliz/milvus2-sdk-node";

const instrumentation = new MilvusInstrumentation();
instrumentation.enable();

const client = new MilvusClient({ address: "localhost:19530" });

// Create collection for image embeddings
async function setupImageCollection() {
  await client.createCollection({
    collection_name: "images",
    fields: [
      { name: "id", data_type: DataType.Int64, is_primary_key: true },
      { name: "embedding", data_type: DataType.FloatVector, dim: 512 },
      { name: "filename", data_type: DataType.VarChar, max_length: 256 },
      { name: "category", data_type: DataType.VarChar, max_length: 64 },
    ],
    index_params: {
      field_name: "embedding",
      index_type: "IVF_FLAT",
      metric_type: "L2",
      params: { nlist: 1024 },
    },
  });
}

// Search similar images (traced)
async function findSimilarImages(
  imageEmbedding: number[],
  category?: string,
  limit = 20
) {
  const searchParams = {
    collection_name: "images",
    vector: imageEmbedding,
    limit,
    output_fields: ["filename", "category"],
    params: { nprobe: 16 },
  };

  if (category) {
    searchParams["filter"] = `category == "${category}"`;
  }

  const results = await client.search(searchParams);
  return results.results;
}
```

### 2. Real-time Fraud Detection

```typescript
async function detectFraud(transactionVector: number[], threshold = 0.85) {
  // Search for similar transaction patterns
  const results = await client.search({
    collection_name: "fraud_patterns",
    vector: transactionVector,
    limit: 5,
    params: { nprobe: 32 },
    output_fields: ["pattern_type", "risk_score", "description"],
  });

  const fraudMatches = results.results.filter(
    (r) => r.score >= threshold && r.pattern_type === "fraud"
  );

  return {
    isFraudulent: fraudMatches.length > 0,
    confidence: fraudMatches[0]?.score ?? 0,
    matchedPatterns: fraudMatches,
  };
}
```

### 3. Multi-modal Search (Text + Image)

```typescript
async function multiModalSearch(
  textEmbedding: number[],
  imageEmbedding: number[],
  weights = { text: 0.6, image: 0.4 }
) {
  // Combine embeddings with weights
  const combinedVector = textEmbedding.map(
    (val, i) => val * weights.text + imageEmbedding[i] * weights.image
  );

  const results = await client.search({
    collection_name: "products",
    vector: combinedVector,
    limit: 50,
    output_fields: ["name", "description", "image_url", "price"],
    params: { nprobe: 64 },
  });

  return results.results;
}
```

### 4. Streaming Data Ingestion

```typescript
async function ingestEmbeddings(
  embeddings: { id: number; vector: number[]; metadata: any }[]
) {
  const BATCH_SIZE = 1000;

  for (let i = 0; i < embeddings.length; i += BATCH_SIZE) {
    const batch = embeddings.slice(i, i + BATCH_SIZE);

    // Each upsert is traced separately
    await client.upsert({
      collection_name: "documents",
      data: batch.map((item) => ({
        id: item.id,
        embedding: item.vector,
        metadata: JSON.stringify(item.metadata),
      })),
    });
  }
}
```

### 5. Recommendation with User History

```typescript
async function getPersonalizedRecommendations(
  userId: string,
  recentItemVectors: number[][],
  limit = 20
) {
  // Average recent item vectors for user preference
  const userPreference = recentItemVectors[0].map((_, i) =>
    recentItemVectors.reduce((sum, vec) => sum + vec[i], 0) /
    recentItemVectors.length
  );

  // Get user's already viewed items
  const viewedItems = await client.query({
    collection_name: "user_views",
    filter: `user_id == "${userId}"`,
    output_fields: ["item_id"],
  });

  const excludeIds = viewedItems.data.map((v) => v.item_id);

  // Search for recommendations excluding viewed items
  const results = await client.search({
    collection_name: "items",
    vector: userPreference,
    limit: limit + excludeIds.length,
    output_fields: ["name", "category", "thumbnail"],
    filter:
      excludeIds.length > 0
        ? `id not in [${excludeIds.join(",")}]`
        : undefined,
  });

  return results.results.slice(0, limit);
}
```

### 6. Hybrid Search with Scalar Filtering

```typescript
async function hybridProductSearch(
  queryVector: number[],
  filters: {
    minPrice?: number;
    maxPrice?: number;
    categories?: string[];
    inStock?: boolean;
  }
) {
  // Build filter expression
  const filterParts: string[] = [];

  if (filters.minPrice !== undefined) {
    filterParts.push(`price >= ${filters.minPrice}`);
  }
  if (filters.maxPrice !== undefined) {
    filterParts.push(`price <= ${filters.maxPrice}`);
  }
  if (filters.categories?.length) {
    filterParts.push(
      `category in [${filters.categories.map((c) => `"${c}"`).join(",")}]`
    );
  }
  if (filters.inStock !== undefined) {
    filterParts.push(`in_stock == ${filters.inStock}`);
  }

  const results = await client.search({
    collection_name: "products",
    vector: queryVector,
    limit: 100,
    filter: filterParts.length > 0 ? filterParts.join(" && ") : undefined,
    output_fields: ["name", "price", "category", "in_stock", "rating"],
  });

  return results.results;
}
```

## Integration with OpenTelemetry

```typescript
import { NodeSDK } from "@opentelemetry/sdk-node";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { MilvusInstrumentation } from "@traceai/milvus";

const sdk = new NodeSDK({
  traceExporter: new OTLPTraceExporter({
    url: "http://localhost:4318/v1/traces",
  }),
  instrumentations: [new MilvusInstrumentation()],
});

sdk.start();
```

## Performance Considerations

- **Batch Operations**: Use `insert` and `upsert` with batches for better throughput
- **Index Selection**: Choose appropriate index type based on your use case:
  - `IVF_FLAT`: Good balance of speed and accuracy
  - `HNSW`: Best for high-recall requirements
  - `IVF_PQ`: Best for memory-constrained environments
- **Partition Keys**: Use partitions for multi-tenant scenarios

## License

Apache-2.0
