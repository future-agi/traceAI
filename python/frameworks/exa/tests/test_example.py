"""examples/search.py runs end to end through the shared harness.

harness.run starts the example as a subprocess with FI_BASE_URL pointed at
harness.Receiver and EXA_BASE_URL pointed at the loopback Exa fake, so the
real exporter and the real exa-py client both run. Nothing calls Exa.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("exa_py", reason="exa-py must be installed to run the example")
pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")

from _support import CONTENT_MARKERS, FakeExa  # noqa: E402
from harness import Receiver, _flatten_attributes, run  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1]
PYTHON_ROOT = PACKAGE.parents[1]
EXAMPLE = PACKAGE / "examples" / "search.py"
FI_API_KEY = "placeholder-fi-api-key"
FI_SECRET_KEY = "placeholder-fi-secret-key"


def test_example_exports_one_exa_search_span():
    with FakeExa() as fake, Receiver() as receiver:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("FI_", "EXA_", "OTEL_"))
        }
        env.update(
            {
                "FI_BASE_URL": receiver.origin,
                "FI_API_KEY": FI_API_KEY,
                "FI_SECRET_KEY": FI_SECRET_KEY,
                "EXA_BASE_URL": fake.origin,
                "PYTHONPATH": os.pathsep.join(
                    [str(PACKAGE), str(PYTHON_ROOT), os.environ.get("PYTHONPATH", "")]
                ),
            }
        )
        result = run([sys.executable, str(EXAMPLE)], env, None, timeout=120)
        spans = receiver.spans()
        exports = receiver.requests()

    assert not result.timed_out
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert fake.paths() == ["/search"]

    assert [span["name"] for span in spans] == ["exa.search"]
    span = spans[0]
    values = _flatten_attributes(span["attributes"])
    assert values["fi.span.kind"] == "RETRIEVER"
    assert values["fi.retrieval.query"] == "TraceAI Exa instrumentation example"
    assert values["input.value"] == "TraceAI Exa instrumentation example"
    assert int(values["fi.retrieval.document_count"]) == 1
    assert span["status"]["code"] == "STATUS_CODE_OK"

    assert exports
    for export in exports:
        assert export["path"] == "/tracer/v1/traces"
        assert export["headers"]["x-api-key"] == FI_API_KEY
        for resource in export["resource_attributes"]:
            assert resource["project_name"] == "exa-example"

    wire = json.dumps(spans)
    assert "exa-dummy-key" not in wire
    for marker in CONTENT_MARKERS:
        assert marker not in wire
