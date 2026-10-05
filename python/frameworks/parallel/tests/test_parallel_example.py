"""examples/search_and_extract.py runs end to end through the shared harness.

harness.run starts the example as a subprocess with FI_BASE_URL pointed at
harness.Receiver and PARALLEL_BASE_URL pointed at the loopback Parallel fake,
so the real exporter and the real parallel-web client both run. The process
exits on its own; the spans it exported prove the flush at exit (AC-08).
Nothing calls api.parallel.ai.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("parallel", reason="parallel-web must be installed to run the example")
pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))

from harness import Receiver, _flatten_attributes, run  # noqa: E402

from _parallel_support import CONTENT_MARKERS, PARALLEL_KEY, FakeParallel  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1]
EXAMPLE = PACKAGE / "examples" / "search_and_extract.py"
FI_API_KEY = "placeholder-fi-api-key"
FI_SECRET_KEY = "placeholder-fi-secret-key"


def test_example_exports_one_search_and_one_extract_span():
    with FakeParallel() as fake, Receiver() as receiver:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("FI_", "PARALLEL_", "OTEL_"))
        }
        env.update(
            {
                "FI_BASE_URL": receiver.origin,
                "FI_API_KEY": FI_API_KEY,
                "FI_SECRET_KEY": FI_SECRET_KEY,
                # parallel-web reads both of these itself.
                "PARALLEL_API_KEY": PARALLEL_KEY,
                "PARALLEL_BASE_URL": fake.origin,
                # The subprocess imports exactly what this test process imports.
                "PYTHONPATH": os.pathsep.join(sys.path),
            }
        )
        result = run([sys.executable, str(EXAMPLE)], env, None, timeout=120)
        spans = receiver.spans()
        exports = receiver.requests()

    assert not result.timed_out
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert fake.paths() == ["/v1/search", "/v1/extract"]
    assert fake.calls[0][1]["x-api-key"] == PARALLEL_KEY

    assert [span["name"] for span in spans] == ["parallel.search", "parallel.extract"]
    search, extract = (_flatten_attributes(span["attributes"]) for span in spans)
    assert search["fi.span.kind"] == extract["fi.span.kind"] == "RETRIEVER"
    assert search["input.value"] == "traceAI Parallel example"
    assert search["parallel.mode"] == "turbo"
    assert int(search["parallel.result_count"]) == 2
    assert int(extract["parallel.url_count"]) == 1
    assert int(extract["parallel.result_count"]) == 1
    assert all(span["status"]["code"] == "STATUS_CODE_OK" for span in spans)

    assert exports
    for export in exports:
        assert export["path"] == "/tracer/v1/traces"
        assert export["headers"]["x-api-key"] == FI_API_KEY
        assert export["headers"]["x-secret-key"] == FI_SECRET_KEY
        for resource in export["resource_attributes"]:
            assert resource["project_name"] == "parallel-example"
            assert resource["project_type"] == "observe"

    wire = json.dumps(spans)
    assert PARALLEL_KEY not in wire
    for marker in CONTENT_MARKERS:
        assert marker not in wire, marker
