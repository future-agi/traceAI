# @traceai/lancedb

OpenTelemetry instrumentation for [LanceDB](https://lancedb.com/) vector database in Node.js/TypeScript applications.

## Installation

```bash
npm install @traceai/lancedb
# or
pnpm add @traceai/lancedb
# or
yarn add @traceai/lancedb
```

## Prerequisites

- Node.js >= 18
- LanceDB (`@lancedb/lancedb` >= 0.1.0)
- OpenTelemetry SDK configured in your application

## Quick Start

```typescript
import { LanceDBInstrumentation } from "@traceai/lancedb";
import * as lancedb from "@lancedb/lancedb";

// Initialize instrumentation
const instrumentation = new LanceDBInstrumentation({
  traceConfig: {
    maskInputs: false,
    maskOutputs: false,
  },
});

// Enable instrumentation
instrumentation.enable();
instrumentation.manuallyInstrument(lancedb);

// Now all LanceDB operations will be traced
const db = await lancedb.connect("./my-database");
const table = await db.openTable("vectors");

// This search will be automatically traced
const results = await table.search([0.1, 0.2, 0.3]).limit(10).toArray();
```

## Configuration Options

```typescript
interface LanceDBInstrumentationConfig {
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

The instrumentation automatically traces the following LanceDB operations:

### Table Operations

- `search` - Vector similarity search
- `add` - Add records to table
- `update` - Update existing records
- `delete` - Delete records

### Database Operations

- `createTable` - Create new table
- `dropTable` - Drop table
- `openTable` - Open existing table
- `tableNames` - List all tables

### Utility Operations

- `countRows` - Count rows in table

## Span Attributes

Each traced operation includes relevant attributes:

| Attribute            | Description                              |
| -------------------- | ---------------------------------------- |
| `db.system`          | Always "lancedb"                         |
| `db.operation`       | Operation name (e.g., "search", "add")   |
| `db.collection.name` | Table name                               |
| `db.lancedb.limit`   | Query limit                              |
| `db.lancedb.metric`  | Distance metric (L2, cosine, dot)        |
| `db.lancedb.nprobes` | Number of probes for approximate search  |

## Real-World Use Cases

### 1. Local-First RAG Application

```typescript
import { LanceDBInstrumentation } from "@traceai/lancedb";
import * as lancedb from "@lancedb/lancedb";
import { pipeline } from "@xenova/transformers";

const instrumentation = new LanceDBInstrumentation();
instrumentation.enable();
instrumentation.manuallyInstrument(lancedb);

// Initialize embedding model
const embedder = await pipeline("feature-extraction", "Xenova/all-MiniLM-L6-v2");

async function embedText(text: string): Promise<number[]> {
  const output = await embedder(text, { pooling: "mean", normalize: true });
  return Array.from(output.data);
}

// Setup local database
const db = await lancedb.connect("./knowledge-base");

async function ingestDocuments(documents: { id: string; content: string }[]) {
  const records = await Promise.all(
    documents.map(async (doc) => ({
      id: doc.id,
      content: doc.content,
      vector: await embedText(doc.content),
      timestamp: new Date().toISOString(),
    }))
  );

  // Create or append to table (traced)
  try {
    const table = await db.openTable("documents");
    await table.add(records);
  } catch {
    await db.createTable("documents", records);
  }
}

async function searchDocuments(query: string, limit = 5) {
  const queryVector = await embedText(query);
  const table = await db.openTable("documents");

  // Vector search (traced)
  const results = await table
    .search(queryVector)
    .limit(limit)
    .select(["id", "content"])
    .toArray();

  return results;
}
```

### 2. Embedded Vector Search for Desktop Apps

```typescript
import * as lancedb from "@lancedb/lancedb";
import { app } from "electron";
import path from "path";

// Store database in user's app data directory
const dbPath = path.join(app.getPath("userData"), "vectors.lance");
const db = await lancedb.connect(dbPath);

async function indexLocalFiles(files: FileMetadata[]) {
  const records = files.map((file) => ({
    path: file.path,
    name: file.name,
    vector: file.embedding,
    size: file.size,
    modified: file.modifiedTime,
  }));

  const table = await db.createTable("files", records, { mode: "overwrite" });
  return table.countRows();
}

async function searchFiles(queryVector: number[], filters?: FileFilters) {
  const table = await db.openTable("files");

  let query = table.search(queryVector).limit(20);

  if (filters?.minSize) {
    query = query.where(`size >= ${filters.minSize}`);
  }

  return query.toArray();
}
```

### 3. Multi-modal Search (Images + Text)

```typescript
interface MediaRecord {
  id: string;
  type: "image" | "video" | "document";
  path: string;
  vector: number[];
  thumbnail?: string;
  metadata: Record<string, any>;
}

async function createMediaIndex(media: MediaRecord[]) {
  const table = await db.createTable("media", media, { mode: "overwrite" });

  // Create vector index for faster search
  await table.createIndex("vector", {
    type: "IVF_PQ",
    num_partitions: 256,
    num_sub_vectors: 96,
  });

  return table;
}

async function searchMedia(
  queryVector: number[],
  mediaType?: "image" | "video" | "document"
) {
  const table = await db.openTable("media");

  let query = table.search(queryVector).limit(50).select(["id", "type", "path", "thumbnail"]);

  if (mediaType) {
    query = query.where(`type = '${mediaType}'`);
  }

  return query.toArray();
}
```

### 4. Time-Series Anomaly Detection

```typescript
async function detectAnomalies(
  currentMetricVector: number[],
  windowHours = 24
) {
  const table = await db.openTable("metrics");
  const cutoffTime = new Date(Date.now() - windowHours * 60 * 60 * 1000);

  // Find similar historical patterns
  const similar = await table
    .search(currentMetricVector)
    .where(`timestamp >= '${cutoffTime.toISOString()}'`)
    .limit(100)
    .toArray();

  // Calculate anomaly score based on distance
  const avgDistance =
    similar.reduce((sum, r) => sum + r._distance, 0) / similar.length;
  const isAnomaly = avgDistance > 0.5; // threshold

  return {
    isAnomaly,
    score: avgDistance,
    similarPatterns: similar.slice(0, 5),
  };
}
```

### 5. Semantic Code Search

```typescript
interface CodeSnippet {
  id: string;
  filepath: string;
  content: string;
  language: string;
  vector: number[];
  functions: string[];
}

async function indexCodebase(snippets: CodeSnippet[]) {
  await db.createTable("code", snippets, { mode: "overwrite" });
}

async function searchCode(
  query: string,
  queryVector: number[],
  language?: string
) {
  const table = await db.openTable("code");

  let search = table
    .search(queryVector)
    .limit(20)
    .select(["filepath", "content", "language", "functions"]);

  if (language) {
    search = search.where(`language = '${language}'`);
  }

  const results = await search.toArray();

  return results.map((r) => ({
    ...r,
    preview: r.content.substring(0, 200) + "...",
  }));
}
```

### 6. Incremental Updates with Versioning

```typescript
async function updateWithVersioning(
  tableName: string,
  updates: { id: string; vector: number[]; data: any }[]
) {
  const table = await db.openTable(tableName);

  // Get current version
  const currentVersion = await table
    .search(updates[0].vector)
    .limit(1)
    .select(["version"])
    .toArray();

  const newVersion = (currentVersion[0]?.version ?? 0) + 1;

  // Add new records with version
  const records = updates.map((u) => ({
    ...u.data,
    id: u.id,
    vector: u.vector,
    version: newVersion,
    updatedAt: new Date().toISOString(),
  }));

  await table.add(records);

  // Optionally clean up old versions
  await table.delete(`version < ${newVersion - 5}`);

  return newVersion;
}
```

## Integration with OpenTelemetry

```typescript
import { NodeSDK } from "@opentelemetry/sdk-node";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { LanceDBInstrumentation } from "@traceai/lancedb";

const sdk = new NodeSDK({
  traceExporter: new OTLPTraceExporter({
    url: "http://localhost:4318/v1/traces",
  }),
  instrumentations: [new LanceDBInstrumentation()],
});

sdk.start();
```

## Why LanceDB?

- **Embedded**: No server required, runs in-process
- **Fast**: Written in Rust with zero-copy access
- **Portable**: Works on desktop, mobile, and edge devices
- **Versioned**: Built on Lance format with automatic versioning
- **Cost-effective**: No infrastructure costs for development/testing

## License

Apache-2.0
