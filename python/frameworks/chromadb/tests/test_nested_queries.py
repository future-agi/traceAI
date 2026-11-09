"""Real HTTP clients must trace independent queries from embedding callbacks."""

import asyncio
import inspect
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import chromadb
import numpy as np
import pytest
from chromadb.config import Settings
from opentelemetry.trace import SpanKind, StatusCode


@pytest.fixture
def chroma_server():
    requests = []
    model = {
        "id": "00000000-0000-0000-0000-000000000001",
        "name": "nested-query-test",
        "metadata": None,
        "dimension": 3,
        "tenant": "default_tenant",
        "database": "default_database",
        "configuration_json": {},
        "serialized_schema": None,
        "version": 0,
        "log_position": 0,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size)) if size else {}
            route = self.path.split("?")[0]
            requests.append((route, body))
            if route.endswith("/identity"):
                data = {
                    "user_id": "offline",
                    "tenant": "default_tenant",
                    "databases": ["default_database"],
                }
            elif route.endswith("/default_tenant"):
                data = {"name": "default_tenant"}
            elif route.endswith("/default_database"):
                data = {"id": model["id"], "name": "default_database", "tenant": "default_tenant"}
            elif route.endswith("/query"):
                data = {
                    "ids": [["synthetic-result"]],
                    "distances": [[0.0]],
                    "documents": None,
                    "embeddings": None,
                    "metadatas": None,
                    "uris": None,
                    "data": None,
                    "included": ["distances"],
                }
            elif "/collections/" in route:
                data = model
            else:
                self.send_error(404)
                return
            payload = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_same_collection_embedding_query(
    asynchronous, chroma_server, instrument_chromadb, tracer_provider, in_memory_span_exporter
):
    port, requests = chroma_server

    async def exercise():
        kwargs = {
            "host": "127.0.0.1",
            "port": port,
            "settings": Settings(anonymized_telemetry=False),
        }
        client = await chromadb.AsyncHttpClient(**kwargs) if asynchronous else chromadb.HttpClient(**kwargs)

        class NestedEmbedding:
            def name(self):
                return "synthetic-nested-query"

            def get_config(self):
                return {}

            def embed_query(self, input):
                return np.asarray(self(input), dtype=np.float32)

            def __call__(self, input):
                def lookup():
                    result = collection.query(query_embeddings=[[1.0, 0.0, 0.0]], n_results=1)
                    return asyncio.run(result) if inspect.isawaitable(result) else result

                # Chroma's embedding callback is synchronous even on AsyncCollection.
                # Run its async lookup on a worker loop, propagating the OTel context.
                if asynchronous:
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        result = pool.submit(copy_context().run, lookup).result(timeout=10)
                else:
                    result = lookup()
                assert result["ids"] == [["synthetic-result"]]
                return [[1.0, 0.0, 0.0] for _ in input]

        collection = client.get_collection("nested-query-test", embedding_function=NestedEmbedding())
        if inspect.isawaitable(collection):
            collection = await collection
        in_memory_span_exporter.clear()
        requests.clear()
        with tracer_provider.get_tracer("test").start_as_current_span("request") as parent:
            result = collection.query(query_texts=["synthetic text"], n_results=1)
            if inspect.isawaitable(result):
                result = await result
            assert result["ids"] == [["synthetic-result"]]
        queries = [body for route, body in requests if route.endswith("/query")]
        assert len(queries) == 2
        assert all(body["query_embeddings"] == [[1.0, 0.0, 0.0]] for body in queries)
        spans = in_memory_span_exporter.get_finished_spans()
        assert [span.name for span in spans] == ["chroma query", "chroma query", "request"]
        inner, outer, _ = spans
        assert inner.parent.span_id == outer.context.span_id
        assert outer.parent.span_id == parent.get_span_context().span_id
        assert inner.context.trace_id == outer.context.trace_id == parent.get_span_context().trace_id
        for span in (inner, outer):
            assert span.kind == SpanKind.CLIENT
            assert span.status.status_code == StatusCode.OK
            assert span.end_time >= span.start_time
        assert inner.attributes["db.vector.query.type"] == "embedding"
        assert outer.attributes["db.vector.query.type"] == "text"

    asyncio.run(exercise())
