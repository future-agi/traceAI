# @traceai/pinecone

OpenTelemetry instrumentation for [Pinecone](https://www.pinecone.io/) - the serverless vector database for high-performance AI applications.

## Overview

This package provides automatic tracing for Pinecone operations in Node.js applications, enabling full observability of your vector database interactions in production AI systems, RAG pipelines, and semantic search applications.

## Installation

```bash
npm install @traceai/pinecone
# or
yarn add @traceai/pinecone
# or
pnpm add @traceai/pinecone
```

## Quick Start

### Basic Setup

```typescript
import { PineconeInstrumentation } from "@traceai/pinecone";
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

// Register Pinecone instrumentation
registerInstrumentations({
  instrumentations: [
    new PineconeInstrumentation(),
  ],
});

// Now use Pinecone as normal - all operations are traced
import { Pinecone } from "@pinecone-database/pinecone";

const pinecone = new Pinecone({ apiKey: process.env.PINECONE_API_KEY });
const index = pinecone.index("my-index");

// This operation will be automatically traced
const results = await index.query({
  vector: [0.1, 0.2, 0.3, /* ... */],
  topK: 10,
  includeMetadata: true,
});
```

### Manual Instrumentation

```typescript
import { PineconeInstrumentation } from "@traceai/pinecone";
import * as pinecone from "@pinecone-database/pinecone";

const instrumentation = new PineconeInstrumentation();
instrumentation.manuallyInstrument(pinecone);
```

## Configuration Options

```typescript
interface PineconeInstrumentationConfig {
  // Whether to capture query vectors in spans (may be large)
  captureQueryVectors?: boolean;

  // Whether to capture result vectors in spans (may be large)
  captureResultVectors?: boolean;
}

// Example with all options
const instrumentation = new PineconeInstrumentation({
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
| `query` | `pinecone query` | Semantic similarity search |
| `upsert` | `pinecone upsert` | Insert or update vectors |
| `fetch` | `pinecone fetch` | Retrieve vectors by ID |
| `update` | `pinecone update` | Update vector metadata |
| `deleteOne` | `pinecone delete` | Delete single vector |
| `deleteMany` | `pinecone delete_many` | Delete multiple vectors |
| `deleteAll` | `pinecone delete_all` | Delete all vectors in namespace |
| `listPaginated` | `pinecone list` | List vectors with pagination |
| `describeIndexStats` | `pinecone describe_stats` | Get index statistics |

## Span Attributes

Each span includes these semantic convention attributes:

| Attribute | Description | Example |
|-----------|-------------|---------|
| `db.system` | Database system identifier | `pinecone` |
| `db.operation.name` | Operation being performed | `query`, `upsert` |
| `db.vector.index.name` | Index name | `my-index` |
| `db.vector.namespace` | Namespace (if used) | `documents` |
| `db.vector.query.top_k` | Number of results requested | `10` |
| `db.vector.query.filter` | Query filter (JSON) | `{"category": "docs"}` |
| `db.vector.query.include_metadata` | Whether metadata included | `true` |
| `db.vector.query.include_vectors` | Whether vectors included | `false` |
| `db.vector.results.count` | Number of results returned | `10` |
| `db.vector.upsert.count` | Number of vectors upserted | `100` |
| `db.vector.upsert.dimensions` | Vector dimensions | `1536` |
| `db.vector.delete.count` | Number of vectors deleted | `50` |
| `db.vector.delete.all` | Whether all deleted | `true` |
| `db.vector.index.dimensions` | Index dimensions | `1536` |
| `fi.span.kind` | TraceAI span kind | `VECTOR_DB` |

## Real-World Use Cases

### 1. Production RAG with Serverless Pinecone

High-scale retrieval-augmented generation:

```typescript
import { Pinecone } from "@pinecone-database/pinecone";
import OpenAI from "openai";

const pinecone = new Pinecone();
const openai = new OpenAI();

const index = pinecone.index("knowledge-base");

// Ingest documents with namespace isolation
async function ingestDocuments(documents: Document[], namespace: string) {
  const batchSize = 100;

  for (let i = 0; i < documents.length; i += batchSize) {
    const batch = documents.slice(i, i + batchSize);

    // Generate embeddings
    const embeddings = await openai.embeddings.create({
      model: "text-embedding-3-large",
      input: batch.map(d => d.content),
      dimensions: 1536,
    });

    // Upsert to Pinecone (traced automatically)
    await index.namespace(namespace).upsert(
      batch.map((doc, j) => ({
        id: doc.id,
        values: embeddings.data[j].embedding,
        metadata: {
          title: doc.title,
          source: doc.source,
          created_at: doc.createdAt,
          chunk_index: doc.chunkIndex,
        },
      }))
    );
  }
}

// RAG query with hybrid search (traced automatically)
async function ragQuery(question: string, namespace: string, filters?: object) {
  // Generate query embedding
  const queryEmbedding = await openai.embeddings.create({
    model: "text-embedding-3-large",
    input: question,
    dimensions: 1536,
  });

  // Semantic search with metadata filtering
  const results = await index.namespace(namespace).query({
    vector: queryEmbedding.data[0].embedding,
    topK: 5,
    includeMetadata: true,
    filter: filters,
  });

  // Build context from results
  const context = results.matches
    .map(m => m.metadata?.content)
    .filter(Boolean)
    .join("\n\n");

  // Generate response
  const response = await openai.chat.completions.create({
    model: "gpt-4-turbo",
    messages: [
      {
        role: "system",
        content: `Answer based on this context:\n${context}\n\nIf the context doesn't contain relevant information, say so.`,
      },
      { role: "user", content: question },
    ],
  });

  return {
    answer: response.choices[0].message.content,
    sources: results.matches.map(m => ({
      id: m.id,
      score: m.score,
      title: m.metadata?.title,
    })),
  };
}
```

### 2. Multi-Tenant SaaS Application

Isolate customer data using namespaces:

```typescript
class MultiTenantVectorStore {
  private index: any;

  constructor(indexName: string) {
    const pinecone = new Pinecone();
    this.index = pinecone.index(indexName);
  }

  // Store customer data in isolated namespace (traced)
  async storeForTenant(tenantId: string, documents: any[]) {
    const ns = this.index.namespace(`tenant_${tenantId}`);

    await ns.upsert(documents.map(doc => ({
      id: `${tenantId}_${doc.id}`,
      values: doc.embedding,
      metadata: {
        tenant_id: tenantId,
        ...doc.metadata,
      },
    })));
  }

  // Query within tenant namespace only (traced)
  async queryForTenant(tenantId: string, queryVector: number[], options: any = {}) {
    const ns = this.index.namespace(`tenant_${tenantId}`);

    return await ns.query({
      vector: queryVector,
      topK: options.topK || 10,
      includeMetadata: true,
      filter: options.filter,
    });
  }

  // Get tenant usage statistics (traced)
  async getTenantStats(tenantId: string) {
    const stats = await this.index.describeIndexStats();
    const tenantNamespace = `tenant_${tenantId}`;

    return {
      vectorCount: stats.namespaces[tenantNamespace]?.recordCount || 0,
      totalIndexVectors: stats.totalRecordCount,
    };
  }

  // Clean up tenant data on deletion (traced)
  async deleteTenant(tenantId: string) {
    const ns = this.index.namespace(`tenant_${tenantId}`);
    await ns.deleteAll();
  }
}

// Usage
const vectorStore = new MultiTenantVectorStore("saas-production");

// Store data for tenant
await vectorStore.storeForTenant("acme-corp", customerDocuments);

// Query within tenant isolation
const results = await vectorStore.queryForTenant("acme-corp", queryVector, {
  topK: 5,
  filter: { document_type: "contract" },
});
```

### 3. Real-Time Recommendation Engine

Product recommendations with hybrid filtering:

```typescript
class RecommendationEngine {
  private index: any;

  constructor() {
    const pinecone = new Pinecone();
    this.index = pinecone.index("product-embeddings");
  }

  // Index product catalog (traced)
  async indexProducts(products: Product[]) {
    const batches = this.chunk(products, 100);

    for (const batch of batches) {
      await this.index.upsert(
        batch.map(p => ({
          id: p.id,
          values: p.embedding,
          metadata: {
            name: p.name,
            category: p.category,
            brand: p.brand,
            price: p.price,
            rating: p.rating,
            in_stock: p.inStock,
            tags: p.tags,
          },
        }))
      );
    }
  }

  // Find similar products (traced)
  async findSimilar(productId: string, options: RecommendationOptions = {}) {
    // Fetch the source product embedding
    const { records } = await this.index.fetch([productId]);
    const sourceProduct = records[productId];

    if (!sourceProduct) {
      throw new Error(`Product ${productId} not found`);
    }

    // Query for similar products with filters
    const results = await this.index.query({
      vector: sourceProduct.values,
      topK: options.limit || 10,
      includeMetadata: true,
      filter: {
        $and: [
          // Exclude the source product
          { id: { $ne: productId } },
          // Only in-stock items
          { in_stock: { $eq: true } },
          // Optional category filter
          ...(options.sameCategory
            ? [{ category: { $eq: sourceProduct.metadata?.category } }]
            : []),
          // Price range filter
          ...(options.maxPrice
            ? [{ price: { $lte: options.maxPrice } }]
            : []),
        ],
      },
    });

    return results.matches.map(m => ({
      id: m.id,
      score: m.score,
      ...m.metadata,
    }));
  }

  // Personalized recommendations (traced)
  async getPersonalized(userEmbedding: number[], preferences: UserPreferences) {
    const results = await this.index.query({
      vector: userEmbedding,
      topK: 20,
      includeMetadata: true,
      filter: {
        $and: [
          { in_stock: { $eq: true } },
          { category: { $in: preferences.favoriteCategories } },
          { brand: { $in: preferences.preferredBrands } },
          { price: { $lte: preferences.maxBudget } },
          { rating: { $gte: 4.0 } },
        ],
      },
    });

    return results.matches;
  }

  private chunk<T>(array: T[], size: number): T[][] {
    return Array.from({ length: Math.ceil(array.length / size) }, (_, i) =>
      array.slice(i * size, i * size + size)
    );
  }
}
```

### 4. Semantic Code Search

Search code repositories by natural language:

```typescript
class CodeSearchEngine {
  private index: any;

  constructor() {
    const pinecone = new Pinecone();
    this.index = pinecone.index("code-embeddings");
  }

  // Index code files (traced)
  async indexRepository(repoId: string, files: CodeFile[]) {
    const ns = this.index.namespace(`repo_${repoId}`);

    // Chunk files into smaller pieces
    const chunks = files.flatMap(file =>
      this.chunkCode(file).map((chunk, i) => ({
        id: `${file.path}_chunk_${i}`,
        values: chunk.embedding,
        metadata: {
          repo_id: repoId,
          file_path: file.path,
          language: file.language,
          start_line: chunk.startLine,
          end_line: chunk.endLine,
          content: chunk.content,
          functions: chunk.functions,
        },
      }))
    );

    // Batch upsert
    for (let i = 0; i < chunks.length; i += 100) {
      await ns.upsert(chunks.slice(i, i + 100));
    }
  }

  // Natural language code search (traced)
  async search(repoId: string, query: string, options: SearchOptions = {}) {
    const ns = this.index.namespace(`repo_${repoId}`);
    const queryEmbedding = await this.getEmbedding(query);

    const filter: any = {};
    if (options.language) {
      filter.language = { $eq: options.language };
    }
    if (options.filePath) {
      filter.file_path = { $eq: options.filePath };
    }

    const results = await ns.query({
      vector: queryEmbedding,
      topK: options.limit || 10,
      includeMetadata: true,
      filter: Object.keys(filter).length > 0 ? filter : undefined,
    });

    return results.matches.map(m => ({
      filePath: m.metadata?.file_path,
      language: m.metadata?.language,
      content: m.metadata?.content,
      startLine: m.metadata?.start_line,
      endLine: m.metadata?.end_line,
      score: m.score,
    }));
  }

  // Find similar code patterns (traced)
  async findSimilarCode(codeSnippet: string, repoId: string) {
    const ns = this.index.namespace(`repo_${repoId}`);
    const codeEmbedding = await this.getEmbedding(codeSnippet);

    const results = await ns.query({
      vector: codeEmbedding,
      topK: 5,
      includeMetadata: true,
    });

    return results.matches;
  }

  private chunkCode(file: CodeFile) {
    // Implementation for chunking code files
    return [];
  }

  private async getEmbedding(text: string): Promise<number[]> {
    // Implementation for getting embeddings
    return [];
  }
}
```

### 5. Anomaly Detection Pipeline

Detect anomalies using vector similarity:

```typescript
class AnomalyDetector {
  private index: any;
  private threshold: number;

  constructor(threshold = 0.7) {
    const pinecone = new Pinecone();
    this.index = pinecone.index("behavior-patterns");
    this.threshold = threshold;
  }

  // Index normal behavior patterns (traced)
  async indexNormalPatterns(patterns: BehaviorPattern[]) {
    await this.index.namespace("normal").upsert(
      patterns.map(p => ({
        id: p.id,
        values: p.embedding,
        metadata: {
          pattern_type: p.type,
          frequency: p.frequency,
          timestamp: p.timestamp,
        },
      }))
    );
  }

  // Check if behavior is anomalous (traced)
  async detectAnomaly(behaviorEmbedding: number[]): Promise<AnomalyResult> {
    const results = await this.index.namespace("normal").query({
      vector: behaviorEmbedding,
      topK: 5,
      includeMetadata: true,
    });

    // If no close matches, it's anomalous
    const maxSimilarity = results.matches[0]?.score || 0;
    const isAnomalous = maxSimilarity < this.threshold;

    return {
      isAnomalous,
      confidence: isAnomalous ? 1 - maxSimilarity : maxSimilarity,
      closestPatterns: results.matches.slice(0, 3),
    };
  }

  // Batch anomaly detection (traced)
  async detectAnomaliesBatch(behaviors: number[][]): Promise<AnomalyResult[]> {
    return Promise.all(
      behaviors.map(b => this.detectAnomaly(b))
    );
  }
}
```

## Performance Tips

### Batch Operations

For high-throughput scenarios, batch your upserts:

```typescript
async function batchUpsert(index: any, vectors: any[], batchSize = 100) {
  for (let i = 0; i < vectors.length; i += batchSize) {
    const batch = vectors.slice(i, i + batchSize);
    await index.upsert(batch);  // Each batch creates a trace span
  }
}
```

### Use Namespaces for Isolation

Namespaces provide logical separation and can improve query performance:

```typescript
// Separate by data type
const docsIndex = index.namespace("documents");
const productsIndex = index.namespace("products");

// Or by tenant
const tenantIndex = index.namespace(`tenant_${tenantId}`);
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `PINECONE_API_KEY` | Your Pinecone API key |
| `FI_API_KEY` | Your Future AGI API key |
| `FI_PROJECT_NAME` | Your project name |

## Troubleshooting

### Connection Issues

Ensure your Pinecone API key is valid and the index exists:

```typescript
const pinecone = new Pinecone({ apiKey: process.env.PINECONE_API_KEY });
const indexes = await pinecone.listIndexes();
console.log("Available indexes:", indexes);
```

### Large Span Sizes

Disable vector capture for production:

```typescript
new PineconeInstrumentation({
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
- [Pinecone Documentation](https://docs.pinecone.io)
- [OpenTelemetry](https://opentelemetry.io)
- [GitHub Issues](https://github.com/future-agi/traceAI/issues)
