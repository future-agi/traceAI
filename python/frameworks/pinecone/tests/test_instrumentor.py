"""Real Pinecone Index contracts with a loopback HTTP endpoint, no vendor calls."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pinecone
try:
    from pinecone import Index
except ImportError:
    from pinecone.db_data import Index
import pytest
from opentelemetry.trace import StatusCode
from traceai_pinecone import PineconeInstrumentor


@pytest.fixture
def endpoint():
    requests = []
    state = {"error": False}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            requests.append((self.path, payload))
            responses = {
                "/query": {"matches": [{"id": "vec1", "score": 0.95, "metadata": {"text": "doc1"}}], "namespace": "test-ns"},
                "/vectors/upsert": {"upsertedCount": 2},
                "/vectors/delete": {},
                "/vectors/update": {},
                "/describe_index_stats": {"totalVectorCount": 2, "dimension": 3, "namespaces": {}},
            }
            assert self.path in responses, self.path
            self.send_response(400 if state["error"] else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            response = {"code": 3, "message": "invalid query"} if state["error"] else responses[self.path]
            self.wfile.write(json.dumps(response).encode())

        def do_GET(self):
            requests.append((self.path, None))
            assert self.path.startswith("/vectors/fetch"), self.path
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"vectors": {"vec1": {"id": "vec1", "values": [0.1, 0.2, 0.3]}}, "namespace": "test-ns"}).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, state
    finally:
        server.shutdown()
        worker.join()
        server.server_close()


@pytest.fixture
def index(endpoint, instrument_pinecone):
    index = Index(api_key="test-not-a-secret", host=endpoint[0])
    try:
        yield index
    finally:
        if hasattr(index, "close"):
            index.close()


class TestPineconeInstrumentor:
    def test_instrumentor_can_be_instantiated(self):
        assert isinstance(PineconeInstrumentor(), PineconeInstrumentor)

    def test_instrumentation_dependencies(self):
        assert tuple(PineconeInstrumentor().instrumentation_dependencies()) == ("pinecone >= 3.0.0",)

    def test_distribution_preserves_historical_floor(self):
        from importlib.metadata import requires
        from packaging.requirements import Requirement

        upstream, = [Requirement(value) for value in requires("traceAI-pinecone")
                     if Requirement(value).name == "pinecone"]
        assert upstream.specifier.contains("3.0.0")
        assert upstream.specifier.contains("4.0.0")

    def test_uninstrument_restores_index(self, tracer_provider):
        names = ("query", "upsert", "delete", "fetch", "update", "describe_index_stats")
        originals = {name: getattr(Index, name) for name in names}
        instrumentor = PineconeInstrumentor()
        instrumentor.instrument(tracer_provider=tracer_provider)
        try:
            for name, original in originals.items():
                assert getattr(Index, name) is not original
        finally:
            instrumentor.uninstrument()
        for name, original in originals.items():
            assert getattr(Index, name) is original


class TestPineconeQuery:
    def test_query_creates_span(self, index, endpoint, in_memory_span_exporter):
        result = index.query(vector=[0.1, 0.2, 0.3], top_k=10, namespace="test-ns", include_metadata=True)
        assert result.matches[0].id == "vec1"
        assert result.matches[0].score == 0.95
        assert result.matches[0].metadata == {"text": "doc1"}
        assert endpoint[1][0][1]["vector"] == [0.1, 0.2, 0.3]
        span, = in_memory_span_exporter.get_finished_spans()
        assert span.name == "pinecone query"
        assert span.attributes["db.system"] == "pinecone"
        assert span.attributes["db.operation.name"] == "query"
        assert span.attributes["db.vector.results.count"] == 1
        assert span.status.status_code == StatusCode.OK

    def test_query_with_filter_captures_filter(self, index, endpoint, in_memory_span_exporter):
        index.query(vector=[0.1, 0.2, 0.3], top_k=5, filter={"genre": "action"})
        assert endpoint[1][0][1]["filter"] == {"genre": "action"}
        span, = in_memory_span_exporter.get_finished_spans()
        assert span.attributes["db.vector.query.top_k"] == 5
        assert json.loads(span.attributes["db.vector.query.filter"]) == {"genre": "action"}


class TestPineconeUpsert:
    def test_upsert_creates_span(self, index, in_memory_span_exporter):
        result = index.upsert(vectors=[{"id": "vec1", "values": [0.1, 0.2, 0.3]}, {"id": "vec2", "values": [0.2, 0.3, 0.4]}], namespace="test-ns")
        assert result.upserted_count == 2
        span, = in_memory_span_exporter.get_finished_spans()
        assert span.name == "pinecone upsert"
        assert span.attributes["db.system"] == "pinecone"
        assert span.attributes["db.vector.upsert.count"] == 2
        assert span.status.status_code == StatusCode.OK


class TestPineconeDelete:
    def test_delete_creates_span(self, index, endpoint, in_memory_span_exporter):
        index.delete(ids=["vec1", "vec2"], namespace="test-ns")
        assert endpoint[1][0][1]["ids"] == ["vec1", "vec2"]
        span, = in_memory_span_exporter.get_finished_spans()
        assert span.name == "pinecone delete"
        assert span.attributes["db.system"] == "pinecone"
        assert span.status.status_code == StatusCode.OK


class TestPineconeErrorHandling:
    def test_query_error_is_recorded(self, index, endpoint, in_memory_span_exporter):
        endpoint[2]["error"] = True
        with pytest.raises(Exception, match="invalid query") as caught:
            index.query(vector=[0.1, 0.2, 0.3], top_k=10)
        assert type(caught.value).__module__.startswith("pinecone")
        span, = in_memory_span_exporter.get_finished_spans()
        assert span.status.status_code == StatusCode.ERROR
        assert span.events[0].name == "exception"
        assert "invalid query" in span.events[0].attributes["exception.message"]
