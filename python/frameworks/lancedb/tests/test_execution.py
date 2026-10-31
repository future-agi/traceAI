"""Offline execution contract tests using real local LanceDB and Arrow readers."""

import asyncio
import gc
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import lancedb
import pyarrow as pa
import pytest
from opentelemetry import context, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.utils import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

ROWS = [
    {"id": 1, "vector": [1.0, 0.0], "text": "violet flower"},
    {"id": 2, "vector": [0.0, 1.0], "text": "violet sky"},
    {"id": 3, "vector": [0.5, 0.5], "text": "blue sea"},
]


@pytest.fixture
def table(tmp_path, telemetry):
    table = lancedb.connect(str(tmp_path / "db")).create_table("documents", ROWS)
    telemetry[0].clear()
    return table


def search_spans(exporter):
    return [s for s in exporter.get_finished_spans() if s.name == "lancedb search"]


@pytest.mark.parametrize("method", ["to_list", "to_arrow", "to_pandas", "to_pydantic"])
def test_one_span_per_materialization(table, telemetry, method):
    from lancedb.pydantic import LanceModel

    class Row(LanceModel):
        id: int

    query = table.search([1.0, 0.0]).limit(2)
    result = getattr(query, method)(*([Row] if method == "to_pydantic" else []))
    assert len(result) == 2
    spans = search_spans(telemetry[0])
    assert len(spans) == 1
    assert spans[0].attributes["db.namespace"] == "documents"
    assert spans[0].attributes["db.vector.results.count"] == 2
    assert spans[0].attributes["db.vector.search.output_format"] == method


@pytest.mark.parametrize("query_type", ["fts", "hybrid", "scan"])
def test_query_kinds(table, telemetry, query_type):
    if query_type == "scan":
        query = table.search()
    else:
        table.create_fts_index("text", use_tantivy=False)
        query = (
            table.search("violet", query_type="fts")
            if query_type == "fts"
            else (table.search(query_type="hybrid").vector([1.0, 0.0]).text("violet"))
        )
    telemetry[0].clear()
    assert len(query.limit(2).to_list()) == 2
    assert len(search_spans(telemetry[0])) == 1


@pytest.mark.parametrize(
    "consume", ["iterate", "read_all", "read_next_batch", "c_stream", "cast"]
)
def test_batch_reader_lifecycle(table, telemetry, consume):
    reader = table.search([1.0, 0.0]).limit(3).to_batches()
    assert isinstance(reader, pa.RecordBatchReader)
    assert not search_spans(telemetry[0])
    assert not trace.get_current_span().get_span_context().is_valid
    if consume == "read_all":
        count = reader.read_all().num_rows
    elif consume == "read_next_batch":
        count = 0
        while True:
            try:
                count += reader.read_next_batch().num_rows
            except StopIteration:
                break
    elif consume == "c_stream":
        count = pa.RecordBatchReader.from_stream(reader).read_all().num_rows
    elif consume == "cast":
        count = reader.cast(reader.schema).read_all().num_rows
    else:
        count = sum(batch.num_rows for batch in reader)
    assert count == 3
    spans = search_spans(telemetry[0])
    assert len(spans) == 1
    assert spans[0].attributes["db.vector.results.count"] == 3
    assert spans[0].status.status_code.name == "OK"


def test_partial_reader_close_and_abandon(table, telemetry):
    reader = table.search([1.0, 0.0]).to_batches()
    reader.close()
    reader.close()
    assert len(search_spans(telemetry[0])) == 1
    assert search_spans(telemetry[0])[0].status.status_code.name == "UNSET"
    reader = table.search([1.0, 0.0]).to_batches()
    del reader
    gc.collect()
    assert len(search_spans(telemetry[0])) == 2


def test_disabled_and_reinstrumented(table, telemetry):
    exporter, provider, instrumentor = telemetry
    instrumentor.uninstrument()
    table.search([1.0, 0.0]).to_list()
    assert not exporter.get_finished_spans()
    instrumentor.instrument(tracer_provider=provider)
    table.search([1.0, 0.0]).to_list()
    assert len(search_spans(exporter)) == 1


