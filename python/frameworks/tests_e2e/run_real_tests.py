#!/usr/bin/env python3
"""
Real E2E Integration Tests for Vector Database Instrumentation

This script runs actual tests with real data against embedded vector databases.
No mocking - everything is real.
"""

import sys
import os
import tempfile
import shutil

# Add parent paths for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'chromadb'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lancedb'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'qdrant'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'weaviate'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'milvus'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'mongodb-vector'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'pgvector'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'redis-vector'))

# Docker service ports (can be overridden by environment variables)
MONGODB_PORT = int(os.environ.get("MONGODB_PORT", "37017"))
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "25432"))
REDIS_PORT = int(os.environ.get("REDIS_PORT", "26379"))

from opentelemetry import trace as trace_api
from opentelemetry.sdk import trace as trace_sdk
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Global tracer setup - only once
EXPORTER = InMemorySpanExporter()
RESOURCE = Resource(attributes={"service.name": "real-e2e-test"})
PROVIDER = trace_sdk.TracerProvider(resource=RESOURCE)
PROVIDER.add_span_processor(SimpleSpanProcessor(EXPORTER))
trace_api.set_tracer_provider(PROVIDER)


def generate_embedding(text: str, dim: int = 384) -> list[float]:
    """Generate deterministic embedding from text."""
    import hashlib
    hash_bytes = hashlib.sha256(text.encode()).digest()
    embedding = []
    for i in range(dim):
        byte_val = hash_bytes[i % len(hash_bytes)]
        embedding.append((byte_val / 255.0) * 2 - 1)
    return embedding


def print_spans(title: str):
    """Print captured spans."""
    spans = EXPORTER.get_finished_spans()
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")
    print(f"  Total spans captured: {len(spans)}")
    print()
    for span in spans:
        attrs = dict(span.attributes or {})
        status = "✓" if span.status.is_ok else "✗"
        print(f"  {status} Span: {span.name}")
        print(f"      db.system: {attrs.get('db.system', 'N/A')}")
        print(f"      db.operation.name: {attrs.get('db.operation.name', 'N/A')}")
        if 'db.vector.query.top_k' in attrs:
            print(f"      db.vector.query.top_k: {attrs['db.vector.query.top_k']}")
        if 'db.vector.results.count' in attrs:
            print(f"      db.vector.results.count: {attrs['db.vector.results.count']}")
        if 'db.vector.upsert.count' in attrs:
            print(f"      db.vector.upsert.count: {attrs['db.vector.upsert.count']}")
        print()
    span_count = len(spans)
    EXPORTER.clear()
    return span_count


