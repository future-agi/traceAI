"""Trace one Voyage embed call and one rerank call.

Set FI_API_KEY, FI_SECRET_KEY and VOYAGE_API_KEY, then run this file. The
Voyage client picks its host from the key (Atlas keys go to ai.mongodb.com,
Voyage platform keys to api.voyageai.com). VOYAGE_BASE_URL is only read so a
test can point the client at a local fake; leave it unset otherwise.
"""

import os

import voyageai
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_voyage import VoyageInstrumentor

DOCUMENTS = [
    "EXAMPLE-DOC: Voyage embeddings turn text into vectors.",
    "EXAMPLE-DOC: A reranker orders documents by relevance to a query.",
]
QUERY = "EXAMPLE-QUERY: which document explains reranking?"


def main() -> None:
    tracer_provider = register(
        project_name="voyage-example",
        project_type=ProjectType.OBSERVE,
        batch=False,
        verbose=False,
    )
    VoyageInstrumentor().instrument(tracer_provider=tracer_provider)

    # The client reads VOYAGE_API_KEY. base_url=None keeps the client's own
    # host choice; the instrumentor never sets one.
    client = voyageai.Client(base_url=os.getenv("VOYAGE_BASE_URL"))

    embeddings = client.embed(DOCUMENTS, model="voyage-3.5", input_type="document")
    print(
        "embedded {0} documents, {1} tokens".format(
            len(embeddings.embeddings), embeddings.total_tokens
        )
    )

    reranking = client.rerank(QUERY, DOCUMENTS, model="rerank-2.5", top_k=2)
    print("top document index: {0}".format(reranking.results[0].index))

    tracer_provider.force_flush()


if __name__ == "__main__":
    main()
