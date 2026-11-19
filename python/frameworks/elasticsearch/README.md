# Elasticsearch Python client tracing

Trace calls made through the [`elasticsearch` Python client](https://pypi.org/project/elasticsearch/) with the maintained `traceAI-elasticsearch` package. It supports `elasticsearch` 8.x and 9.x, sync (`Elasticsearch`) and async (`AsyncElasticsearch`).

```bash
pip install traceAI-elasticsearch
```

```python
from elasticsearch import Elasticsearch
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_elasticsearch import ElasticsearchInstrumentor

provider = register(project_type=ProjectType.OBSERVE, project_name="es-demo")
ElasticsearchInstrumentor().instrument(tracer_provider=provider)
client = Elasticsearch("http://localhost:9200")
hits = client.search(index="products", query={"match": {"title": "shoes"}}, size=3)["hits"]["hits"]
provider.force_flush()
```

With the `fi-instrument` launcher or `FI_INSTRUMENTATIONS`, no code is needed: importing `elasticsearch` turns this integration on.

| Span | Kind | Records |
| --- | --- | --- |
| `elasticsearch.search` | RETRIEVER | index, query and filters, `size` or kNN `k` as top-k, kNN field, `num_candidates` and query-vector dimension count, result count, ids, scores, total hits, documents (id, score, content) |
| `elasticsearch.knn_search` | RETRIEVER | the same, on client releases that still have the call |
| `elasticsearch.index` / `elasticsearch.bulk` / `elasticsearch.delete` | VECTOR_DB | index, document id or write count, the documents written, bulk error and failure counts, result and version |

Every span has `gen_ai.provider.name` and `db.system` set to `elasticsearch`, the operation, and the index name (`db.vector.index.name`, `db.namespace`). Only the outermost client call is traced. Exceptions set the span status to ERROR, `error.type` and the HTTP status code, and are re-raised unchanged; server error messages are never exported.

Embedding vectors are never exported: `query_vector` is dropped from requests, and numeric arrays in documents are replaced by a placeholder. Raw request and response bodies are not recorded, and no span carries a whole response: only the fields listed above. Query text, filters, written documents and result content are captured by default, capped, and removed by the standard hide settings (`TraceConfig(hide_inputs=True, hide_outputs=True)` passed to `instrument(config=...)`, or the `FI_HIDE_*` variables); the directional `hide_input_*` and `hide_output_*` flags suppress the corresponding content. Numeric and boolean content values follow the same rules as text. Result ids, scores and top score are result content: any output hide setting removes them along with the documents, while counts and total hits stay. Array-likes (numpy arrays and scalars) are treated as vectors, and values of unknown types are reduced to a type note, never to their text. The package does not send requests to Elasticsearch on its own.

The client also has a built-in OpenTelemetry integration of its own (`OTEL_PYTHON_INSTRUMENTATION_ELASTICSEARCH_ENABLED`). If both are active a call produces two spans; set that variable to `false` when you use this package.
