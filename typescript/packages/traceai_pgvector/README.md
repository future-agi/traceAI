# @traceai/pgvector

OpenTelemetry instrumentation for [pgvector](https://github.com/pgvector/pgvector) - the open-source vector similarity search extension for PostgreSQL.

## Installation

```bash
npm install @traceai/pgvector
# or
pnpm add @traceai/pgvector
# or
yarn add @traceai/pgvector
```

## Prerequisites

- Node.js >= 18
- PostgreSQL driver (`pg` >= 8.0.0)
- PostgreSQL with pgvector extension installed
- OpenTelemetry SDK configured in your application

## Quick Start

```typescript
import { PgVectorInstrumentation } from "@traceai/pgvector";
import { Pool } from "pg";

// Initialize instrumentation
const instrumentation = new PgVectorInstrumentation({
  traceConfig: {
    maskInputs: false,
    maskOutputs: false,
  },
});

// Enable instrumentation
instrumentation.enable();

// Manually instrument the pg module
import * as pg from "pg";
instrumentation.manuallyInstrument(pg);

// Now all PostgreSQL queries will be traced with vector search detection
const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
});

// Vector similarity search (traced with vector-specific attributes)
const result = await pool.query(
  `SELECT id, content, embedding <-> $1 AS distance
   FROM documents
   ORDER BY embedding <-> $1
   LIMIT 10`,
  [[0.1, 0.2, 0.3, ...]]
);
```

## Configuration Options

```typescript
interface PgVectorInstrumentationConfig {
  // Enable/disable the instrumentation
  enabled?: boolean;

  // Capture SQL statements in spans
  captureStatements?: boolean;

  // Capture query parameters
  captureParameters?: boolean;
}

interface TraceConfigOptions {
  // Mask sensitive input data
  maskInputs?: boolean;

  // Mask sensitive output data
  maskOutputs?: boolean;
}
```

## Traced Operations

The instrumentation traces all PostgreSQL queries with special detection for pgvector operations:

### Vector Distance Operators

- `<->` - L2 (Euclidean) distance
- `<#>` - Negative inner product
- `<=>` - Cosine distance

### Standard Operations

- `SELECT` - Read queries
- `INSERT` - Insert operations
- `UPDATE` - Update operations
- `DELETE` - Delete operations

## Span Attributes

Each traced operation includes relevant attributes:

| Attribute                    | Description                                    |
| ---------------------------- | ---------------------------------------------- |
| `db.system`                  | Always "postgresql"                            |
| `db.operation`               | SQL operation (SELECT, INSERT, etc.)           |
| `db.statement`               | SQL query (if captureStatements enabled)       |
| `db.pgvector.is_vector_query`| true if vector operators detected              |
| `db.pgvector.distance_metric`| Distance metric (l2, cosine, inner_product)    |
| `db.pgvector.dimensions`     | Vector dimensions (when detectable)            |

## Real-World Use Cases

### 1. Semantic Search API

```typescript
import { PgVectorInstrumentation } from "@traceai/pgvector";
import { Pool } from "pg";
import OpenAI from "openai";

const instrumentation = new PgVectorInstrumentation();
instrumentation.enable();

const pool = new Pool({ connectionString: process.env.DATABASE_URL });
const openai = new OpenAI();

async function semanticSearch(query: string, limit = 10) {
  // Generate embedding
  const embeddingResponse = await openai.embeddings.create({
    model: "text-embedding-3-small",
    input: query,
  });
  const queryVector = embeddingResponse.data[0].embedding;

  // Vector search with cosine similarity (traced)
  const result = await pool.query(
    `SELECT
       id,
       title,
       content,
       1 - (embedding <=> $1) AS similarity
     FROM articles
     WHERE 1 - (embedding <=> $1) > 0.7
     ORDER BY embedding <=> $1
     LIMIT $2`,
    [JSON.stringify(queryVector), limit]
  );

  return result.rows;
}
```

### 2. Hybrid Search with Full-Text

```typescript
async function hybridSearch(
  queryVector: number[],
  textQuery: string,
  weights = { vector: 0.7, text: 0.3 }
) {
  // Combine vector similarity with full-text search (traced)
  const result = await pool.query(
    `WITH vector_results AS (
       SELECT
         id,
         1 - (embedding <=> $1) AS vector_score
       FROM products
       ORDER BY embedding <=> $1
       LIMIT 100
     ),
     text_results AS (
       SELECT
         id,
         ts_rank(search_vector, plainto_tsquery($2)) AS text_score
       FROM products
       WHERE search_vector @@ plainto_tsquery($2)
     )
     SELECT
       p.id,
       p.name,
       p.description,
       p.price,
       COALESCE(v.vector_score, 0) * $3 + COALESCE(t.text_score, 0) * $4 AS combined_score
     FROM products p
     LEFT JOIN vector_results v ON p.id = v.id
     LEFT JOIN text_results t ON p.id = t.id
     WHERE v.id IS NOT NULL OR t.id IS NOT NULL
     ORDER BY combined_score DESC
     LIMIT 20`,
    [JSON.stringify(queryVector), textQuery, weights.vector, weights.text]
  );

  return result.rows;
}
```

### 3. K-Nearest Neighbors with Filtering

```typescript
async function findSimilarProducts(
  productId: number,
  filters: {
    category?: string;
    minPrice?: number;
    maxPrice?: number;
    inStock?: boolean;
  },
  limit = 10
) {
  // Build dynamic WHERE clause
  const conditions: string[] = ["id != $1"];
  const params: any[] = [productId];
  let paramIndex = 2;

  if (filters.category) {
    conditions.push(`category = $${paramIndex++}`);
    params.push(filters.category);
  }
  if (filters.minPrice !== undefined) {
    conditions.push(`price >= $${paramIndex++}`);
    params.push(filters.minPrice);
  }
  if (filters.maxPrice !== undefined) {
    conditions.push(`price <= $${paramIndex++}`);
    params.push(filters.maxPrice);
  }
  if (filters.inStock !== undefined) {
    conditions.push(`in_stock = $${paramIndex++}`);
    params.push(filters.inStock);
  }

  // Get source product embedding and find similar (traced)
  const result = await pool.query(
    `WITH source AS (
       SELECT embedding FROM products WHERE id = $1
     )
     SELECT
       p.id,
       p.name,
       p.price,
       p.category,
       p.embedding <-> s.embedding AS distance
     FROM products p, source s
     WHERE ${conditions.join(" AND ")}
     ORDER BY p.embedding <-> s.embedding
     LIMIT $${paramIndex}`,
    [...params, limit]
  );

  return result.rows;
}
```

### 4. Batch Vector Upsert

```typescript
async function upsertEmbeddings(
  items: { id: number; content: string; embedding: number[] }[]
) {
  const client = await pool.connect();

  try {
    await client.query("BEGIN");

    // Use COPY for high-performance bulk insert
    for (const item of items) {
      await client.query(
        `INSERT INTO documents (id, content, embedding)
         VALUES ($1, $2, $3)
         ON CONFLICT (id) DO UPDATE
         SET content = EXCLUDED.content,
             embedding = EXCLUDED.embedding,
             updated_at = NOW()`,
        [item.id, item.content, JSON.stringify(item.embedding)]
      );
    }

    await client.query("COMMIT");
    return items.length;
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}
```

### 5. Multi-tenant Vector Search

```typescript
async function tenantVectorSearch(
  tenantId: string,
  queryVector: number[],
  limit = 20
) {
  // Row-level security ensures tenant isolation
  // Vector search within tenant partition (traced)
  const result = await pool.query(
    `SELECT
       id,
       title,
       content,
       embedding <=> $2 AS distance
     FROM tenant_documents
     WHERE tenant_id = $1
     ORDER BY embedding <=> $2
     LIMIT $3`,
    [tenantId, JSON.stringify(queryVector), limit]
  );

  return result.rows;
}
```

### 6. Approximate Nearest Neighbor with HNSW Index

```typescript
// First, create an HNSW index for faster approximate search
async function createHNSWIndex() {
  await pool.query(`
    CREATE INDEX IF NOT EXISTS documents_embedding_hnsw_idx
    ON documents
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64)
  `);
}

// Then use it for fast approximate search
async function fastVectorSearch(queryVector: number[], limit = 10) {
  // Set search parameters for quality/speed tradeoff
  await pool.query("SET hnsw.ef_search = 100");

  // ANN search using HNSW index (traced)
  const result = await pool.query(
    `SELECT
       id,
       content,
       1 - (embedding <=> $1) AS similarity
     FROM documents
     ORDER BY embedding <=> $1
     LIMIT $2`,
    [JSON.stringify(queryVector), limit]
  );

  return result.rows;
}
```

