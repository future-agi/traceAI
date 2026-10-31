"""Offline driver-contract tests; no PostgreSQL or vendor credentials required."""

import asyncio
from types import SimpleNamespace

import psycopg
import psycopg2
import pytest
from opentelemetry import trace
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_pgvector import PgVectorInstrumentor
from traceai_pgvector._wrappers import query_attributes


class SyntheticDriverError(Exception):
    pass


class SyntheticCursor:
    def __init__(self, connection):
        self._connection = connection
        self.calls = []

    @property
    def connection(self):
        return self._connection

    def execute(self, query, params=None, **kwargs):
        self.calls.append((query, params, kwargs))
        if params == "FAIL":
            raise SyntheticDriverError("secret-error-message 'private vector'")
        return self

    def executemany(self, query, params_seq=None, **kwargs):
        if params_seq is None:
            params_seq = kwargs["vars_list"]
        self.calls.append((query, list(params_seq), kwargs))
        return self

    def __del__(self):
        pass


# Real psycopg2 C-extension subclass: proves no immutable type is modified and
# custom cursor factories remain usable through the maintained driver route.
class Cursor2(SyntheticCursor, psycopg2.extensions.cursor):
    pass


class Cursor3(SyntheticCursor, psycopg.Cursor):
    pass


class AsyncCursor3(SyntheticCursor, psycopg.AsyncCursor):
    async def execute(self, *args, **kwargs):
        await asyncio.sleep(0)
        return SyntheticCursor.execute(self, *args, **kwargs)

    async def executemany(self, *args, **kwargs):
        await asyncio.sleep(0)
        return SyntheticCursor.executemany(self, *args, **kwargs)


class Connection:
    def __init__(self, cursor_factory):
        self.cursor_factory = cursor_factory
        self.info = SimpleNamespace(
            dbname="secret-db", user="secret-user", host="secret-host", port=5432
        )

    def cursor(self):
        return self.cursor_factory(self)


@pytest.fixture
def telemetry(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def connect(*args, **kwargs):
        return Connection(kwargs["cursor_factory"])

    async def connect_async(*args, **kwargs):
        return Connection(kwargs["cursor_factory"])

    monkeypatch.setattr(psycopg2, "connect", connect)
    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(connect))
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", classmethod(connect_async))
    instrumentor = PgVectorInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        yield provider, exporter, instrumentor
    finally:
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
        provider.shutdown()


@pytest.mark.parametrize(
    "driver,factory",
    [(psycopg2, Cursor2), (psycopg, Cursor3), (psycopg.Connection, Cursor3)],
)
@pytest.mark.parametrize("keyword", [False, True])
def test_execute_preserves_result_and_emits_one_span(
    telemetry, driver, factory, keyword
):
    provider, exporter, _ = telemetry
    cursor = driver.connect(cursor_factory=factory).cursor()
    query = "SELECT * FROM secret_table ORDER BY embedding <-> %s LIMIT 5"
    parameters = ([0.1, 0.2],)
    with provider.get_tracer("test").start_as_current_span("parent") as parent:
        if keyword:
            result = cursor.execute(query=query, params=parameters)
        else:
            result = cursor.execute(query, parameters)
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    span = spans[0]
    assert result is cursor
    assert cursor.calls == [(query, parameters, {})]
    assert span.parent.span_id == parent.get_span_context().span_id
    assert span.kind == trace.SpanKind.CLIENT
    assert span.name == "postgresql select"
    assert span.attributes["db.vector.distance_type"] == "l2_distance"
    assert span.attributes["db.vector.query.top_k"] == 5
    assert "secret" not in str(span.attributes)
    assert not span.events


