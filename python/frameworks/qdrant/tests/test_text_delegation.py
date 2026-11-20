"""Real SDK text delegation with synthetic embeddings; no model download."""

import asyncio
import inspect
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest
import qdrant_client
from qdrant_client import QdrantClient, models


TEXT_CLIENTS = [QdrantClient]
if hasattr(qdrant_client, "AsyncQdrantClient"):
    TEXT_CLIENTS.append(qdrant_client.AsyncQdrantClient)


async def call(method, *args, **kwargs):
    result = method(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


@pytest.mark.parametrize("pure_python", [False, True])
def test_text_delegation_with_both_wrapt_implementations(pure_python, tmp_path):
    env = os.environ.copy()
    env.pop("WRAPT_DISABLE_EXTENSIONS", None)
    if pure_python:
        env["WRAPT_DISABLE_EXTENSIONS"] = "true"
    script = """
import socket
import sys
import wrapt
from wrapt.wrappers import FunctionWrapper as PythonFunctionWrapper
import pytest
assert (wrapt.FunctionWrapper is PythonFunctionWrapper) == (sys.argv[2] == 'true')
def deny_connect(*args, **kwargs):
    raise AssertionError('offline Qdrant contract attempted a connection')
socket.socket.connect = deny_connect
socket.socket.connect_ex = deny_connect
raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider', sys.argv[1]]))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script,
         str(Path(__file__).resolve()) + "::test_text_and_batch_delegation",
         str(pure_python).lower()],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("client_type,hybrid", [
    (client_type, hybrid)
    for client_type in TEXT_CLIENTS
    for hybrid in ([False, True] if hasattr(client_type, "query") and hasattr(client_type, "get_sparse_vector_field_name") else [False])
])
async def test_text_and_batch_delegation(client_type, hybrid, instrument_qdrant, in_memory_span_exporter):
    client = client_type(":memory:")
    try:
        if not hasattr(client, "query"):
            # SDK 1.20 removed these APIs. The current-query tests cover their
            # replacement; instrumentation must not restore removed methods.
            assert not hasattr(client, "query_batch")
            assert hasattr(client, "query_points")
            return
        name_fn = getattr(client, "get_vector_field_name", None) or client._get_vector_field_name
        name = name_fn()
        sparse_config = {}
        vectors = {name: [1.0, 0.0, 0.0]}
        if hybrid:
            client._sparse_embedding_model_name = "offline"
            client.get_sparse_vector_field_name = lambda: "sparse"
            sparse_config = {"sparse_vectors_config": {"sparse": models.SparseVectorParams()}}
            vectors["sparse"] = models.SparseVector(indices=[1], values=[1.0])
            def sparse_embed(documents):
                count = len(documents) if isinstance(documents, list) else 1
                return [SimpleNamespace(indices=np.array([1]), values=np.array([1.0]))] * count
            client._get_or_init_sparse_model = lambda **kwargs: SimpleNamespace(embed=sparse_embed)
        await call(client.create_collection, "docs", vectors_config={name: models.VectorParams(
            size=3, distance=models.Distance.COSINE)}, **sparse_config)
        await call(client.upsert, "docs", points=[models.PointStruct(
            id=1, vector=vectors, payload={"document": "hello"})])
        independent_calls = []
        def vector_query():
            return client.search("docs", query_vector=models.NamedVector(
                name=name, vector=[1.0, 0.0, 0.0]), limit=1)
        def embed(**kwargs):
            # A sync embedding callback can start an independent async task.
            # Context inheritance must not suppress that operation's span.
            result = vector_query()
            independent_calls.append(asyncio.create_task(result) if inspect.isawaitable(result) else result)
            queries = kwargs["query"]
            count = len(queries) if isinstance(queries, list) else 1
            return [np.array([1.0, 0.0, 0.0])] * count
        client._get_or_init_model = lambda **kwargs: SimpleNamespace(query_embed=embed)
        for method, params in ((client.query, {"query_text": "hello"}),
                               (client.query_batch, {"query_texts": ["hello", "world"]})):
            in_memory_span_exporter.clear()
            independent_calls.clear()
            result = await call(method, "docs", **params, limit=1)
            if "query_texts" in params:
                assert len(result) == 2 and all(r[0].document == "hello" for r in result)
            else:
                assert result[0].document == "hello"
            for pending in independent_calls:
                records = await pending if inspect.isawaitable(pending) else pending
                assert records[0].id == 1
            spans = in_memory_span_exporter.get_finished_spans()
            roots = [s for s in spans if s.parent is None]
            assert len(roots) == 1
            root = roots[0]
            # No extra span for the SDK's direct search/search_batch delegation.
            assert len(spans) == 1 + len(independent_calls)
            assert all(s.parent.span_id == root.context.span_id for s in spans if s is not root)
            assert root.attributes["db.vector.results.count"] == (2 if "query_texts" in params else 1)
            if "query_texts" in params:
                assert root.attributes["db.vector.query.batch_size"] == 2
            assert all(s.status.status_code.name == "OK" for s in spans)
    finally:
        await call(client.close)
