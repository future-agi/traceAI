"""A stream left open at interpreter exit still exports its span (PRD AC-09).

register() installs an atexit hook that shuts the tracer provider down. A
stream still referenced from a module global is only finalised after atexit
hooks run, too late for the exporter, so the instrumentor ends open stream
spans from its own atexit hook, which runs first (atexit is LIFO and
instrument() follows register()).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("exa_py", reason="exa-py must be installed to run the script")
pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")

from _support import FakeExa  # noqa: E402
from harness import Receiver, _flatten_attributes, run  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1]
PYTHON_ROOT = PACKAGE.parents[1]

SCRIPT = """
import os

from exa_py import Exa
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_exa import ExaInstrumentor

provider = register(
    project_name="exa-exit", project_type=ProjectType.OBSERVE, batch={batch}, verbose=False
)
ExaInstrumentor().instrument(tracer_provider=provider)
client = Exa(api_key="exa-dummy-key", base_url=os.environ["EXA_BASE_URL"])
stream = client.stream_search("left open at exit")
next(iter(stream))
# The script ends here with the stream still referenced by a module global.
"""


@pytest.mark.parametrize("batch", [True, False], ids=["batch", "simple"])
def test_stream_left_open_at_exit_exports_a_cancelled_span(tmp_path, batch):
    script = tmp_path / "left_open.py"
    script.write_text(SCRIPT.format(batch=batch))
    with FakeExa() as fake, Receiver() as receiver:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("FI_", "EXA_", "OTEL_"))
        }
        env.update(
            {
                "FI_BASE_URL": receiver.origin,
                "FI_API_KEY": "placeholder-fi-api-key",
                "FI_SECRET_KEY": "placeholder-fi-secret-key",
                "EXA_BASE_URL": fake.origin,
                "PYTHONPATH": os.pathsep.join(
                    [str(PACKAGE), str(PYTHON_ROOT), os.environ.get("PYTHONPATH", "")]
                ),
            }
        )
        result = run([sys.executable, str(script)], env, None, timeout=120)
        spans = receiver.spans()

    assert not result.timed_out
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert [span["name"] for span in spans] == ["exa.search"]
    (span,) = spans
    assert span["status"]["code"] == "STATUS_CODE_ERROR"
    assert span["status"]["message"] == "cancelled"
    assert _flatten_attributes(span["attributes"])["exa.cancelled"] is True
