"""Offline fixtures: real Collection validation with an in-process transport."""

import asyncio
import inspect
from copy import deepcopy
from uuid import UUID

import pytest
from chromadb.api.models.Collection import Collection
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_chromadb import ChromaDBInstrumentor


class Transport:
    def __init__(self):
        self.calls = []
        self.error = None
        self.response = None

    def __getattr__(self, name):
        if not name.startswith("_"):
            raise AttributeError(name)

        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if self.error is not None:
                raise self.error
            if self.response is not None:
                return deepcopy(self.response)
            if name == "_count":
                return 2
            if name in ("_add", "_update", "_upsert", "_delete"):
                return None
            nested = name in ("_query", "_search")
            return {
                "ids": [["a", "b"]] if nested else ["a", "b"],
                "distances": [[0.0, 0.5]] if nested else None,
                "scores": [[0.0, 0.5]] if nested else None,
                "documents": [["private-document", "other"]] if nested else None,
                "embeddings": None,
                "metadatas": None,
                "uris": None,
                "data": None,
                "included": ["documents", "distances"],
            }

        return call


class AsyncTransport(Transport):
    def __getattr__(self, name):
        call = super().__getattr__(name)

        async def async_call(*args, **kwargs):
            await asyncio.sleep(0)
            return call(*args, **kwargs)

        return async_call


def make_collection(collection_class, transport):
    values = {
        "id": UUID("00000000-0000-0000-0000-000000000001"),
        "name": "test-collection",
        "configuration_json": {},
        "serialized_schema": None,
        "metadata": None,
        "dimension": 3,
        "tenant": "default_tenant",
        "database": "default_database",
    }
    if "model" in inspect.signature(collection_class).parameters:
        from chromadb.types import Collection as Model

        model = Model(
            **{
                key: value
                for key, value in values.items()
                if key in inspect.signature(Model).parameters
            }
        )
        return collection_class(client=transport, model=model, embedding_function=None)
    return collection_class(
        client=transport, id=values["id"], name=values["name"], embedding_function=None
    )


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def collection(request):
    if request.param:
        module = pytest.importorskip("chromadb.api.models.AsyncCollection")
        return make_collection(module.AsyncCollection, AsyncTransport())
    return make_collection(Collection, Transport())


@pytest.fixture
def in_memory_span_exporter():
    return InMemorySpanExporter()


@pytest.fixture
def tracer_provider(in_memory_span_exporter):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(in_memory_span_exporter))
    yield provider
    provider.shutdown()


@pytest.fixture
def instrument_chromadb(tracer_provider):
    instrumentor = ChromaDBInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)
    yield instrumentor
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
