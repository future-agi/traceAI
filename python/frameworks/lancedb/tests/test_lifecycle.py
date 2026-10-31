"""Failure paths and synthetic remote surfaces; no vendor connections."""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import lancedb
import pyarrow as pa
import pytest
from opentelemetry import trace

from .test_execution import ROWS, search_spans


def native_async_reader(schema, batches):
    from lancedb.arrow import AsyncRecordBatchReader

    if hasattr(AsyncRecordBatchReader, "_from_batches"):
        return AsyncRecordBatchReader._from_batches(schema, batches)

    async def next_batch():
        try:
            return await batches.__anext__()
        except StopAsyncIteration:
            return None

    return AsyncRecordBatchReader(
        SimpleNamespace(schema=lambda: schema, next=next_batch)
    )


@pytest.mark.parametrize("asynchronous", [False, True])
def test_deferred_reader_failure(telemetry, monkeypatch, asynchronous):
    from lancedb import query

    exporter, provider, instrumentor = telemetry
    instrumentor.uninstrument()
    schema = pa.schema([("id", pa.int64())])
    error = RuntimeError("PRIVATE_READER_ERROR")

    def batches():
        yield pa.record_batch([[1, 2]], schema=schema)
        raise error

    async def async_batches():
        for batch in batches():
            yield batch

    if asynchronous:

        async def to_batches(self):
            return native_async_reader(schema, async_batches())

        cls = query.AsyncQueryBase
    else:

        def to_batches(self):
            return pa.RecordBatchReader.from_batches(schema, batches())

        cls = query.LanceVectorQueryBuilder
    monkeypatch.setattr(cls, "to_batches", to_batches)
    instrumentor.instrument(tracer_provider=provider)
    builder = object.__new__(cls)

    async def consume():
        reader = await builder.to_batches()
        assert not exporter.get_finished_spans()
        await reader.read_all()

    with pytest.raises(RuntimeError, match="PRIVATE_READER_ERROR") as caught:
        if asynchronous:
            asyncio.run(consume())
        else:
            reader = builder.to_batches()
            assert not exporter.get_finished_spans()
            reader.read_all()
    assert caught.value is error
    spans = search_spans(exporter)
    assert len(spans) == 1
    assert spans[0].status.status_code.name == "ERROR"
    assert spans[0].attributes["db.vector.results.count"] == 2
    assert "PRIVATE_READER_ERROR" not in str(spans[0].events)
    assert not trace.get_current_span().get_span_context().is_valid


@pytest.mark.parametrize("during_stream", [False, True])
def test_async_cancellation(telemetry, monkeypatch, during_stream):
    from lancedb.query import AsyncQueryBase

    exporter, provider, instrumentor = telemetry
    instrumentor.uninstrument()
    started = asyncio.Event()

    async def wait():
        started.set()
        await asyncio.Future()

    async def batches():
        await wait()
        yield  # pragma: no cover

    async def to_batches(self):
        if not during_stream:
            await wait()
        return native_async_reader(pa.schema([]), batches())

    monkeypatch.setattr(AsyncQueryBase, "to_batches", to_batches)
    instrumentor.instrument(tracer_provider=provider)
    builder = object.__new__(AsyncQueryBase)

    async def consume():
        reader = await builder.to_batches()
        await reader.read_all()

    async def run():
        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not trace.get_current_span().get_span_context().is_valid

    asyncio.run(run())
    spans = search_spans(exporter)
    assert len(spans) == 1
    assert spans[0].status.status_code.name == "ERROR"
    assert spans[0].attributes["error.type"].endswith("CancelledError")


@pytest.mark.parametrize(
    "module_name,class_name,method",
    [
        ("lancedb.remote.table", "RemoteTable", "add"),
        ("lancedb.remote.table", "RemoteTable", "update"),
        ("lancedb.remote.table", "RemoteTable", "delete"),
        ("lancedb.remote.db", "RemoteDBConnection", "create_table"),
        ("lancedb.remote.db", "RemoteDBConnection", "open_table"),
        ("lancedb.remote.db", "RemoteDBConnection", "drop_table"),
    ],
)
def test_remote_concrete_hook_registration(
    telemetry, monkeypatch, module_name, class_name, method
):
    # Replace only the operation body before instrumenting: verifies the concrete
    # remote class is hooked without constructing a client or opening a socket.
    import importlib

    cls = getattr(importlib.import_module(module_name), class_name)
    exporter, provider, instrumentor = telemetry
    instrumentor.uninstrument()
    sentinel = object()
    received = []

    def operation(self, *args, **kwargs):
        received.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(cls, method, operation)
    instrumentor.instrument(tracer_provider=provider)
    instance = object.__new__(cls)
    assert getattr(instance, method)("documents", synthetic=True) is sentinel
    assert received == [(("documents",), {"synthetic": True})]
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "lancedb " + method
    instrumentor.uninstrument()
    assert vars(cls)[method] is operation


