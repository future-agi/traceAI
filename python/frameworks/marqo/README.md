# Marqo Python SDK tracing

Trace calls made through the [`marqo` Python SDK](https://pypi.org/project/marqo/) with the maintained `traceAI-marqo` package. It supports `marqo` 3.5.0 up to, but not including, 4.0.

```bash
pip install traceAI-marqo
```

Set `FI_API_KEY` and `FI_SECRET_KEY` (and `MARQO_API_KEY` for Marqo Cloud) in your environment. Then:

```python
import os

import marqo
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_marqo import MarqoInstrumentor

provider = register(project_type=ProjectType.OBSERVE, project_name="marqo-demo")
MarqoInstrumentor().instrument(tracer_provider=provider)
client = marqo.Client(url="http://localhost:8882", api_key=os.environ.get("MARQO_API_KEY"))
index = client.index("products")
index.add_documents([{"_id": "1", "title": "red running shoes"}], tensor_fields=["title"])
print(index.search("shoes", limit=3)["hits"])
provider.force_flush()
```

With the `fi-instrument` launcher or `FI_INSTRUMENTATIONS`, no code is needed: importing `marqo` turns this integration on.

Covered calls (`marqo.index.Index`; the SDK is synchronous and has no streaming calls, so there are no async or stream spans):

| Span | Kind | Records |
| --- | --- | --- |
| `marqo.search` | RETRIEVER | query, `limit` as top-k, search parameters, hit count, ids, scores, document content |
| `marqo.add_documents` | CHAIN | document count, batch and tensor-field parameters, documents, whether any item failed |
| `marqo.get_documents` / `marqo.get_document` | RETRIEVER | found-document count, ids, document content |

Every span has `gen_ai.provider.name` and `db.system` set to `marqo`, the operation, and the index name (`db.vector.index.name`, `db.namespace`). Only the outermost SDK call is traced. Exceptions set the span status to ERROR and `error.type`, and are re-raised unchanged; server error messages and stacks are never exported.

Embedding vectors are never exported: numeric arrays and `vector` fields are replaced in document content, and `_highlights`, `_tensor_facets`, `context` and other underscore fields, `mappings` and request headers are not captured. Query text, documents and result content are captured by default, capped, and removed by `TraceConfig(hide_inputs=True, hide_outputs=True)` passed to `instrument(config=...)`; the directional `hide_input_*` and `hide_output_*` flags also suppress the corresponding content. Do not instrument `requests` for the same calls, or you will get duplicate spans. The package does not send requests to Marqo on its own.
