"""Privacy must hold on the documented register() path, before export."""

import asyncio
import inspect
import json

import pytest
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.instrumentation import TraceConfig

from traceai_chromadb import ChromaDBInstrumentor


def call(collection, name, *args, **kwargs):
    result = getattr(collection, name)(*args, **kwargs)
    return asyncio.run(result) if inspect.isawaitable(result) else result


@pytest.mark.parametrize("source", ["environment", "config", "inherited_errors"])
@pytest.mark.parametrize(
    "hidden",
    [
        (),
        ("inputs",),
        ("outputs",),
        ("error_details",),
        ("inputs", "outputs", "error_details"),
    ],
)
def test_privacy(collection, in_memory_span_exporter, monkeypatch, source, hidden):
    for name in ("inputs", "outputs", "error_details"):
        monkeypatch.setenv(
            f"FI_HIDE_{name.upper()}",
            str(source != "config" and name in hidden).lower(),
        )
    if source == "inherited_errors":
        monkeypatch.delenv("FI_HIDE_ERROR_DETAILS", raising=False)
    monkeypatch.setenv("FI_API_KEY", "synthetic-key")
    monkeypatch.setenv("FI_SECRET_KEY", "synthetic-secret")
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:9")
    provider = register(
        project_name="synthetic-chroma-privacy",
        project_type=ProjectType.OBSERVE,
        batch=False,
        verbose=False,
        set_global_tracer_provider=False,
        span_exporter=in_memory_span_exporter,
    )
    options = {}
    if source == "config":
        options["config"] = TraceConfig(
            **{
                f"hide_{name}": name in hidden
                for name in ("inputs", "outputs", "error_details")
            }
        )
    instrumentor = ChromaDBInstrumentor()
    instrumentor.instrument(tracer_provider=provider, **options)
    try:
        for operation in ("query", "get", "delete"):
            kwargs = {
                "where": {"secret": "synthetic-filter-secret"},
                "where_document": {"$contains": "synthetic-document-secret"},
            }
            if operation == "query":
                kwargs["query_embeddings"] = [[1.0, 0.0, 0.0]]
            call(collection, operation, **kwargs)
            span = in_memory_span_exporter.get_finished_spans()[-1]
            assert ("db.namespace" not in span.attributes) == ("inputs" in hidden)
            assert ("db.vector.collection.name" not in span.attributes) == (
                "inputs" in hidden
            )
            assert ("db.vector.query.filter" not in span.attributes) == (
                "inputs" in hidden
            )
            assert ("db.vector.query.where_document" not in span.attributes) == (
                "inputs" in hidden
            )
            if operation == "query":
                assert ("db.vector.results.ids" not in span.attributes) == (
                    "outputs" in hidden
                )
                assert ("db.vector.results.scores" not in span.attributes) == (
                    "outputs" in hidden
                )
                assert span.attributes["db.vector.results.count"] == 2
        if hasattr(collection, "search"):
            from chromadb import Search

            call(collection, "search", Search().limit(1))
            span = in_memory_span_exporter.get_finished_spans()[-1]
            assert ("db.vector.results.ids" not in span.attributes) == (
                "outputs" in hidden
            )
            assert ("db.vector.results.scores" not in span.attributes) == (
                "outputs" in hidden
            )
        error = RuntimeError("synthetic-private-error")
        collection._client.error = error
        with pytest.raises(RuntimeError) as caught:
            call(collection, "count")
        assert caught.value is error
        span = in_memory_span_exporter.get_finished_spans()[-1]
        # These cases explicitly set all three flags, including false.
        redact_error = (
            bool({"inputs", "outputs"}.intersection(hidden))
            if source == "inherited_errors"
            else "error_details" in hidden
        )
        assert span.status.status_code.name == "ERROR"
        assert len(span.events) == 1
        event = span.events[0].attributes
        assert "RuntimeError" in event["exception.type"]
        if redact_error:
            assert "exception.message" not in event
            assert "exception.stacktrace" not in event
            assert span.status.description is None
            assert "synthetic-private-error" not in json.dumps(dict(span.attributes))
        else:
            assert event["exception.message"] == str(error)
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
