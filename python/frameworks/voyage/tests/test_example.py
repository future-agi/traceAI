"""examples/embed_and_rerank.py runs end to end through the shared harness.

harness.run starts the example as a subprocess with FI_BASE_URL pointed at
harness.Receiver and VOYAGE_BASE_URL pointed at the loopback Voyage fake, so
the real exporter and the real voyageai client both run. Nothing calls Voyage.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("voyageai", reason="voyageai must be installed to run the example")
pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")

PACKAGE = Path(__file__).resolve().parents[1]
PYTHON_ROOT = PACKAGE.parents[1]
sys.path.insert(0, str(PYTHON_ROOT / "tests"))

from _support import (  # noqa: E402
    CONTENT_MARKERS,
    SCORE_MARKERS,
    VECTOR_MARKER,
    VOYAGE_KEY,
    FakeVoyage,
)
from harness import Receiver, _flatten_attributes, run  # noqa: E402

EXAMPLE = PACKAGE / "examples" / "embed_and_rerank.py"
FI_API_KEY = "placeholder-fi-api-key"
FI_SECRET_KEY = "placeholder-fi-secret-key"


def test_example_exports_one_embedding_and_one_reranker_span():
    with FakeVoyage() as fake, Receiver() as receiver:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("FI_", "VOYAGE_", "OTEL_"))
        }
        env.update(
            {
                "FI_BASE_URL": receiver.origin,
                "FI_API_KEY": FI_API_KEY,
                "FI_SECRET_KEY": FI_SECRET_KEY,
                "VOYAGE_API_KEY": VOYAGE_KEY,
                "VOYAGE_BASE_URL": fake.base_url,
                "PYTHONPATH": os.pathsep.join(
                    [str(PACKAGE), str(PYTHON_ROOT), os.environ.get("PYTHONPATH", "")]
                ),
            }
        )
        result = run([sys.executable, str(EXAMPLE)], env, None, timeout=120)
        spans = receiver.spans()
        exports = receiver.requests()
        paths = fake.paths()

    assert not result.timed_out
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert paths == ["/v1/embeddings", "/v1/rerank"]

    assert sorted(span["name"] for span in spans) == ["voyage.embed", "voyage.rerank"]
    by_name = {span["name"]: _flatten_attributes(span["attributes"]) for span in spans}
    assert by_name["voyage.embed"]["gen_ai.span.kind"] == "EMBEDDING"
    assert int(by_name["voyage.embed"]["voyage.embedding.count"]) == 2
    assert int(by_name["voyage.embed"]["gen_ai.usage.total_tokens"]) > 0
    assert by_name["voyage.rerank"]["gen_ai.span.kind"] == "RERANKER"
    assert int(by_name["voyage.rerank"]["reranker.top_k"]) == 2
    for span in spans:
        assert span["status"]["code"] == "STATUS_CODE_OK"

    assert exports
    for export in exports:
        assert export["path"] == "/tracer/v1/traces"
        assert export["headers"]["x-api-key"] == FI_API_KEY
        assert export["headers"]["x-secret-key"] == FI_SECRET_KEY
        for resource in export["resource_attributes"]:
            assert resource["project_name"] == "voyage-example"
            assert resource["project_type"] == "observe"

    # The example's own documents and query, its key, vectors and scores
    # stay off the wire (content capture is off by default).
    wire = json.dumps(spans)
    example_markers = ("EXAMPLE-DOC", "EXAMPLE-QUERY")
    for marker in (VOYAGE_KEY, VECTOR_MARKER) + example_markers + CONTENT_MARKERS + SCORE_MARKERS:
        assert marker not in wire, marker
