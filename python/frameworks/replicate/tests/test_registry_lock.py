"""Releasing a held-open prediction inside a registry operation must not deadlock.

The cyclic GC can run a ``PendingPrediction.__del__`` at any allocation,
including while this thread holds the pending-span registry's lock. That
``__del__`` only queues the finish; ``uninstrument()`` drains the queue, ends
the span and removes it from the registry. The scenario runs in a subprocess
so that a regression is a timeout, not a hung test session.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("replicate", reason="replicate must be installed to run the script")

from _support import SLOW_MODEL, FakeReplicate  # noqa: E402
from harness import run  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1]
PYTHON_ROOT = PACKAGE.parents[1]

SCRIPT = """
import os

import replicate
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_replicate import ReplicateInstrumentor

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
instrumentor = ReplicateInstrumentor()
instrumentor.instrument(tracer_provider=provider)
client = replicate.Client(api_token="r8_lock-placeholder-token", base_url=os.environ["FAKE"])
prediction = client.predictions.create(model="{model}", input={{}})
with instrumentor._registry._lock:  # as if the cyclic GC ran inside a registry call
    del prediction
instrumentor.uninstrument()
print([span.name for span in exporter.get_finished_spans()])
""".format(model=SLOW_MODEL)


def test_releasing_a_pending_prediction_under_the_registry_lock_does_not_deadlock(tmp_path):
    script = tmp_path / "release_under_lock.py"
    script.write_text(SCRIPT)
    with FakeReplicate() as fake:
        env = dict(os.environ)
        env.update(
            {
                "FAKE": fake.origin,
                "PYTHONPATH": os.pathsep.join(
                    [str(PACKAGE), str(PYTHON_ROOT), os.environ.get("PYTHONPATH", "")]
                ),
            }
        )
        result = run([sys.executable, str(script)], env, None, timeout=30)

    assert not result.timed_out, "deadlocked: the registry lock is not re-entrant"
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout.decode().strip() == "['replicate.predictions.create']"