@pytest.mark.parametrize("driver,factory", [(psycopg2, Cursor2), (psycopg, Cursor3)])
def test_insert_and_iterable_executemany(telemetry, driver, factory):
    _, exporter, _ = telemetry
    cursor = driver.connect(cursor_factory=factory).cursor()
    query = "INSERT INTO items (embedding) VALUES (%s)"
    assert cursor.execute(query=query, params=([1, 2],)) is cursor
    values = iter([([1, 2],), ([3, 4],)])
    assert cursor.executemany(query, values) is cursor
    assert cursor.calls[-1][1] == [([1, 2],), ([3, 4],)]
    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert all(s.attributes["db.operation.name"] == "insert" for s in spans)
    # Without a vector operator/type context, do not pretend to know the schema.
    assert all("db.vector.extension" not in s.attributes for s in spans)


def test_async_execute_batch_and_context_isolation(telemetry):
    provider, exporter, _ = telemetry

    async def run():
        connection = await psycopg.AsyncConnection.connect(cursor_factory=AsyncCursor3)

        async def query(operator):
            cursor = connection.cursor()
            with provider.get_tracer("test").start_as_current_span("parent"):
                assert (
                    await cursor.execute(
                        query=f"SELECT embedding {operator} %s", params=([1, 2],)
                    )
                    is cursor
                )
                assert (
                    await cursor.executemany(
                        query="INSERT INTO items VALUES (%s)",
                        params_seq=iter([(1,), (2,)]),
                    )
                    is cursor
                )

        await asyncio.gather(query("<->"), query("<=>"))

    asyncio.run(run())
    spans = exporter.get_finished_spans()
    assert len(spans) == 6
    queries = [s for s in spans if s.name == "postgresql select"]
    assert {s.attributes["db.vector.distance_type"] for s in queries} == {
        "l2_distance",
        "cosine_distance",
    }
    assert len({s.parent.span_id for s in queries}) == 2


@pytest.mark.parametrize("driver,factory", [(psycopg2, Cursor2), (psycopg, Cursor3)])
def test_errors_rethrown_without_details(telemetry, driver, factory):
    _, exporter, _ = telemetry
    cursor = driver.connect(cursor_factory=factory).cursor()
    with pytest.raises(SyntheticDriverError, match="secret-error-message"):
        cursor.execute("SELECT 'secret-query' <-> %s", "FAIL")
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code == trace.StatusCode.ERROR
    assert span.status.description is None
    assert not span.events
    assert "secret" not in str(span.attributes)


def test_suppression_and_double_instrument(telemetry):
    provider, exporter, instrumentor = telemetry
    instrumentor.instrument(tracer_provider=provider)
    cursor = psycopg2.connect(cursor_factory=Cursor2).cursor()
    with suppress_instrumentation():
        cursor.execute("SELECT 1")
    assert not exporter.get_finished_spans()
    cursor.execute("SELECT 1")
    assert len(exporter.get_finished_spans()) == 1


def test_uninstrument_stops_old_connections_and_reinstrument_is_clean(telemetry):
    provider, exporter, instrumentor = telemetry
    cursor = psycopg2.connect(cursor_factory=Cursor2).cursor()
    instrumentor.uninstrument()
    cursor.execute("SELECT 1")
    assert not exporter.get_finished_spans()
    assert not Psycopg2Instrumentor().is_instrumented_by_opentelemetry
    assert not PsycopgInstrumentor().is_instrumented_by_opentelemetry
    instrumentor.instrument(tracer_provider=provider)
    cursor.execute("SELECT 1")
    assert not exporter.get_finished_spans()
    psycopg2.connect(cursor_factory=Cursor2).cursor().execute("SELECT 1")
    assert len(exporter.get_finished_spans()) == 1


def test_existing_driver_ownership_is_not_stolen(telemetry):
    provider, _, instrumentor = telemetry
    instrumentor.uninstrument()
    driver = Psycopg2Instrumentor()
    driver.instrument(tracer_provider=provider)
    try:
        with pytest.raises(RuntimeError, match="already enabled"):
            instrumentor.instrument(tracer_provider=provider)
        assert driver.is_instrumented_by_opentelemetry
        assert not instrumentor.is_instrumented_by_opentelemetry
        assert not PsycopgInstrumentor().is_instrumented_by_opentelemetry
    finally:
        driver.uninstrument()