def test_chromadb_real():
    """Test ChromaDB with real data."""
    print("\n" + "="*70)
    print("  TESTING: ChromaDB (Embedded)")
    print("="*70)

    # Import and instrument
    from traceai_chromadb import ChromaDBInstrumentor
    import chromadb

    instrumentor = ChromaDBInstrumentor()
    instrumentor.instrument(tracer_provider=trace_api.get_tracer_provider())

    try:
        # Create ephemeral client
        client = chromadb.Client()
        print("  ✓ Created ChromaDB client")

        # Create collection
        collection = client.create_collection(
            name="test_collection",
            metadata={"hnsw:space": "cosine"}
        )
        print("  ✓ Created collection 'test_collection'")

        # Add documents with embeddings
        documents = [
            "Machine learning is a subset of artificial intelligence.",
            "Deep learning uses neural networks with multiple layers.",
            "Natural language processing analyzes human language.",
            "Computer vision enables machines to interpret images.",
            "Reinforcement learning uses reward-based training.",
        ]

        embeddings = [generate_embedding(doc) for doc in documents]
        ids = [f"doc_{i}" for i in range(len(documents))]

        collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=[{"category": "ml"} for _ in documents],
        )
        print(f"  ✓ Added {len(documents)} documents with embeddings")

        # Query
        query_embedding = generate_embedding("neural networks and AI")
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=3,
            include=["documents", "distances", "metadatas"],
        )
        print(f"  ✓ Query returned {len(results['ids'][0])} results")

        # Get by ID
        get_results = collection.get(ids=["doc_0", "doc_1"])
        print(f"  ✓ Get returned {len(get_results['ids'])} documents")

        # Count
        count = collection.count()
        print(f"  ✓ Collection count: {count}")

        # Delete
        collection.delete(ids=["doc_4"])
        print("  ✓ Deleted 1 document")

        # Verify count after delete
        new_count = collection.count()
        print(f"  ✓ New count after delete: {new_count}")

        # Print spans
        span_count = print_spans("ChromaDB Spans")

        # Cleanup
        client.delete_collection("test_collection")

        print(f"\n  ✓ ChromaDB TEST PASSED - {span_count} spans captured")
        return True, span_count

    except Exception as e:
        print(f"\n  ✗ ChromaDB TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False, 0
    finally:
        instrumentor.uninstrument()


def test_lancedb_real():
    """Test LanceDB with real data."""
    print("\n" + "="*70)
    print("  TESTING: LanceDB (Embedded)")
    print("="*70)

    # Import and instrument
    from traceai_lancedb import LanceDBInstrumentor
    import lancedb

    instrumentor = LanceDBInstrumentor()
    instrumentor.instrument(tracer_provider=trace_api.get_tracer_provider())

    # Create temp directory for LanceDB
    temp_dir = tempfile.mkdtemp()

    try:
        # Connect to database
        db = lancedb.connect(temp_dir)
        print(f"  ✓ Connected to LanceDB at {temp_dir}")

        # Create table with data
        documents = [
            "Machine learning algorithms learn patterns from data.",
            "Deep neural networks have multiple hidden layers.",
            "Natural language processing understands human text.",
            "Computer vision processes images and videos.",
            "Reinforcement learning optimizes through rewards.",
        ]

        data = [
            {
                "id": i,
                "text": doc,
                "vector": generate_embedding(doc),
            }
            for i, doc in enumerate(documents)
        ]

        table = db.create_table("documents", data)
        print(f"  ✓ Created table with {len(documents)} documents")

        # Search
        query_vector = generate_embedding("neural networks")
        results = table.search(query_vector).limit(3).to_list()
        print(f"  ✓ Search returned {len(results)} results")

        # Add more data
        new_data = [
            {"id": 10, "text": "Transformers revolutionized NLP.", "vector": generate_embedding("Transformers")}
        ]
        table.add(new_data)
        print("  ✓ Added 1 new document")

        # Search again
        results2 = table.search(query_vector).limit(5).to_list()
        print(f"  ✓ Second search returned {len(results2)} results")

        # Delete
        table.delete("id = 0")
        print("  ✓ Deleted document with id=0")

        # Print spans
        span_count = print_spans("LanceDB Spans")

        print(f"\n  ✓ LanceDB TEST PASSED - {span_count} spans captured")
        return True, span_count

    except Exception as e:
        print(f"\n  ✗ LanceDB TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False, 0
    finally:
        instrumentor.uninstrument()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_qdrant_real():
    """Test Qdrant with real data (in-memory mode)."""
    print("\n" + "="*70)
    print("  TESTING: Qdrant (In-Memory)")
    print("="*70)

    # Import and instrument
    from traceai_qdrant import QdrantInstrumentor
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams, PointStruct

    instrumentor = QdrantInstrumentor()
    instrumentor.instrument(tracer_provider=trace_api.get_tracer_provider())

    try:
        # Create in-memory client
        client = QdrantClient(":memory:")
        print("  ✓ Created Qdrant in-memory client")

        collection_name = "test_collection"

        # Create collection
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=384, distance=Distance.COSINE),
        )
        print(f"  ✓ Created collection '{collection_name}'")

        # Add points
        documents = [
            "Machine learning enables pattern recognition.",
            "Deep learning uses multi-layer neural networks.",
            "NLP processes human language data.",
            "Computer vision interprets visual information.",
            "RL agents learn through trial and error.",
        ]

        points = [
            PointStruct(
                id=i,
                vector=generate_embedding(doc),
                payload={"text": doc, "category": "ml"}
            )
            for i, doc in enumerate(documents)
        ]

        client.upsert(collection_name=collection_name, points=points)
        print(f"  ✓ Upserted {len(points)} points")

        # Query (using query_points - new Qdrant API)
        query_vector = generate_embedding("neural networks AI")
        results = client.query_points(
            collection_name=collection_name,
            query=query_vector,
            limit=3,
            with_payload=True,
        )
        print(f"  ✓ Query returned {len(results.points)} results")

        # Retrieve by ID
        retrieved = client.retrieve(
            collection_name=collection_name,
            ids=[0, 1],
            with_payload=True,
        )
        print(f"  ✓ Retrieved {len(retrieved)} points by ID")

        # Count
        count = client.count(collection_name=collection_name)
        print(f"  ✓ Collection count: {count.count}")

        # Delete
        client.delete(
            collection_name=collection_name,
            points_selector=[4],
        )
        print("  ✓ Deleted 1 point")

        # Scroll
        scroll_results = client.scroll(
            collection_name=collection_name,
            limit=10,
            with_payload=True,
        )
        print(f"  ✓ Scroll returned {len(scroll_results[0])} points")

        # Print spans
        span_count = print_spans("Qdrant Spans")

        print(f"\n  ✓ Qdrant TEST PASSED - {span_count} spans captured")
        return True, span_count

    except Exception as e:
        print(f"\n  ✗ Qdrant TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False, 0
    finally:
        instrumentor.uninstrument()


def test_weaviate_real():
    """Test Weaviate with real data (embedded mode)."""
    print("\n" + "="*70)
    print("  TESTING: Weaviate (Embedded)")
    print("="*70)

    # Import and instrument
    from traceai_weaviate import WeaviateInstrumentor
    import weaviate
    from weaviate.classes.config import Property, DataType, Configure

    instrumentor = WeaviateInstrumentor()
    instrumentor.instrument(tracer_provider=trace_api.get_tracer_provider())

    client = None
    try:
        # Create embedded client
        client = weaviate.connect_to_embedded()
        print("  ✓ Created Weaviate embedded client")

        collection_name = "TestDocuments"

        # Delete collection if it exists
        if client.collections.exists(collection_name):
            client.collections.delete(collection_name)

        # Create collection with vector index
        collection = client.collections.create(
            name=collection_name,
            properties=[
                Property(name="text", data_type=DataType.TEXT),
                Property(name="category", data_type=DataType.TEXT),
            ],
            vectorizer_config=Configure.Vectorizer.none(),
        )
        print(f"  ✓ Created collection '{collection_name}'")

        # Insert documents with embeddings
        documents = [
            "Machine learning enables pattern recognition.",
            "Deep learning uses multi-layer neural networks.",
            "NLP processes human language data.",
            "Computer vision interprets visual information.",
            "RL agents learn through trial and error.",
        ]

        # Insert documents one by one
        for i, doc in enumerate(documents):
            collection.data.insert(
                properties={"text": doc, "category": "ml"},
                vector=generate_embedding(doc),
            )
        print(f"  ✓ Inserted {len(documents)} documents")

        # Near vector search
        query_vector = generate_embedding("neural networks AI")
        results = collection.query.near_vector(
            near_vector=query_vector,
            limit=3,
            return_properties=["text", "category"],
        )
        print(f"  ✓ Near vector search returned {len(results.objects)} results")

        # Fetch objects
        fetch_results = collection.query.fetch_objects(limit=5)
        print(f"  ✓ Fetch objects returned {len(fetch_results.objects)} results")

        # Delete by ID (get first object's UUID)
        if results.objects:
            first_uuid = results.objects[0].uuid
            collection.data.delete_by_id(first_uuid)
            print("  ✓ Deleted 1 document by ID")

        # Print spans
        span_count = print_spans("Weaviate Spans")

        # Cleanup
        client.collections.delete(collection_name)

        print(f"\n  ✓ Weaviate TEST PASSED - {span_count} spans captured")
        return True, span_count

    except Exception as e:
        print(f"\n  ✗ Weaviate TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False, 0
    finally:
        instrumentor.uninstrument()
        if client:
            client.close()


def test_milvus_real():
    """Test Milvus with real data (Milvus Lite mode)."""
    print("\n" + "="*70)
    print("  TESTING: Milvus (Milvus Lite)")
    print("="*70)

    # Import and instrument
    from traceai_milvus import MilvusInstrumentor
    from pymilvus import MilvusClient

    instrumentor = MilvusInstrumentor()
    instrumentor.instrument(tracer_provider=trace_api.get_tracer_provider())

    # Create temp file for Milvus Lite
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "milvus.db")

    try:
        # Create Milvus Lite client
        client = MilvusClient(db_path)
        print(f"  ✓ Created Milvus Lite client at {db_path}")

        collection_name = "test_documents"

        # Drop collection if exists
        if client.has_collection(collection_name):
            client.drop_collection(collection_name)

        # Create collection
        client.create_collection(
            collection_name=collection_name,
            dimension=384,
        )
        print(f"  ✓ Created collection '{collection_name}'")

        # Insert documents
        documents = [
            "Machine learning enables pattern recognition.",
            "Deep learning uses multi-layer neural networks.",
            "NLP processes human language data.",
            "Computer vision interprets visual information.",
            "RL agents learn through trial and error.",
        ]

        data = [
            {
                "id": i,
                "vector": generate_embedding(doc),
                "text": doc,
                "category": "ml",
            }
            for i, doc in enumerate(documents)
        ]

        client.insert(collection_name=collection_name, data=data)
        print(f"  ✓ Inserted {len(documents)} documents")

        # Search
        query_vector = generate_embedding("neural networks AI")
        results = client.search(
            collection_name=collection_name,
            data=[query_vector],
            limit=3,
            output_fields=["text", "category"],
        )
        print(f"  ✓ Search returned {len(results[0])} results")

        # Query by filter
        query_results = client.query(
            collection_name=collection_name,
            filter="category == 'ml'",
            output_fields=["text", "category"],
            limit=5,
        )
        print(f"  ✓ Query returned {len(query_results)} results")

        # Get by ID
        get_results = client.get(
            collection_name=collection_name,
            ids=[0, 1],
            output_fields=["text", "category"],
        )
        print(f"  ✓ Get returned {len(get_results)} results")

        # Delete
        client.delete(
            collection_name=collection_name,
            ids=[4],
        )
        print("  ✓ Deleted 1 document")

        # Print spans
        span_count = print_spans("Milvus Spans")

        # Cleanup
        client.drop_collection(collection_name)
        client.close()

        print(f"\n  ✓ Milvus TEST PASSED - {span_count} spans captured")
        return True, span_count

    except Exception as e:
        print(f"\n  ✗ Milvus TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False, 0
    finally:
        instrumentor.uninstrument()
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_mongodb_real():
    """Test MongoDB with real data (requires Docker)."""
    print("\n" + "="*70)
    print("  TESTING: MongoDB (Docker)")
    print("="*70)

    # Check if MongoDB is available
    try:
        from pymongo import MongoClient
        from pymongo.errors import ServerSelectionTimeoutError

        test_client = MongoClient(f"mongodb://admin:admin@localhost:{MONGODB_PORT}/", serverSelectionTimeoutMS=2000)
        test_client.server_info()
        test_client.close()
    except Exception as e:
        print(f"  ⊘ MongoDB not available on port {MONGODB_PORT}: {e}")
        print("  ⊘ SKIPPED (start with: docker-compose up -d mongodb)")
        return None, 0

    # Import and instrument
    from traceai_mongodb import MongoDBInstrumentor

    instrumentor = MongoDBInstrumentor()
    instrumentor.instrument(tracer_provider=trace_api.get_tracer_provider())

    try:
        client = MongoClient(f"mongodb://admin:admin@localhost:{MONGODB_PORT}/")
        db = client["test_vectordb"]
        collection = db["documents"]
        print("  ✓ Connected to MongoDB")

        # Clear existing data
        collection.delete_many({})

        # Insert documents
        documents = [
            {"text": "Machine learning enables pattern recognition.", "category": "ml", "vector": generate_embedding("ML", 128)},
            {"text": "Deep learning uses neural networks.", "category": "ml", "vector": generate_embedding("DL", 128)},
            {"text": "NLP processes human language.", "category": "nlp", "vector": generate_embedding("NLP", 128)},
            {"text": "Computer vision interprets images.", "category": "cv", "vector": generate_embedding("CV", 128)},
            {"text": "RL agents learn through rewards.", "category": "rl", "vector": generate_embedding("RL", 128)},
        ]

        result = collection.insert_many(documents)
        print(f"  ✓ Inserted {len(result.inserted_ids)} documents")

        # Find one
        doc = collection.find_one({"category": "ml"})
        print(f"  ✓ Find one returned: {doc['text'][:30]}...")

        # Find many
        cursor = collection.find({"category": "ml"})
        results = list(cursor)
        print(f"  ✓ Find returned {len(results)} documents")

        # Aggregate (simulating vector search pipeline)
        pipeline = [
            {"$match": {"category": {"$in": ["ml", "nlp"]}}},
            {"$limit": 3},
        ]
        agg_results = list(collection.aggregate(pipeline))
        print(f"  ✓ Aggregate returned {len(agg_results)} documents")

        # Update
        update_result = collection.update_one(
            {"category": "ml"},
            {"$set": {"updated": True}}
        )
        print(f"  ✓ Updated {update_result.modified_count} document")

        # Delete
        delete_result = collection.delete_one({"category": "rl"})
        print(f"  ✓ Deleted {delete_result.deleted_count} document")

        # Print spans
        span_count = print_spans("MongoDB Spans")

        # Cleanup
        collection.drop()
        client.close()

        print(f"\n  ✓ MongoDB TEST PASSED - {span_count} spans captured")
        return True, span_count

    except Exception as e:
        print(f"\n  ✗ MongoDB TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False, 0
    finally:
        instrumentor.uninstrument()


def test_pgvector_real():
    """Test pgvector with real data (requires Docker)."""
    print("\n" + "="*70)
    print("  TESTING: pgvector (Docker)")
    print("="*70)

    # Import and instrument FIRST (before any psycopg imports)
    from traceai_pgvector import PgVectorInstrumentor
    import numpy as np

    instrumentor = PgVectorInstrumentor()
    instrumentor.instrument(tracer_provider=trace_api.get_tracer_provider())

    # Now import psycopg (v3)
    import psycopg
    from pgvector.psycopg import register_vector

    # Check if PostgreSQL is available
    try:
        test_conn = psycopg.connect(
            host="localhost",
            port=POSTGRES_PORT,
            user="postgres",
            password="postgres",
            dbname="vectordb",
            connect_timeout=2
        )
        test_conn.close()
    except Exception as e:
        print(f"  ⊘ PostgreSQL not available on port {POSTGRES_PORT}: {e}")
        print("  ⊘ SKIPPED (start with: docker-compose up -d postgres)")
        instrumentor.uninstrument()
        return None, 0

    try:
        conn = psycopg.connect(
            host="localhost",
            port=POSTGRES_PORT,
            user="postgres",
            password="postgres",
            dbname="vectordb",
            autocommit=True
        )

        # Register vector type BEFORE creating cursor (enables numpy array support)
        register_vector(conn)

        cur = conn.cursor()
        print("  ✓ Connected to PostgreSQL (psycopg3)")

        # Enable vector extension
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        print("  ✓ Vector extension enabled")

        # Drop and create table
        cur.execute("DROP TABLE IF EXISTS documents")
        cur.execute("""
            CREATE TABLE documents (
                id SERIAL PRIMARY KEY,
                text TEXT,
                category TEXT,
                embedding vector(128)
            )
        """)
        print("  ✓ Created documents table")

        # Insert documents using numpy arrays (supported by pgvector adapter)
        documents = [
            ("Machine learning enables pattern recognition.", "ml"),
            ("Deep learning uses neural networks.", "ml"),
            ("NLP processes human language.", "nlp"),
            ("Computer vision interprets images.", "cv"),
            ("RL agents learn through rewards.", "rl"),
        ]

        for text, category in documents:
            embedding = np.array(generate_embedding(text, 128), dtype=np.float32)
            cur.execute(
                "INSERT INTO documents (text, category, embedding) VALUES (%s, %s, %s)",
                (text, category, embedding)
            )
        print(f"  ✓ Inserted {len(documents)} documents")

        # Vector similarity search (L2 distance)
        query_vector = np.array(generate_embedding("neural networks", 128), dtype=np.float32)
        cur.execute("""
            SELECT id, text, embedding <-> %s AS distance
            FROM documents
            ORDER BY distance
            LIMIT 3
        """, (query_vector,))
        results = cur.fetchall()
        print(f"  ✓ L2 search returned {len(results)} results")

        # Cosine similarity search
        cur.execute("""
            SELECT id, text, 1 - (embedding <=> %s) AS similarity
            FROM documents
            ORDER BY similarity DESC
            LIMIT 3
        """, (query_vector,))
        cosine_results = cur.fetchall()
        print(f"  ✓ Cosine search returned {len(cosine_results)} results")

        # Regular query
        cur.execute("SELECT COUNT(*) FROM documents WHERE category = 'ml'")
        count = cur.fetchone()[0]
        print(f"  ✓ Count query: {count} ML documents")

        # Print spans
        span_count = print_spans("pgvector Spans")

        # Cleanup
        cur.execute("DROP TABLE documents")
        cur.close()
        conn.close()

        print(f"\n  ✓ pgvector TEST PASSED - {span_count} spans captured")
        return True, span_count

    except Exception as e:
        print(f"\n  ✗ pgvector TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False, 0
    finally:
        instrumentor.uninstrument()


def test_redis_real():
    """Test Redis Vector Search with real data (requires Docker)."""
    print("\n" + "="*70)
    print("  TESTING: Redis Vector Search (Docker)")
    print("="*70)

    # Check if Redis is available
    try:
        import redis
        test_client = redis.Redis(host="localhost", port=REDIS_PORT, socket_timeout=2)
        test_client.ping()
        test_client.close()
    except Exception as e:
        print(f"  ⊘ Redis not available on port {REDIS_PORT}: {e}")
        print("  ⊘ SKIPPED (start with: docker-compose up -d redis)")
        return None, 0

    # Import and instrument
    from traceai_redis import RedisInstrumentor
    import redis
    from redis.commands.search.field import VectorField, TextField, TagField
    from redis.commands.search.index_definition import IndexDefinition, IndexType
    from redis.commands.search.query import Query
    import numpy as np

    instrumentor = RedisInstrumentor()
    instrumentor.instrument(tracer_provider=trace_api.get_tracer_provider())

    try:
        client = redis.Redis(host="localhost", port=REDIS_PORT, decode_responses=False)
        print("  ✓ Connected to Redis")

        index_name = "documents_idx"

        # Delete existing index if it exists
        try:
            client.ft(index_name).dropindex(delete_documents=True)
        except:
            pass

        # Create schema for vector search
        schema = (
            TextField("text"),
            TagField("category"),
            VectorField("embedding",
                "FLAT",
                {
                    "TYPE": "FLOAT32",
                    "DIM": 128,
                    "DISTANCE_METRIC": "COSINE",
                }
            ),
        )

        # Create index
        client.ft(index_name).create_index(
            schema,
            definition=IndexDefinition(prefix=["doc:"], index_type=IndexType.HASH)
        )
        print("  ✓ Created search index")

        # Insert documents
        documents = [
            ("Machine learning enables pattern recognition.", "ml"),
            ("Deep learning uses neural networks.", "ml"),
            ("NLP processes human language.", "nlp"),
            ("Computer vision interprets images.", "cv"),
            ("RL agents learn through rewards.", "rl"),
        ]

        for i, (text, category) in enumerate(documents):
            embedding = np.array(generate_embedding(text, 128), dtype=np.float32).tobytes()
            client.hset(f"doc:{i}", mapping={
                "text": text,
                "category": category,
                "embedding": embedding,
            })
        print(f"  ✓ Inserted {len(documents)} documents")

        # Vector similarity search
        query_vector = np.array(generate_embedding("neural networks", 128), dtype=np.float32).tobytes()

        q = Query("*=>[KNN 3 @embedding $vec AS score]").sort_by("score").return_fields("text", "category", "score").dialect(2)
        results = client.ft(index_name).search(q, query_params={"vec": query_vector})
        print(f"  ✓ Vector search returned {results.total} results")

        # Text search
        text_query = Query("@category:{ml}").return_fields("text", "category")
        text_results = client.ft(index_name).search(text_query)
        print(f"  ✓ Text search returned {text_results.total} results")

        # Print spans
        span_count = print_spans("Redis Spans")

        # Cleanup
        client.ft(index_name).dropindex(delete_documents=True)
        client.close()

        print(f"\n  ✓ Redis TEST PASSED - {span_count} spans captured")
        return True, span_count

    except Exception as e:
        print(f"\n  ✗ Redis TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False, 0
    finally:
        instrumentor.uninstrument()


def main():
    """Run all real E2E tests."""
    print("\n" + "="*70)
    print("  VECTOR DATABASE INSTRUMENTATION - REAL E2E TESTS")
    print("="*70)
    print("\n  Running tests with REAL data (no mocking)")
    print("  Using embedded/in-memory databases")

    results = []

    # Test ChromaDB
    success, spans = test_chromadb_real()
    results.append(("ChromaDB", success, spans))

    # Test LanceDB
    success, spans = test_lancedb_real()
    results.append(("LanceDB", success, spans))

    # Test Qdrant
    success, spans = test_qdrant_real()
    results.append(("Qdrant", success, spans))

    # Test Weaviate
    success, spans = test_weaviate_real()
    results.append(("Weaviate", success, spans))

    # Test Milvus
    success, spans = test_milvus_real()
    results.append(("Milvus", success, spans))

    # Docker-based tests (optional - skip if not available)
    print("\n" + "="*70)
    print("  DOCKER-BASED TESTS (optional)")
    print("="*70)

    # Test MongoDB
    success, spans = test_mongodb_real()
    if success is not None:  # None means skipped
        results.append(("MongoDB", success, spans))

    # Test pgvector
    success, spans = test_pgvector_real()
    if success is not None:
        results.append(("pgvector", success, spans))

    # Test Redis
    success, spans = test_redis_real()
    if success is not None:
        results.append(("Redis", success, spans))

    # Summary
    print("\n" + "="*70)
    print("  TEST SUMMARY")
    print("="*70)

    total_spans = 0
    all_passed = True

    for db_name, success, spans in results:
        if success is None:
            status = "⊘ SKIPPED"
        elif success:
            status = "✓ PASSED"
        else:
            status = "✗ FAILED"
        print(f"  {status}: {db_name} ({spans} spans)")
        total_spans += spans
        if not success:
            all_passed = False

    print(f"\n  Total spans captured: {total_spans}")
    print("="*70)

    if all_passed:
        print("\n  ✓ ALL TESTS PASSED!")
        return 0
    else:
        print("\n  ✗ SOME TESTS FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
