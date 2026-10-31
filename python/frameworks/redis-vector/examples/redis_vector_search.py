"""Trace a small RedisVL vector search.

Install traceAI-redis and run against Redis with vector search enabled.
Set FI_API_KEY and FI_SECRET_KEY for Future AGI, and optionally REDIS_URL.
The three-dimensional vectors are sample data, not generated embeddings.
"""

import os
import struct

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from redisvl.index import SearchIndex
from redisvl.query import VectorQuery
from traceai_redis import RedisInstrumentor


def main() -> None:
    provider = register(
        project_name="redis-vector-search", project_type=ProjectType.OBSERVE
    )
    instrumentor = RedisInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        index = SearchIndex.from_dict(
            {
                "index": {"name": "traceai-example", "prefix": "traceai-example"},
                "fields": [
                    {"name": "content", "type": "text"},
                    {
                        "name": "embedding",
                        "type": "vector",
                        "attrs": {
                            "dims": 3,
                            "algorithm": "flat",
                            "datatype": "float32",
                            "distance_metric": "cosine",
                        },
                    },
                ],
            },
            redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379"),
        )
        index.create(overwrite=False)
        index.load(
            [
                {
                    "content": "Vector search",
                    "embedding": struct.pack("3f", 0.1, 0.2, 0.3),
                },
                {
                    "content": "Document storage",
                    "embedding": struct.pack("3f", 0.8, 0.1, 0.1),
                },
            ],
            keys=["traceai-example:1", "traceai-example:2"],
        )
        query = VectorQuery(
            vector=[0.1, 0.2, 0.3],
            vector_field_name="embedding",
            return_fields=["content"],
            num_results=2,
        )
        print(index.query(query))
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


if __name__ == "__main__":
    main()