def test_suppression(table, telemetry):
    token = context.attach(context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        table.search([1.0, 0.0]).to_list()
    finally:
        context.detach(token)
    assert not telemetry[0].get_finished_spans()


def test_parent_context_and_independent_queries(table, telemetry):
    exporter, provider, _ = telemetry
    with provider.get_tracer("test").start_as_current_span("parent") as parent:
        reader = table.search([1.0, 0.0]).to_batches()
        assert trace.get_current_span() is parent
        with ThreadPoolExecutor(2) as executor:
            assert list(
                executor.map(
                    lambda _: len(table.search([1.0, 0.0]).to_list()), range(2)
                )
            ) == [3, 3]
        assert reader.read_all().num_rows == 3
        assert trace.get_current_span() is parent
    spans = search_spans(exporter)
    assert len(spans) == 3
    assert (
        sum(
            s.parent is not None
            and s.parent.span_id == parent.get_span_context().span_id
            for s in spans
        )
        == 1
    )


def test_sync_writes(tmp_path, telemetry):
    exporter = telemetry[0]
    db = lancedb.connect(str(tmp_path / "writes"))
    table = db.create_table("documents", ROWS)
    table = db.open_table("documents")
    table.add([dict(ROWS[0], id=4)])
    table.update(where="id = 4", values={"text": "changed"})
    table.delete("id = 4")
    db.drop_table("documents")
    assert [s.name for s in exporter.get_finished_spans()] == [
        "lancedb create_table",
        "lancedb open_table",
        "lancedb add",
        "lancedb update",
        "lancedb delete",
        "lancedb drop_table",
    ]


def test_async_queries_and_writes(tmp_path, telemetry):
    async def run():
        db = await lancedb.connect_async(str(tmp_path / "async"))
        table = await db.create_table("documents", ROWS)
        table = await db.open_table("documents")
        await table.add([dict(ROWS[0], id=4)])
        await table.update(where="id = 4", updates={"text": "changed"})
        await table.delete("id = 4")
        exporter = telemetry[0]
        assert [s.name for s in exporter.get_finished_spans()] == [
            "lancedb create_table",
            "lancedb open_table",
            "lancedb add",
            "lancedb update",
            "lancedb delete",
        ]
        exporter.clear()
        query = table.vector_search([1.0, 0.0]).limit(2)
        pending = query.to_list()
        assert not exporter.get_finished_spans()
        assert len(await pending) == 2
        assert len(search_spans(exporter)) == 1
        exporter.clear()
        reader = await table.query().limit(2).to_batches()
        assert not exporter.get_finished_spans()
        assert sum(batch.num_rows for batch in await reader.read_all()) == 2
        assert len(search_spans(exporter)) == 1
        assert search_spans(exporter)[0].attributes["db.vector.results.count"] == 2
        exporter.clear()
        await asyncio.gather(
            *(table.vector_search([1.0, 0.0]).to_list() for _ in range(3))
        )
        assert len(search_spans(exporter)) == 3
        await db.drop_table("documents")

    asyncio.run(run())


@contextmanager
def otlp_receiver():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(
                ExportTraceServiceRequest.FromString(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
            )
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/traces", requests
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_otlp_wire_shape_and_privacy(table, telemetry):
    _, provider, _ = telemetry
    with otlp_receiver() as (endpoint, requests):
        provider.add_span_processor(
            SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        )
        assert len(table.search([1.0, 0.0]).limit(2).to_list()) == 2
        table.delete("text = 'PRIVATE_FILTER_SENTINEL'")
        with pytest.raises((RuntimeError, ValueError)):
            table.search([1.0, 0.0]).where("PRIVATE_ERROR_SENTINEL = 1").to_list()
        wire = b"".join(r.SerializeToString() for r in requests)
        assert b"PRIVATE_FILTER_SENTINEL" not in wire
        assert b"PRIVATE_ERROR_SENTINEL" not in wire
        spans = [
            s
            for r in requests
            for rs in r.resource_spans
            for ss in rs.scope_spans
            for s in ss.spans
        ]
        assert len(spans) == 3
        assert all(s.kind == 3 for s in spans)  # OTLP CLIENT
        assert spans[-1].status.code == 2
        assert spans[-1].end_time_unix_nano >= spans[-1].start_time_unix_nano
