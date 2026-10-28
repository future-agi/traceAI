"""A held create span survives SIGTERM / SIGINT under ``register()`` (PRD J2.4, AC-02).

``fi_instrumentation.register()`` installs SIGTERM and SIGINT handlers that
shut the tracer provider down and then call ``sys.exit(0)``. Any span that
ends after that shutdown is dropped by the processors, so a create span that
is still held open on its prediction must be ended before the provider shuts
down, not from an ``atexit`` hook that runs afterwards.

The script runs as a subprocess with the real ``register()`` OTLP exporter
(batch and simple processors) and the real replicate client against the
loopback fake. It creates a prediction, keeps it in a module global, signals
that it is ready, and sleeps until the test sends the signal. A released
variant drops the prediction first, so the span waits in the release queue.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("replicate", reason="replicate must be installed to run the script")
pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")

import fi_instrumentation  # noqa: E402

from _support import SLOW_MODEL, FakeReplicate  # noqa: E402
from harness import Receiver, _flatten_attributes  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1]
PYTHON_ROOT = PACKAGE.parents[1]
# The subprocess must export through the fi_instrumentation this test imported.
FI_ROOT = Path(fi_instrumentation.__file__).resolve().parents[1]

SCRIPT = """
import os
import time

import replicate
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_replicate import ReplicateInstrumentor

provider = register(
    project_name="replicate-signal",
    project_type=ProjectType.OBSERVE,
    batch=os.environ["BATCH"] == "1",
    verbose=False,
)
ReplicateInstrumentor().instrument(tracer_provider=provider)
client = replicate.Client(base_url=os.environ["REPLICATE_BASE_URL"])
PREDICTION = client.predictions.create(model="{model}", input={{}})
returned = time.time_ns()
status = PREDICTION.status
if os.environ["RELEASE"] == "1":
    del PREDICTION  # released: its finish is queued, not run, in __del__
ready = os.environ["READY_FILE"]
with open(ready + ".tmp", "w") as handle:
    handle.write("{{0}} {{1}}".format(status, returned))
os.replace(ready + ".tmp", ready)
while True:
    time.sleep(0.05)
""".format(model=SLOW_MODEL)


def _run_until_signalled(tmp_path, fake, receiver, signum, batch, release=False):
    script = tmp_path / "hold_then_signal.py"
    script.write_text(SCRIPT)
    ready = tmp_path / "ready"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("FI_", "REPLICATE_", "OTEL_"))
    }
    env.update(
        {
            "BATCH": "1" if batch else "0",
            "RELEASE": "1" if release else "0",
            "READY_FILE": str(ready),
            "FI_BASE_URL": receiver.origin,
            "FI_API_KEY": "placeholder-fi-api-key",
            "FI_SECRET_KEY": "placeholder-fi-secret-key",
            "REPLICATE_BASE_URL": fake.origin,
            "REPLICATE_API_TOKEN": "r8_signal-placeholder-token",
            "PYTHONPATH": os.pathsep.join(
                [str(PACKAGE), str(FI_ROOT), str(PYTHON_ROOT), os.environ.get("PYTHONPATH", "")]
            ),
        }
    )
    process = subprocess.Popen(
        [sys.executable, str(script)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        deadline = time.monotonic() + 60
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if not ready.exists():
            process.kill()
            _, stderr = process.communicate()
            pytest.fail("the script never got ready: " + stderr.decode(errors="replace"))
        process.send_signal(signum)
        _, stderr = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    status, returned = ready.read_text().split()
    return process.returncode, stderr.decode(errors="replace"), status, int(returned)


CASES = [
    pytest.param(signal.SIGTERM, True, False, id="SIGTERM-batch"),
    pytest.param(signal.SIGTERM, False, False, id="SIGTERM-simple"),
    pytest.param(signal.SIGINT, True, False, id="SIGINT-batch"),
    pytest.param(signal.SIGINT, False, False, id="SIGINT-simple"),
    pytest.param(signal.SIGTERM, True, True, id="SIGTERM-batch-released"),
    pytest.param(signal.SIGTERM, False, True, id="SIGTERM-simple-released"),
]


@pytest.mark.parametrize("signum, batch, release", CASES)
def test_signal_under_register_exports_the_held_create_span_with_its_create_status(
    tmp_path, signum, batch, release
):
    with FakeReplicate() as fake, Receiver() as receiver:
        returncode, stderr, status, returned = _run_until_signalled(
            tmp_path, fake, receiver, signum, batch, release
        )
        spans = receiver.spans()

    assert returncode == 0, stderr  # register()'s handler exits with 0
    assert status == "starting"
    assert fake.paths("GET") == []  # nothing polled on the way out
    assert [span["name"] for span in spans] == ["replicate.predictions.create"], stderr
    (span,) = spans
    values = _flatten_attributes(span["attributes"])
    assert values["replicate.prediction.status"] == "starting"
    assert values["replicate.prediction.id"] == "pred0001"
    assert values["gen_ai.request.model"] == SLOW_MODEL
    assert values["gen_ai.span.kind"] == "CHAIN"
    assert "gen_ai.span.leaked" not in values  # ended by us, not swept up at shutdown
    assert span["status"]["code"] == "STATUS_CODE_OK"
    # Ended as of create time, not when the signal arrived.
    assert int(span["endTimeUnixNano"]) <= returned
