# @traceai/qdrant

OpenTelemetry instrumentation for [Qdrant](https://qdrant.tech/) - the high-performance open-source vector database.

## Overview

This package provides automatic tracing for Qdrant operations in Node.js applications, enabling full observability of your self-hosted or cloud vector database interactions in RAG pipelines, semantic search, and AI applications.

## Installation

```bash
npm install @traceai/qdrant
# or
yarn add @traceai/qdrant
# or
pnpm add @traceai/qdrant
```

## Quick Start

### Basic Setup

```typescript
import { QdrantInstrumentation } from "@traceai/qdrant";
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

// Register Qdrant instrumentation
registerInstrumentations({
  instrumentations: [
    new QdrantInstrumentation(),
  ],
});

// Now use Qdrant as normal - all operations are traced
import { QdrantClient } from "@qdrant/js-client-rest";

const client = new QdrantClient({ url: "http://localhost:6333" });

// This operation will be automatically traced
const results = await client.search("my_collection", {
  vector: [0.1, 0.2, 0.3, /* ... */],
  limit: 10,
  with_payload: true,
});
```

### Manual Instrumentation

```typescript
import { QdrantInstrumentation } from "@traceai/qdrant";
import * as qdrant from "@qdrant/js-client-rest";

const instrumentation = new QdrantInstrumentation();
instrumentation.manuallyInstrument(qdrant);
```

## Configuration Options

```typescript
interface QdrantInstrumentationConfig {
  // Whether to capture query vectors in spans (may be large)
  captureQueryVectors?: boolean;

  // Whether to capture result vectors in spans (may be large)
  captureResultVectors?: boolean;
}

// Example with all options
const instrumentation = new QdrantInstrumentation({
  instrumentationConfig: {
    captureQueryVectors: false,  // Recommended for production
    captureResultVectors: false,
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
| `search` | `qdrant search` | Semantic similarity search |
| `query` | `qdrant query` | Query points (newer API) |
| `queryPoints` | `qdrant query_points` | Query points with prefetch |
| `upsert` | `qdrant upsert` | Insert or update vectors |
| `delete` | `qdrant delete` | Delete vectors |
| `retrieve` | `qdrant retrieve` | Retrieve vectors by ID |
| `scroll` | `qdrant scroll` | Paginate through vectors |
| `count` | `qdrant count` | Count vectors in collection |
| `getCollection` | `qdrant get_collection` | Get collection info |

## Span Attributes

Each span includes these semantic convention attributes:

| Attribute | Description | Example |
|-----------|-------------|---------|
| `db.system` | Database system identifier | `qdrant` |
| `db.operation.name` | Operation being performed | `search`, `upsert` |
| `db.namespace` | Collection name | `my_collection` |
| `db.vector.collection.name` | Collection name | `my_collection` |
| `db.vector.query.top_k` | Number of results requested | `10` |
| `db.vector.query.filter` | Query filter (JSON) | `{"must": [...]}` |
| `db.vector.query.include_metadata` | Whether payload included | `true` |
| `db.vector.query.include_vectors` | Whether vectors included | `false` |
| `db.vector.query.score_threshold` | Minimum score threshold | `0.7` |
| `db.vector.results.count` | Number of results returned | `10` |
| `db.vector.upsert.count` | Number of vectors upserted | `100` |
| `db.vector.upsert.dimensions` | Vector dimensions | `768` |
| `db.vector.delete.count` | Number of vectors deleted | `50` |
| `db.vector.index.dimensions` | Collection dimensions | `768` |
| `fi.span.kind` | TraceAI span kind | `VECTOR_DB` |

## Real-World Use Cases

### 1. Self-Hosted RAG Pipeline

Build a privacy-first RAG system with self-hosted Qdrant:

```typescript
import { QdrantClient } from "@qdrant/js-client-rest";
import OpenAI from "openai";

const qdrant = new QdrantClient({ url: process.env.QDRANT_URL });
const openai = new OpenAI();

// Create collection with optimal settings
async function initializeCollection() {
  const collections = await qdrant.getCollections();
  const exists = collections.collections.some(c => c.name === "knowledge_base");

  if (!exists) {
    await qdrant.createCollection("knowledge_base", {
      vectors: {
        size: 1536,
        distance: "Cosine",
      },
      optimizers_config: {
        indexing_threshold: 20000,
      },
      replication_factor: 2,
    });
  }
}

// Ingest documents (traced automatically)
async function ingestDocuments(documents: Document[]) {
  const points = await Promise.all(
    documents.map(async (doc, i) => {
      const embedding = await openai.embeddings.create({
        model: "text-embedding-3-small",
        input: doc.content,
      });

      return {
        id: doc.id,
        vector: embedding.data[0].embedding,
        payload: {
          content: doc.content,
          title: doc.title,
          source: doc.source,
          created_at: new Date().toISOString(),
          metadata: doc.metadata,
        },
      };
    })
  );

  // Batch upsert (traced)
  await qdrant.upsert("knowledge_base", {
    points,
    wait: true,
  });
}

// RAG query with advanced filtering (traced)
async function ragQuery(question: string, filters?: FilterOptions) {
  const queryEmbedding = await openai.embeddings.create({
    model: "text-embedding-3-small",
    input: question,
  });

  // Build Qdrant filter
  const filter: any = { must: [] };
  if (filters?.source) {
    filter.must.push({ key: "source", match: { value: filters.source } });
  }
  if (filters?.dateRange) {
    filter.must.push({
      key: "created_at",
      range: {
        gte: filters.dateRange.start,
        lte: filters.dateRange.end,
      },
    });
  }

  // Semantic search (traced)
  const results = await qdrant.search("knowledge_base", {
    vector: queryEmbedding.data[0].embedding,
    limit: 5,
    with_payload: true,
    filter: filter.must.length > 0 ? filter : undefined,
    score_threshold: 0.7,
  });

  // Build context and generate response
  const context = results
    .map(r => r.payload?.content)
    .filter(Boolean)
    .join("\n\n");

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

  return {
    answer: response.choices[0].message.content,
    sources: results.map(r => ({
      id: r.id,
      score: r.score,
      title: r.payload?.title,
    })),
  };
}
```

### 2. Multi-Modal Search (Text + Images)

Implement search across text and images using CLIP embeddings:

```typescript
class MultiModalSearch {
  private client: QdrantClient;

  constructor() {
    this.client = new QdrantClient({ url: process.env.QDRANT_URL });
  }

  // Initialize multi-modal collection
  async initialize() {
    await this.client.createCollection("multimodal", {
      vectors: {
        text: { size: 512, distance: "Cosine" },
        image: { size: 512, distance: "Cosine" },
      },
    });
  }

  // Index item with both text and image embeddings (traced)
  async indexItem(item: MultiModalItem) {
    await this.client.upsert("multimodal", {
      points: [{
        id: item.id,
        vector: {
          text: item.textEmbedding,
          image: item.imageEmbedding,
        },
        payload: {
          title: item.title,
          description: item.description,
          image_url: item.imageUrl,
          category: item.category,
          created_at: new Date().toISOString(),
        },
      }],
    });
  }

  // Search by text (traced)
  async searchByText(textEmbedding: number[], options: SearchOptions = {}) {
    return await this.client.search("multimodal", {
      vector: {
        name: "text",
        vector: textEmbedding,
      },
      limit: options.limit || 10,
      with_payload: true,
      filter: options.filter,
    });
  }

  // Search by image (traced)
  async searchByImage(imageEmbedding: number[], options: SearchOptions = {}) {
    return await this.client.search("multimodal", {
      vector: {
        name: "image",
        vector: imageEmbedding,
      },
      limit: options.limit || 10,
      with_payload: true,
      filter: options.filter,
    });
  }

  // Hybrid search combining both modalities (traced)
  async hybridSearch(
    textEmbedding: number[],
    imageEmbedding: number[],
    textWeight = 0.5
  ) {
    const textResults = await this.searchByText(textEmbedding, { limit: 20 });
    const imageResults = await this.searchByImage(imageEmbedding, { limit: 20 });

    // Combine and re-rank results
    const scoreMap = new Map<string, number>();

    textResults.forEach(r => {
      scoreMap.set(String(r.id), (r.score || 0) * textWeight);
    });

    imageResults.forEach(r => {
      const existing = scoreMap.get(String(r.id)) || 0;
      scoreMap.set(String(r.id), existing + (r.score || 0) * (1 - textWeight));
    });

    // Sort by combined score
    return Array.from(scoreMap.entries())
      .sort((a, b) => b[1] - a[1])
      .slice(0, 10);
  }
}
```

### 3. Real-Time Recommendation System

Build a recommendation engine with user preference tracking:

```typescript
class RecommendationEngine {
  private client: QdrantClient;

  constructor() {
    this.client = new QdrantClient({ url: process.env.QDRANT_URL });
  }

  // Index products (traced)
  async indexProducts(products: Product[]) {
    const points = products.map(p => ({
      id: p.id,
      vector: p.embedding,
      payload: {
        name: p.name,
        category: p.category,
        subcategory: p.subcategory,
        price: p.price,
        rating: p.rating,
        tags: p.tags,
        in_stock: p.inStock,
      },
    }));

    await this.client.upsert("products", { points });
  }

  // Get recommendations based on user's liked items (traced)
  async getRecommendations(
    userId: string,
    likedProductIds: string[],
    options: RecommendationOptions = {}
  ) {
    // Fetch liked products to get their embeddings
    const likedProducts = await this.client.retrieve("products", {
      ids: likedProductIds.map(id => parseInt(id)),
      with_vector: true,
    });

    // Average the embeddings for preference vector
    const avgVector = this.averageVectors(
      likedProducts.map(p => p.vector as number[])
    );

    // Search for similar products (traced)
    const results = await this.client.search("products", {
      vector: avgVector,
      limit: options.limit || 20,
      with_payload: true,
      filter: {
        must: [
          { key: "in_stock", match: { value: true } },
        ],
        must_not: [
          // Exclude already liked items
          ...likedProductIds.map(id => ({
            has_id: [parseInt(id)],
          })),
        ],
        should: options.preferredCategories?.map(cat => ({
          key: "category",
          match: { value: cat },
        })),
      },
      score_threshold: 0.5,
    });

    return results.map(r => ({
      id: r.id,
      score: r.score,
      product: r.payload,
    }));
  }

  // Find similar products (traced)
  async findSimilar(productId: string, limit = 10) {
    const [product] = await this.client.retrieve("products", {
      ids: [parseInt(productId)],
      with_vector: true,
    });

    if (!product) {
      throw new Error(`Product ${productId} not found`);
    }

    return await this.client.search("products", {
      vector: product.vector as number[],
      limit: limit + 1, // +1 to exclude self
      with_payload: true,
      filter: {
        must_not: [{ has_id: [parseInt(productId)] }],
      },
    });
  }

  private averageVectors(vectors: number[][]): number[] {
    const dim = vectors[0].length;
    const avg = new Array(dim).fill(0);
    vectors.forEach(v => {
      v.forEach((val, i) => {
        avg[i] += val / vectors.length;
      });
    });
    return avg;
  }
}
```

### 4. Document Deduplication Pipeline

Detect and remove duplicate documents:

```typescript
class DeduplicationPipeline {
  private client: QdrantClient;
  private similarityThreshold: number;

  constructor(threshold = 0.95) {
    this.client = new QdrantClient({ url: process.env.QDRANT_URL });
    this.similarityThreshold = threshold;
  }

  // Find duplicates for a document (traced)
  async findDuplicates(docId: number, embedding: number[]) {
    const results = await this.client.search("documents", {
      vector: embedding,
      limit: 10,
      score_threshold: this.similarityThreshold,
      filter: {
        must_not: [{ has_id: [docId] }],
      },
    });

    return results.map(r => ({
      id: r.id,
      score: r.score,
      payload: r.payload,
    }));
  }

  // Scan collection for duplicates (traced)
  async scanForDuplicates(batchSize = 100) {
    const duplicateGroups: Map<number, number[]> = new Map();
    let offset: number | undefined;

    while (true) {
      // Scroll through collection (traced)
      const batch = await this.client.scroll("documents", {
        limit: batchSize,
        offset,
        with_vector: true,
        with_payload: true,
      });

      if (batch.points.length === 0) break;

      for (const point of batch.points) {
        const duplicates = await this.findDuplicates(
          point.id as number,
          point.vector as number[]
        );

        if (duplicates.length > 0) {
          duplicateGroups.set(
            point.id as number,
            duplicates.map(d => d.id as number)
          );
        }
      }

      offset = batch.next_page_offset as number | undefined;
      if (!offset) break;
    }

    return duplicateGroups;
  }

  // Remove duplicates, keeping originals (traced)
  async removeDuplicates(duplicateGroups: Map<number, number[]>) {
    const idsToRemove = new Set<number>();

    duplicateGroups.forEach((duplicates, original) => {
      duplicates.forEach(id => {
        if (id !== original) {
          idsToRemove.add(id);
        }
      });
    });

    if (idsToRemove.size > 0) {
      await this.client.delete("documents", {
        points: Array.from(idsToRemove),
      });
    }

    return idsToRemove.size;
  }
}
```

### 5. Semantic Caching for LLM Responses

Cache LLM responses based on semantic similarity:

```typescript
class SemanticCache {
  private client: QdrantClient;
  private ttlMs: number;
  private similarityThreshold: number;

  constructor(options: { ttlMs?: number; threshold?: number } = {}) {
    this.client = new QdrantClient({ url: process.env.QDRANT_URL });
    this.ttlMs = options.ttlMs || 3600000; // 1 hour default
    this.similarityThreshold = options.threshold || 0.95;
  }

  // Check cache for similar query (traced)
  async get(queryEmbedding: number[]): Promise<CacheResult | null> {
    const now = Date.now();

    const results = await this.client.search("llm_cache", {
      vector: queryEmbedding,
      limit: 1,
      score_threshold: this.similarityThreshold,
      filter: {
        must: [
          {
            key: "expires_at",
            range: { gt: now },
          },
        ],
      },
      with_payload: true,
    });

    if (results.length > 0) {
      const hit = results[0];
      return {
        response: hit.payload?.response as string,
        score: hit.score,
        cached_at: hit.payload?.cached_at as string,
      };
    }

    return null;
  }

  // Store response in cache (traced)
  async set(
    queryEmbedding: number[],
    query: string,
    response: string
  ) {
    const now = Date.now();

    await this.client.upsert("llm_cache", {
      points: [{
        id: now, // Use timestamp as ID
        vector: queryEmbedding,
        payload: {
          query,
          response,
          cached_at: new Date().toISOString(),
          expires_at: now + this.ttlMs,
        },
      }],
    });
  }

  // Clean expired entries (traced)
  async cleanup() {
    const now = Date.now();

    await this.client.delete("llm_cache", {
      filter: {
        must: [
          {
            key: "expires_at",
            range: { lt: now },
          },
        ],
      },
    });
  }

  // Get cache statistics (traced)
  async getStats() {
    const info = await this.client.getCollection("llm_cache");
    const count = await this.client.count("llm_cache", {
      filter: {
        must: [
          {
            key: "expires_at",
            range: { gt: Date.now() },
          },
        ],
      },
    });

    return {
      totalEntries: info.points_count,
      activeEntries: count.count,
      vectorDimension: info.config.params.vectors?.size,
    };
  }
}
```

## Docker Compose Setup

Run Qdrant locally with Docker:

```yaml
version: '3.8'
services:
  qdrant:
    image: qdrant/qdrant:latest
    ports:
      - "6333:6333"
      - "6334:6334"
    volumes:
      - qdrant_storage:/qdrant/storage
    environment:
      - QDRANT__SERVICE__GRPC_PORT=6334

volumes:
  qdrant_storage:
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `QDRANT_URL` | Qdrant server URL (e.g., `http://localhost:6333`) |
| `QDRANT_API_KEY` | API key for Qdrant Cloud |
| `FI_API_KEY` | Your Future AGI API key |
| `FI_PROJECT_NAME` | Your project name |

## Troubleshooting

### Connection Issues

Verify Qdrant is running and accessible:

```typescript
const client = new QdrantClient({ url: "http://localhost:6333" });

try {
  const collections = await client.getCollections();
  console.log("Connected! Collections:", collections);
} catch (error) {
  console.error("Connection failed:", error);
}
```

### Performance Tuning

For high-throughput scenarios:

```typescript
// Use batched upserts
const batchSize = 100;
for (let i = 0; i < points.length; i += batchSize) {
  await client.upsert("collection", {
    points: points.slice(i, i + batchSize),
    wait: false, // Don't wait for indexing
  });
}

// Use scroll for large result sets instead of search
const allResults = [];
let offset;
do {
  const batch = await client.scroll("collection", {
    limit: 100,
    offset,
    with_payload: true,
  });
  allResults.push(...batch.points);
  offset = batch.next_page_offset;
} while (offset);
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
- [Qdrant Documentation](https://qdrant.tech/documentation/)
- [OpenTelemetry](https://opentelemetry.io)
- [GitHub Issues](https://github.com/future-agi/traceAI/issues)
