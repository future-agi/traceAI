# Changelog

## Unreleased

- Result ids, scores and top score follow the output hide settings; numpy and other array-likes are omitted as vectors; values of unknown types are recorded as a type note; numeric and boolean values deep in a request are kept; the deprecated `body=` forms of `index`, `bulk` and `search` are read as input content, write counts and request parameters; `retriever` searches record their query and top-k.

## 0.1.0

- Initial release: RETRIEVER spans for `search` and `knn_search`, VECTOR_DB spans for `index`, `bulk` and `delete`, for `Elasticsearch` and `AsyncElasticsearch` (elasticsearch-py 8.x and 9.x). Content follows the traceAI `TraceConfig` hide settings; vectors and raw bodies are never recorded.
