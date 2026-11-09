"""Real PyMilvus clients against a loopback gRPC server; no provider credentials."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import grpc
import pymilvus
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pymilvus.exceptions import MilvusException
from pymilvus.grpc_gen import common_pb2, milvus_pb2, milvus_pb2_grpc, schema_pb2
from traceai_milvus import MilvusInstrumentor

EMAIL = "alice@example.com"
ERROR = f"query rejected for {EMAIL}"


class ErrorServer(milvus_pb2_grpc.MilvusServiceServicer):
    def __init__(self):
        self.filters = []

    def Connect(self, request, context):
        return milvus_pb2.ConnectResponse(identifier=77)

    def DescribeCollection(self, request, context):
        response = milvus_pb2.DescribeCollectionResponse(collectionID=1)
        response.schema.name = "documents"
        response.schema.fields.add(
            name="id", fieldID=100, data_type=schema_pb2.Int64, is_primary_key=True
        )
        response.schema.fields.add(
            name="text", fieldID=101, data_type=schema_pb2.VarChar
        )
        return response

    def Query(self, request, context):
        self.filters.append(request.expr)
        return milvus_pb2.QueryResults(
            status=common_pb2.Status(error_code=1, reason=ERROR)
        )


@pytest.fixture
def runtime():
    handler = ErrorServer()
    executor = ThreadPoolExecutor(max_workers=4)
    server = grpc.server(executor)
    milvus_pb2_grpc.add_MilvusServiceServicer_to_server(handler, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = MilvusInstrumentor()
    try:
        yield f"http://127.0.0.1:{port}", handler, provider, exporter, instrumentor
    finally:
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
        provider.shutdown()
        server.stop(0).wait()
        executor.shutdown(wait=True)


@pytest.mark.parametrize("source", ["config", "environment"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_query_error_pii_redacted(runtime, monkeypatch, source, asynchronous):
    if asynchronous and not hasattr(pymilvus, "AsyncMilvusClient"):
        pytest.skip("AsyncMilvusClient not provided by this SDK version")
    uri, handler, provider, exporter, instrumentor = runtime
    monkeypatch.setenv("FI_PII_REDACTION", "true" if source == "environment" else "false")
    config = TraceConfig(pii_redaction=True) if source == "config" else None
    instrumentor.instrument(tracer_provider=provider, config=config)
    query_filter = f'text == "{EMAIL}"'

    async def query_async():
        client = pymilvus.AsyncMilvusClient(uri=uri, timeout=3)
        try:
            await client.query(
                "documents", filter=query_filter, output_fields=["id"], timeout=3
            )
        finally:
            await client.close()

    with pytest.raises(MilvusException, match=EMAIL) as raised:
        if asynchronous:
            asyncio.run(query_async())
        else:
            client = pymilvus.MilvusClient(uri=uri, timeout=3)
            try:
                client.query(
                    "documents", filter=query_filter, output_fields=["id"], timeout=3
                )
            finally:
                client.close()
    # SDK requests and exceptions remain unchanged; only telemetry is redacted.
    assert handler.filters == [query_filter]
    assert ERROR in str(raised.value)
    (span,) = exporter.get_finished_spans()
    assert span.name == "milvus query"
    assert span.status.status_code == StatusCode.ERROR
    assert span.end_time is not None
    assert span.attributes["db.vector.query.filter"] == 'text == "<EMAIL_ADDRESS>"'
    assert "query rejected for <EMAIL_ADDRESS>" in span.status.description
    assert len(span.events) == 1
    attributes = span.events[0].attributes
    assert attributes["exception.type"] == "pymilvus.exceptions.MilvusException"
    assert attributes["exception.escaped"] == "False"
    assert "query rejected for <EMAIL_ADDRESS>" in attributes["exception.message"]
    assert "query rejected for <EMAIL_ADDRESS>" in attributes["exception.stacktrace"]
    assert "Traceback (most recent call last)" in attributes["exception.stacktrace"]
    assert EMAIL.encode() not in encode_spans([span]).SerializeToString()