def test_original_connects_and_cursors_restored():
    before = (
        psycopg2.connect,
        psycopg.connect,
        psycopg.Connection.connect.__func__,
        psycopg.AsyncConnection.connect.__func__,
        psycopg2.extensions.connection.cursor,
        psycopg.Cursor.execute,
        psycopg.AsyncCursor.execute,
    )
    instrumentor = PgVectorInstrumentor()
    instrumentor.instrument(tracer_provider=TracerProvider())
    instrumentor.uninstrument()
    after = (
        psycopg2.connect,
        psycopg.connect,
        psycopg.Connection.connect.__func__,
        psycopg.AsyncConnection.connect.__func__,
        psycopg2.extensions.connection.cursor,
        psycopg.Cursor.execute,
        psycopg.AsyncCursor.execute,
    )
    assert all(original is restored for original, restored in zip(before, after))


@pytest.mark.parametrize(
    "query",
    [
        "SELECT '<->' FROM items",
        'SELECT "<->" FROM items',
        "SELECT 1 /* <-> */",
        "SELECT 1 -- <->\n",
        "SELECT $$<->$$",
        "SELECT $tag$<->$tag$",
        "SELECT 1 /* outer /* <-> */ comment */",
    ],
)
def test_literals_and_comments_are_not_vector_operations(query):
    assert "db.vector.operator" not in query_attributes(query)


@pytest.mark.parametrize(
    "operator,distance",
    [
        ("<->", "l2_distance"),
        ("<=>", "cosine_distance"),
        ("<#>", "inner_product"),
        ("<+>", "l1_distance"),
        ("<~>", "hamming_distance"),
        ("<%>", "jaccard_distance"),
    ],
)
def test_all_vector_operators(operator, distance):
    attributes = query_attributes(
        f"SELECT * FROM items ORDER BY embedding {operator} %s LIMIT 5"
    )
    assert attributes["db.vector.distance_type"] == distance
    assert attributes["db.vector.query.type"] == "similarity_search"
    assert attributes["db.vector.query.top_k"] == 5


def test_bytes_and_composed(telemetry):
    _, exporter, _ = telemetry
    cursor = psycopg.connect(cursor_factory=Cursor3).cursor()
    cursor.execute(b"SELECT embedding <=> %s", ([1, 2],))
    cursor.execute(psycopg.sql.SQL("SELECT embedding <#> %s"), ([1, 2],))
    assert [
        s.attributes["db.vector.distance_type"] for s in exporter.get_finished_spans()
    ] == ["cosine_distance", "inner_product"]


@pytest.mark.parametrize("query", [None, "", b"", object()])
def test_unknown_query_metadata_is_empty(query):
    assert query_attributes(query) == {}


def test_failed_metadata_conversion_logs_without_private_error(caplog):
    class Query:
        def as_string(self, cursor):
            raise ValueError("secret query cannot render")

    assert query_attributes(Query()) == {}
    assert "Could not enrich pgvector query metadata" in caplog.text
    assert "secret" not in caplog.text


def test_partial_install_rolls_back(telemetry, monkeypatch):
    import traceai_pgvector

    provider, _, instrumentor = telemetry
    instrumentor.uninstrument()
    originals = (psycopg2.connect, psycopg.connect)
    real_wrap = traceai_pgvector.wrap_function_wrapper
    calls = 0

    def failing_wrap(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic installation failure")
        return real_wrap(*args)

    monkeypatch.setattr(traceai_pgvector, "wrap_function_wrapper", failing_wrap)
    with pytest.raises(RuntimeError, match="synthetic installation failure"):
        instrumentor.instrument(tracer_provider=provider)
    assert psycopg2.connect is originals[0]
    assert psycopg.connect is originals[1]
    assert not instrumentor.is_instrumented_by_opentelemetry
    assert not PsycopgInstrumentor().is_instrumented_by_opentelemetry
    assert not Psycopg2Instrumentor().is_instrumented_by_opentelemetry