def test_remote_actual_delegation(telemetry):
    from lancedb.remote.table import RemoteTable

    if "LOOP.run" not in inspect.getsource(RemoteTable.add):
        pytest.skip("Older remote SDK uses a different transport")
    # Exercise the real RemoteTable.add body against a fake async transport.
    table = object.__new__(RemoteTable)
    table._table = SimpleNamespace(
        add=AsyncMock(return_value="added"), name="documents"
    )
    assert table.add(ROWS) == "added"
    table._table.add.assert_awaited_once()
    assert len(telemetry[0].get_finished_spans()) == 1


def test_async_fts_hybrid_and_empty(tmp_path, telemetry):
    from lancedb.query import AsyncQuery

    if not hasattr(AsyncQuery, "nearest_to_text"):
        pytest.skip("FTS unavailable on this release")

    async def run():
        db = await lancedb.connect_async(str(tmp_path / "queries"))
        table = await db.create_table("documents", ROWS)
        from lancedb.index import FTS

        await table.create_index("text", config=FTS())
        exporter = telemetry[0]
        for query in (
            table.query().nearest_to_text("violet"),
            table.query().where("id = 999"),
        ):
            exporter.clear()
            result = await query.to_list()
            assert len(search_spans(exporter)) == 1
            assert search_spans(exporter)[0].attributes[
                "db.vector.results.count"
            ] == len(result)
        query = table.vector_search([1.0, 0.0])
        if hasattr(query, "nearest_to_text"):
            exporter.clear()
            result = await query.nearest_to_text("violet").to_list()
            assert result
            assert len(search_spans(exporter)) == 1
        exporter.clear()
        reader = await table.query().to_batches()
        await reader.aclose()
        assert len(search_spans(exporter)) == 1
        assert search_spans(exporter)[0].status.status_code.name == "UNSET"

    asyncio.run(run())


def test_restore_exact_class_dicts(telemetry):
    from lancedb.db import AsyncConnection, LanceDBConnection
    from lancedb.query import AsyncQueryBase, LanceQueryBuilder, LanceVectorQueryBuilder

    _, provider, instrumentor = telemetry
    instrumentor.uninstrument()
    classes = (
        AsyncConnection,
        LanceDBConnection,
        AsyncQueryBase,
        LanceQueryBuilder,
        LanceVectorQueryBuilder,
    )
    originals = {cls: dict(vars(cls)) for cls in classes}
    instrumentor.instrument(tracer_provider=provider)
    instrumentor.uninstrument()
    for cls, original in originals.items():
        assert dict(vars(cls)) == original


def test_metadata_failure_does_not_break_operation(telemetry, caplog):
    import logging

    from traceai_lancedb._wrappers import OperationWrapper

    class Table:
        @property
        def name(self):
            raise ValueError("PRIVATE_METADATA_ERROR")

    exporter, provider, _ = telemetry
    sentinel = object()
    with caplog.at_level(logging.DEBUG, logger="traceai_lancedb._wrappers"):
        result = OperationWrapper(provider.get_tracer("test"), "add", "add")(
            lambda: sentinel, Table(), (), {}
        )
    assert result is sentinel
    assert "metadata unavailable (ValueError)" in caplog.text
    assert "PRIVATE_METADATA_ERROR" not in caplog.text
    assert len(exporter.get_finished_spans()) == 1


def test_failed_install_rolls_back(telemetry, monkeypatch):
    from lancedb.table import LanceTable

    _, provider, instrumentor = telemetry
    instrumentor.uninstrument()
    originals = dict(vars(LanceTable))
    patch = instrumentor._patch
    count = 0

    def fail_third_patch(cls, method, wrapper):
        nonlocal count
        count += 1
        if count == 3:
            raise RuntimeError("synthetic patch failure")
        patch(cls, method, wrapper)

    monkeypatch.setattr(instrumentor, "_patch", fail_third_patch)
    with pytest.raises(RuntimeError, match="synthetic patch failure"):
        instrumentor.instrument(tracer_provider=provider)
    assert dict(vars(LanceTable)) == originals
    assert not instrumentor.is_instrumented_by_opentelemetry
