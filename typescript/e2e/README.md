# TraceAI Vector DB E2E Tests

End-to-end integration tests for TraceAI vector database instrumentations (ChromaDB, Qdrant).

## Prerequisites

- Docker and Docker Compose installed
- Node.js 18+ and pnpm

## Quick Start

### 1. Start the vector database services

```bash
# From the e2e directory
docker-compose up -d

# Or from the root typescript directory
pnpm run e2e:docker:up
```

### 2. Wait for services to be ready

- ChromaDB: http://localhost:8000
- Qdrant: http://localhost:6333

You can check the health of the services:

```bash
# ChromaDB heartbeat
curl http://localhost:8000/api/v1/heartbeat

# Qdrant health
curl http://localhost:6333/
```

### 3. Run the E2E tests

```bash
# From the e2e directory
pnpm test

# Or from the root typescript directory
pnpm run test:e2e

# Run specific test suite
pnpm run test:e2e:chromadb
pnpm run test:e2e:qdrant
```

### 4. Stop the services

```bash
# From the e2e directory
docker-compose down

# Or from the root typescript directory
pnpm run e2e:docker:down
```

## Test Structure

### ChromaDB E2E Tests (`chromadb.e2e.test.ts`)

Tests the `@traceai/chromadb` instrumentation against a real ChromaDB instance:

- **Collection Operations**: Create, get, list collections
- **Document Operations**: Add, query, get, update, upsert, delete, count, peek
- **Real-World Scenarios**:
  - RAG (Retrieval-Augmented Generation) pipeline
  - Chatbot memory storage and retrieval
  - Document similarity search with metadata filtering

### Qdrant E2E Tests (`qdrant.e2e.test.ts`)

Tests the `@traceai/qdrant` instrumentation against a real Qdrant instance:

- **Collection Operations**: Get collection info, list collections
- **Point Operations**: Upsert, search, retrieve, scroll, count, delete
- **Real-World Scenarios**:
  - Self-hosted RAG pipeline
  - E-commerce product similarity search with filtering
  - Image similarity search
  - Recommendation engine

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CHROMADB_URL` | `http://localhost:8000` | ChromaDB server URL |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant server URL |

### Docker Compose Services

The `docker-compose.yml` provides:

- **ChromaDB**: Latest ChromaDB image on port 8000
- **Qdrant**: Latest Qdrant image on ports 6333 (HTTP) and 6334 (gRPC)

## Test Utilities

The `setup.ts` file provides common utilities:

- `getCapturedSpans()`: Get all captured OpenTelemetry spans
- `clearCapturedSpans()`: Reset captured spans before each test
- `findSpanByName(name)`: Find a span by exact name
- `findSpansByPrefix(prefix)`: Find spans by name prefix
- `getSpanAttributes(span)`: Get span attributes
- `generateRandomEmbedding(dimensions)`: Generate random embeddings for testing
- `waitForService(checkFn, maxAttempts, intervalMs)`: Wait for a service to be ready

## Pinecone E2E Tests

Pinecone is a managed cloud service and requires an API key. To run Pinecone E2E tests:

1. Set environment variables:
   ```bash
   export PINECONE_API_KEY=your-api-key
   export PINECONE_INDEX=your-index-name
   ```

2. Create a Pinecone index with the appropriate dimensions (384 for test embeddings)

3. Run the Pinecone-specific tests

Note: Pinecone tests are not included in the default test suite to avoid requiring cloud credentials.

## Troubleshooting

### Services not starting

Check Docker logs:
```bash
docker-compose logs chromadb
docker-compose logs qdrant
```

### Tests timing out

The tests have a 60-second timeout. If services are slow to respond:
1. Increase `testTimeout` in `jest.config.js`
2. Check service health endpoints
3. Ensure Docker has sufficient resources

### Connection refused

Ensure Docker containers are running and ports are exposed:
```bash
docker-compose ps
```

## Writing New E2E Tests

1. Import the instrumentation and enable it before tests
2. Use `clearCapturedSpans()` in `beforeEach` to isolate tests
3. Use `getCapturedSpans()` to verify traces were captured
4. Use `getSpanAttributes()` to verify span attributes
5. Include real-world scenario tests that demonstrate typical usage patterns
