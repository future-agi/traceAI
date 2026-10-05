"""A process that exits between create and wait still exports the create span (PRD J2.4).

The script runs as a subprocess through harness.run with the real
``fi_instrumentation.register()`` exporter (batch processor) and the real
replicate client against the loopback fake. It creates a prediction, keeps it
in a module global, and exits without calling wait().
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("replicate", reason="replicate must be installed to run the script")
pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")

from _support import SLOW_MODEL, FakeReplicate  # noqa: E402
from harness import Receiver, _flatten_attributes, run  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1]
PYTHON_ROOT = PACKAGE.parents[1]

SCRIPT = """
import os

import replicate
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_replicate import ReplicateInstrumentor

provider = register(project_name="replicate-exit", project_type=ProjectType.OBSERVE, verbose=False)
ReplicateInstrumentor().instrument(tracer_provider=provider)
client = replicate.Client(base_url=os.environ["REPLICATE_BASE_URL"])
PREDICTION = client.predictions.create(model="{model}", input={{}})
print(PREDICTION.status)
""".format(model=SLOW_MODEL)


def test_exit_without_wait_exports_the_create_span_with_its_create_status(tmp_path):
    script = tmp_path / "exit_without_wait.py"
    script.write_text(SCRIPT)
    with FakeReplicate() as fake, Receiver() as receiver:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("FI_", "REPLICATE_", "OTEL_"))
        }
        env.update(
            {
                "FI_BASE_URL": receiver.origin,
                "FI_API_KEY": "placeholder-fi-api-key",
                "FI_SECRET_KEY": "placeholder-fi-secret-key",
                "REPLICATE_BASE_URL": fake.origin,
                "REPLICATE_API_TOKEN": "r8_exit-placeholder-token",
                "PYTHONPATH": os.pathsep.join(
                    [str(PACKAGE), str(PYTHON_ROOT), os.environ.get("PYTHONPATH", "")]
                ),
            }
        )
        result = run([sys.executable, str(script)], env, None, timeout=120)
        spans = receiver.spans()

    assert not result.timed_out
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout.decode().strip() == "starting"
    assert fake.paths("GET") == []  # nothing polled on the way out
    assert [span["name"] for span in spans] == ["replicate.predictions.create"]
    values = _flatten_attributes(spans[0]["attributes"])
    assert values["replicate.prediction.status"] == "starting"
    assert values["gen_ai.request.model"] == SLOW_MODEL
    assert spans[0]["status"]["code"] == "STATUS_CODE_OK"
