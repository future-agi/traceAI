"""Real elasticsearch-py clients over a stubbed transport node: no server, no credentials, no network."""
import json

import pytest
from elastic_transport import ApiResponseMeta, HttpHeaders
from elastic_transport._node import BaseNode, NodeApiResponse
from elastic_transport._node._base_async import BaseAsyncNode
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

VECTOR = [0.7312, 0.8421, 0.9533, 0.6644]
SECRET_TEXT = "synthetic-secret-token-value"


class Server:
    """Records the requests it receives and answers like an Elasticsearch node."""

    def __init__(self):
        self.requests = []
        self.fail_with = None

    def respond(self, method, target, body):
        self.requests.append((method, target, body))
        if self.fail_with is not None:
            return self.fail_with, {"error": {"type": "boom_exception", "reason": "failed " + SECRET_TEXT}, "status": self.fail_with}
        path = target.split("?")[0]
        if path.endswith("/_search"):
            return 200, {
                "took": 3, "timed_out": False,
                "hits": {"total": {"value": 2, "relation": "eq"}, "max_score": 1.5, "hits": [
                    {"_index": "docs", "_id": "d1", "_score": 1.5,
                     "_source": {"title": "private doc one", "price": 4242, "in_stock": True, "embedding": VECTOR}},
                    {"_index": "docs", "_id": "d2", "_score": 0.5,
                     "_source": {"title": "private doc two", "price": 17, "in_stock": False, "embedding": VECTOR}},
                ]},
            }
        if path.endswith("/_bulk"):
            return 200, {"took": 4, "errors": False, "items": [{"index": {"_id": "d1", "status": 201}}, {"index": {"_id": "d2", "status": 201}}]}
        if "/_doc/" in path and method == "DELETE":
            return 200, {"_index": "docs", "_id": "d1", "result": "deleted", "_version": 3}
        if "/_doc" in path:
            return 201, {"_index": "docs", "_id": "d1", "result": "created", "_version": 1}
        return 200, {}


def _parse(body):
    """A request body: one JSON document, or newline-delimited JSON for bulk requests."""
    if not body:
        return None
    text = body.decode() if isinstance(body, bytes) else body
    try:
        return json.loads(text)
    except ValueError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def _meta(config, status):
    return ApiResponseMeta(node=config, duration=0.0, http_version="1.1", status=status, headers=HttpHeaders(
        {"content-type": "application/json", "x-elastic-product": "Elasticsearch"}))


def _make_nodes(server):
    class SyncNode(BaseNode):
        def perform_request(self, method, target, body=None, headers=None, request_timeout=None):
            status, payload = server.respond(method, target, _parse(body))
            return NodeApiResponse(_meta(self.config, status), json.dumps(payload).encode())

        def close(self):
            pass

    class AsyncNode(BaseAsyncNode):
        async def perform_request(self, method, target, body=None, headers=None, request_timeout=None):
            status, payload = server.respond(method, target, _parse(body))
            return NodeApiResponse(_meta(self.config, status), json.dumps(payload).encode())

        async def close(self):
            pass

    return SyncNode, AsyncNode


@pytest.fixture
def server():
    return Server()


@pytest.fixture
def clients(server):
    from elasticsearch import AsyncElasticsearch, Elasticsearch

    sync_node, async_node = _make_nodes(server)
    return (Elasticsearch("http://es.invalid:9200", node_class=sync_node),
            lambda: AsyncElasticsearch("http://es.invalid:9200", node_class=async_node))


@pytest.fixture
def exporter():
    return InMemorySpanExporter()


@pytest.fixture
def provider(exporter):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider
