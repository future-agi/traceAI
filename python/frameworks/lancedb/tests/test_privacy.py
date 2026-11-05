"""Privacy configuration works with an ordinary OpenTelemetry provider."""

import asyncio

import lancedb
import pytest
from fi_instrumentation import TraceConfig

TABLE_NAME = "customer-415-555-2671"
REDACTED_NAME = "customer-<PHONE_NUMBER>"
ROWS = [{"id": 1, "vector": [1.0, 0.0], "text": "PRIVATE_ROW_CONTENT"}]
OPERATIONS = [
    "create_table", "open_table", "add", "update", "search", "delete", "drop_table"
]


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    "configuration",
    ["environment", "environment-config", "explicit", "explicit-disable"],
)
def test_table_metadata_respects_pii_configuration(
    tmp_path, telemetry, monkeypatch, configuration, asynchronous
):
    exporter, provider, instrumentor = telemetry
    instrumentor.uninstrument()
    # Configure after importing the package: defaults must be read at instrument().
    monkeypatch.setenv(
        "FI_PII_REDACTION", "false" if configuration == "explicit" else "true"
    )
    options = {}
    if configuration == "environment-config":
        options["config"] = TraceConfig()
    elif configuration == "explicit":
        options["config"] = TraceConfig(pii_redaction=True)
    elif configuration == "explicit-disable":
        options["config"] = TraceConfig(pii_redaction=False)
    instrumentor.instrument(tracer_provider=provider, **options)
    path = str(tmp_path / "db")

    async def run_async():
        db = await lancedb.connect_async(path)
        await db.create_table(TABLE_NAME, ROWS)
        table = await db.open_table(TABLE_NAME)
        await table.add(ROWS)
        await table.update(where="id = 1", updates={"text": "PRIVATE_UPDATE_CONTENT"})
        query = table.vector_search([1.0, 0.0]).limit(1)
        assert len(await query.to_list()) == 1
        await table.delete("id = 1")
        await db.drop_table(TABLE_NAME)
        # Older native async builders do not retain a Python table reference.
        return getattr(query, "_table", None) is not None

    search_has_table_metadata = True
    if asynchronous:
        search_has_table_metadata = asyncio.run(run_async())
    else:
        db = lancedb.connect(path)
        db.create_table(TABLE_NAME, ROWS)
        table = db.open_table(TABLE_NAME)
        table.add(ROWS)
        table.update(where="id = 1", values={"text": "PRIVATE_UPDATE_CONTENT"})
        assert len(table.search([1.0, 0.0]).limit(1).to_list()) == 1
        table.delete("id = 1")
        db.drop_table(TABLE_NAME)

    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == [
        "lancedb " + operation for operation in OPERATIONS
    ]
    expected = TABLE_NAME if configuration == "explicit-disable" else REDACTED_NAME
    for span in spans:
        if span.name == "lancedb search" and not search_has_table_metadata:
            assert "db.namespace" not in span.attributes
            assert "db.vector.collection.name" not in span.attributes
        else:
            assert span.attributes["db.namespace"] == expected
            assert span.attributes["db.vector.collection.name"] == expected
    serialized = " ".join(span.to_json() for span in spans)
    assert "PRIVATE_ROW_CONTENT" not in serialized
    assert "PRIVATE_UPDATE_CONTENT" not in serialized
    assert "id = 1" not in serialized
    if configuration != "explicit-disable":
        assert TABLE_NAME not in serialized


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_redaction_keeps_error_details_private(
    tmp_path, telemetry, monkeypatch, asynchronous
):
    exporter, provider, instrumentor = telemetry
    instrumentor.uninstrument()
    monkeypatch.setenv("FI_PII_REDACTION", "true")
    # Even explicitly opting into error details cannot weaken LanceDB's policy.
    monkeypatch.setenv("FI_HIDE_ERROR_DETAILS", "false")
    instrumentor.instrument(tracer_provider=provider)
    path = str(tmp_path / "db")

    async def run_async():
        db = await lancedb.connect_async(path)
        await db.open_table(TABLE_NAME)

    with pytest.raises((ValueError, RuntimeError)):
        if asynchronous:
            asyncio.run(run_async())
        else:
            lancedb.connect(path).open_table(TABLE_NAME)

    (span,) = exporter.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert span.status.description is None
    assert span.attributes["db.namespace"] == REDACTED_NAME
    assert span.attributes["error.type"]
    assert len(span.events) == 1
    assert set(span.events[0].attributes) == {"exception.type"}
    assert TABLE_NAME not in span.to_json()


def test_pii_redacted_before_sampling(tmp_path, telemetry):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON

    exporter, _, instrumentor = telemetry
    instrumentor.uninstrument()
    seen = []

    class ObservingSampler:
        def should_sample(
            self, parent_context, trace_id, name, kind=None, attributes=None,
            links=None, trace_state=None,
        ):
            seen.append(dict(attributes or {}))
            return ALWAYS_ON.should_sample(
                parent_context, trace_id, name, kind, attributes, links, trace_state
            )

        def get_description(self):
            return "ObservingSampler"

    provider = TracerProvider(sampler=ObservingSampler())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        instrumentor.instrument(
            tracer_provider=provider, config=TraceConfig(pii_redaction=True)
        )
        lancedb.connect(str(tmp_path / "db")).create_table(TABLE_NAME, ROWS)
        assert len(exporter.get_finished_spans()) == 1
        assert seen[0]["db.namespace"] == REDACTED_NAME
        assert seen[0]["db.vector.collection.name"] == REDACTED_NAME
    finally:
        provider.shutdown()


def test_pii_redacted_on_otlp_wire(tmp_path, telemetry):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    from .test_execution import otlp_receiver

    _, provider, instrumentor = telemetry
    instrumentor.uninstrument()
    instrumentor.instrument(
        tracer_provider=provider, config=TraceConfig(pii_redaction=True)
    )
    with otlp_receiver() as (endpoint, requests):
        provider.add_span_processor(
            SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
        )
        table = lancedb.connect(str(tmp_path / "db")).create_table(TABLE_NAME, ROWS)
        assert len(table.search([1.0, 0.0]).to_list()) == 1
        wire = b"".join(request.SerializeToString() for request in requests)
        assert len(requests) == 2
        assert TABLE_NAME.encode() not in wire
        assert REDACTED_NAME.encode() in wire
        assert b"PRIVATE_ROW_CONTENT" not in wire