### 7. Real-time Analytics with Vector Clustering

```typescript
async function clusterAnalytics(centroidVectors: number[][]) {
  // Assign each document to nearest cluster (traced)
  const result = await pool.query(
    `WITH centroids AS (
       SELECT
         row_number() OVER () AS cluster_id,
         centroid::vector AS centroid
       FROM unnest($1::text[]) AS centroid
     )
     SELECT
       c.cluster_id,
       COUNT(*) AS document_count,
       AVG(d.embedding <-> c.centroid) AS avg_distance
     FROM documents d
     CROSS JOIN LATERAL (
       SELECT cluster_id, centroid
       FROM centroids
       ORDER BY d.embedding <-> centroid
       LIMIT 1
     ) c
     GROUP BY c.cluster_id
     ORDER BY c.cluster_id`,
    [centroidVectors.map((v) => JSON.stringify(v))]
  );

  return result.rows;
}
```

## Database Setup

```sql
-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Create table with vector column
CREATE TABLE documents (
  id SERIAL PRIMARY KEY,
  content TEXT NOT NULL,
  embedding vector(1536), -- dimensions must match your embedding model
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
);

-- Create indexes for different distance metrics

-- HNSW index for cosine similarity (recommended for most use cases)
CREATE INDEX documents_embedding_cosine_idx
ON documents USING hnsw (embedding vector_cosine_ops);

-- IVFFlat index for L2 distance (faster to build, uses less memory)
CREATE INDEX documents_embedding_l2_idx
ON documents USING ivfflat (embedding vector_l2_ops)
WITH (lists = 100);
```

## Integration with OpenTelemetry

```typescript
import { NodeSDK } from "@opentelemetry/sdk-node";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { PgVectorInstrumentation } from "@traceai/pgvector";

const sdk = new NodeSDK({
  traceExporter: new OTLPTraceExporter({
    url: "http://localhost:4318/v1/traces",
  }),
  instrumentations: [new PgVectorInstrumentation()],
});

sdk.start();
```

## Performance Tips

1. **Use HNSW indexes** for production workloads - faster queries with minimal accuracy loss
2. **Tune `ef_search`** parameter based on your recall requirements
3. **Partition large tables** by tenant or time for better performance
4. **Use connection pooling** (PgBouncer or built-in pg Pool)
5. **Consider quantization** for very large datasets

## License

Apache-2.0
