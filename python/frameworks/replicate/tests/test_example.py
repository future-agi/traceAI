"""examples/run_model.py runs end to end through the shared harness.

harness.run starts the example as a subprocess with FI_BASE_URL pointed at
harness.Receiver and REPLICATE_BASE_URL pointed at the loopback Replicate
fake, so the real exporter and the real replicate client (through the
module-level ``replicate.run``) both run. Nothing calls Replicate.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("replicate", reason="replicate must be installed to run the example")
pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")

from _support import TEXT_OUTPUT, FakeReplicate  # noqa: E402
from harness import Receiver, _flatten_attributes, run  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1]
PYTHON_ROOT = PACKAGE.parents[1]
EXAMPLE = PACKAGE / "examples" / "run_model.py"
FI_API_KEY = "placeholder-fi-api-key"
FI_SECRET_KEY = "placeholder-fi-secret-key"
TOKEN = "r8_example-placeholder-token"


def test_example_exports_one_replicate_run_span():
    with FakeReplicate() as fake, Receiver() as receiver:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("FI_", "REPLICATE_", "OTEL_"))
        }
        env.update(
            {
                "FI_BASE_URL": receiver.origin,
                "FI_API_KEY": FI_API_KEY,
                "FI_SECRET_KEY": FI_SECRET_KEY,
                "REPLICATE_BASE_URL": fake.origin,
                "REPLICATE_API_TOKEN": TOKEN,
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
    assert TEXT_OUTPUT in result.stdout.decode()
    assert fake.paths() == ["/v1/models/meta/meta-llama-3-8b-instruct/predictions"]
    assert fake.request_headers("POST", fake.paths()[0])["authorization"] == "Bearer " + TOKEN

    assert [span["name"] for span in spans] == ["replicate.run"]
    values = _flatten_attributes(spans[0]["attributes"])
    assert values["gen_ai.provider.name"] == "replicate"
    assert values["gen_ai.request.model"] == "meta/meta-llama-3-8b-instruct"
    assert values["gen_ai.span.kind"] == "LLM"
    assert values["replicate.prediction.status"] == "succeeded"
    assert spans[0]["status"]["code"] == "STATUS_CODE_OK"

    assert exports
    for export in exports:
        assert export["path"] == "/tracer/v1/traces"
        assert export["headers"]["x-api-key"] == FI_API_KEY
        assert export["headers"]["x-secret-key"] == FI_SECRET_KEY
        for resource in export["resource_attributes"]:
            assert resource["project_name"] == "replicate-example"
            assert resource["project_type"] == "observe"

    assert TOKEN not in json.dumps(spans)
