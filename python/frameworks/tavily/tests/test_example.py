"""examples/search.py runs end to end through the shared harness.

harness.run starts the example as a subprocess with FI_BASE_URL pointed at
harness.Receiver and TAVILY_BASE_URL at the loopback Tavily fake, so the
real exporter and the real tavily-python client both run. Nothing calls
api.tavily.com.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("tavily", reason="tavily-python must be installed to run the example")
pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")

from _tavily_fake import CONTENT_MARKERS, TAVILY_KEY, FakeTavily  # noqa: E402
from harness import Receiver, _flatten_attributes, run  # noqa: E402

import fi_instrumentation  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1]
PYTHON_ROOT = PACKAGE.parents[1]
# Subprocesses import fi_instrumentation from where this process did: the
# source tree in a repo run, the installed wheel in a published-release run.
FI_ROOT = Path(fi_instrumentation.__file__).resolve().parents[1]
EXAMPLE = PACKAGE / "examples" / "search.py"
FI_API_KEY = "placeholder-fi-api-key"
FI_SECRET_KEY = "placeholder-fi-secret-key"


def test_example_exports_one_tavily_search_span():
    assert EXAMPLE.is_file(), EXAMPLE
    with FakeTavily() as fake, Receiver() as receiver:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("FI_", "TAVILY_", "OTEL_"))
        }
        env.update(
            {
                "FI_BASE_URL": receiver.origin,
                "FI_API_KEY": FI_API_KEY,
                "FI_SECRET_KEY": FI_SECRET_KEY,
                "TAVILY_API_KEY": TAVILY_KEY,
                "TAVILY_BASE_URL": fake.origin,
                "PYTHONPATH": os.pathsep.join(
                    [str(PACKAGE), str(FI_ROOT), os.environ.get("PYTHONPATH", "")]
                ),
            }
        )
        result = run([sys.executable, str(EXAMPLE)], env, None, timeout=120)
        spans = receiver.spans()
        exports = receiver.requests()
        paths = fake.paths()

    assert not result.timed_out
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert paths == ["/search"]
    # The example prints only the number of results.
    assert result.stdout.decode().strip() == "results: 2"

    assert [span["name"] for span in spans] == ["tavily.search"]
    values = _flatten_attributes(spans[0]["attributes"])
    assert values["gen_ai.span.kind"] == "TOOL"
    assert values["input.value"] == "What is OpenTelemetry?"
    assert int(values["tavily.result_count"]) == 2
    assert spans[0]["status"]["code"] == "STATUS_CODE_OK"

    assert exports
    for export in exports:
        assert export["path"] == "/tracer/v1/traces"
        assert export["headers"]["x-api-key"] == FI_API_KEY
        assert export["headers"]["x-secret-key"] == FI_SECRET_KEY
        for resource in export["resource_attributes"]:
            assert resource["project_name"] == "tavily-example"
            assert resource["project_type"] == "observe"

    wire = json.dumps(spans)
    assert TAVILY_KEY not in wire
    for marker in CONTENT_MARKERS:
        assert marker not in wire
