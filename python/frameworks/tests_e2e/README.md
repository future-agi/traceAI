# End-to-End Integration Tests for Vector Database Instrumentation

This directory contains E2E integration tests that verify the instrumentation
works correctly with real database instances.

## Quick Start

### For Embedded Databases (No Setup Required)

ChromaDB and LanceDB run in-memory and don't require external setup:

```bash
# Install dependencies
pip install chromadb lancedb

# Run tests
pytest tests_e2e/test_chromadb_e2e.py -v
pytest tests_e2e/test_lancedb_e2e.py -v
```

### For External Databases

1. **Start the databases using Docker Compose:**

```bash
cd tests_e2e
docker-compose up -d
```

2. **Wait for databases to be ready** (about 30 seconds)

3. **Run the integration tests:**

```bash
# Run all integration tests
pytest tests_e2e/ -v -m integration

# Run specific database tests
pytest tests_e2e/test_qdrant_e2e.py -v
pytest tests_e2e/test_milvus_e2e.py -v
pytest tests_e2e/test_pgvector_e2e.py -v
pytest tests_e2e/test_redis_e2e.py -v
```

4. **Stop the databases:**

```bash
docker-compose down
```

## Database Connection Details

| Database | Port | Connection String |
|----------|------|-------------------|
| Qdrant | 6333 | `http://localhost:6333` |
| Weaviate | 8080 | `http://localhost:8080` |
| Milvus | 19530 | `localhost:19530` |
| PostgreSQL | 5432 | `postgresql://postgres:postgres@localhost:5432/vectordb` |
| Redis | 6379 | `redis://localhost:6379` |

## Test Markers

Tests are marked with database-specific markers for selective execution:

```bash
# Run only ChromaDB tests
pytest tests_e2e/ -m chromadb

# Run only Qdrant tests
pytest tests_e2e/ -m qdrant

# Run all integration tests
pytest tests_e2e/ -m integration
```

## Environment Variables

For cloud-hosted databases, set these environment variables:

```bash
# Pinecone
export PINECONE_API_KEY=your-api-key

# MongoDB Atlas
export MONGODB_URI=mongodb+srv://...

# Weaviate Cloud
export WEAVIATE_URL=https://your-cluster.weaviate.cloud
export WEAVIATE_API_KEY=your-api-key

# OpenAI (for text2vec-openai in Weaviate)
export OPENAI_API_KEY=your-openai-key
```

## Test Structure

Each test file follows this pattern:

```python
class TestDatabaseE2E:
    def test_full_workflow(self):
        """Tests: create, add, query, delete"""
        pass

    def test_error_handling(self):
        """Tests: error capture in spans"""
        pass

    def test_batch_operations(self):
        """Tests: batch insert/query"""
        pass

    def test_filtered_search(self):
        """Tests: search with metadata filters"""
        pass
```

## Span Verification

Tests verify that:

1. Spans are created for each operation
2. Span names follow the pattern: `{db.system} {operation}`
3. Required attributes are set:
   - `db.system` - Database identifier
   - `db.operation.name` - Operation type
   - `db.vector.query.top_k` - For search operations
   - `db.vector.results.count` - For result sets
   - `db.vector.upsert.count` - For insert operations

## Troubleshooting

### Docker Issues

```bash
# Check container status
docker-compose ps

# View logs
docker-compose logs milvus
docker-compose logs qdrant

# Restart a specific service
docker-compose restart milvus
```

### Test Failures

1. **Connection refused**: Database not started or not ready yet
2. **Import errors**: Install the client library (`pip install qdrant-client`)
3. **Authentication errors**: Check API keys for cloud services

### Cleanup

```bash
# Remove all containers and volumes
docker-compose down -v
```
