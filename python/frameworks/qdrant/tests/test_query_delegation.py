"""Real text/vector operations must not acquire duplicate delegation spans."""

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import numpy as np
from qdrant_client import QdrantClient, models


@pytest.mark.parametrize("pure_python", [False, True])
def test_delegation_with_both_wrapt_implementations(pure_python, tmp_path):
    # wrapt chooses its implementation at import time; a fresh interpreter is
    # required. Run the identical real-SDK callback/direct-operation contract.
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
         str(Path(__file__).resolve()) + "::test_text_query_and_independent_vector_operations",
         str(pure_python).lower()],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_text_query_and_independent_vector_operations(instrument_qdrant, in_memory_span_exporter):
    client = QdrantClient(":memory:")
    try:
        if not hasattr(client, "query"):
            # New SDKs removed query/search; exercise the real replacement,
            # rather than claiming a text API exists or skipping the test.
            client.create_collection("docs", vectors_config=models.VectorParams(
                size=3, distance=models.Distance.COSINE))
            client.upsert("docs", points=[models.PointStruct(id=1, vector=[1.0, 0.0, 0.0])])
            in_memory_span_exporter.clear()
            assert client.query_points("docs", query=[1.0, 0.0, 0.0], limit=1).points[0].id == 1
            (span,) = in_memory_span_exporter.get_finished_spans()
            assert span.attributes["db.vector.results.count"] == 1
            return
        vector_field_name = getattr(client, "get_vector_field_name", None) or client._get_vector_field_name
        name = vector_field_name()
        client.create_collection("docs", vectors_config={name: models.VectorParams(
            size=3, distance=models.Distance.COSINE)})
        client.upsert("docs", points=[models.PointStruct(id=1, vector={name: [1.0, 0.0, 0.0]},
            payload={"document": "hello"})])
        def vector_query():
            if hasattr(client, "search"):
                return client.search("docs", query_vector=models.NamedVector(
                    name=name, vector=[1.0, 0.0, 0.0]), limit=1)
            return client.query_points("docs", query=[1.0, 0.0, 0.0], using=name, limit=1).points
        calls = []
        def embed(**kwargs):
            # This independent query occurs inside a user embedding callback,
            # not the SDK's internal delegate. It must retain its own span.
            calls.append(vector_query())
            return [np.array([1.0, 0.0, 0.0])]
        client._get_or_init_model = lambda **kwargs: SimpleNamespace(query_embed=embed)
        in_memory_span_exporter.clear()
        results = client.query("docs", query_text="hello", limit=1)
        assert [r.document for r in results] == ["hello"]
        assert len(calls) == 1 and calls[0][0].id == 1
        spans = in_memory_span_exporter.get_finished_spans()
        assert len(spans) == 2  # One text operation plus the independent callback.
        nested, text = spans
        assert nested.parent.span_id == text.context.span_id
        assert all(s.attributes["db.vector.results.count"] == 1 for s in spans)
        assert all(s.attributes["db.namespace"] == "docs" for s in spans)
        assert all(s.attributes["db.operation.name"] == "query" for s in spans)
        assert all(s.status.status_code.name == "OK" for s in spans)
        in_memory_span_exporter.clear()
        assert vector_query()[0].id == 1
        (direct,) = in_memory_span_exporter.get_finished_spans()
        assert direct.parent is None
        assert direct.attributes["db.vector.results.count"] == 1
        if hasattr(client, "query_points"):
            in_memory_span_exporter.clear()
            assert client.query_points("docs", query=[1.0, 0.0, 0.0], using=name, limit=1).points[0].id == 1
            assert len(in_memory_span_exporter.get_finished_spans()) == 1
    finally:
        client.close()
