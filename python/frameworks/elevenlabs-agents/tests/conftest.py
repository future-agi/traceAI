import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fi_instrumentation import TraceConfig

sys.path.insert(0, str(Path(__file__).resolve().parent))

from traceai_elevenlabs_agents.config import ForwarderConfig  # noqa: E402
from traceai_elevenlabs_agents.forwarder import Forwarder  # noqa: E402
from traceai_elevenlabs_agents.store import ForwardedStore  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MARKER = "zqx-th8231-privacy-marker-7f3a"
WEBHOOK_SECRET = "wsec_test_9c1d7e0b5a8f4d2e9b6c3a1f0e7d4c2b"
FI_API_KEY = "fi_test_api_key_0123456789abcdef"
FI_SECRET_KEY = "fi_test_secret_key_fedcba9876543210"
#: The four standard switches the privacy proof turns on (FI_HIDE_INPUTS, FI_HIDE_OUTPUTS,
#: FI_HIDE_INPUT_MESSAGES, FI_HIDE_OUTPUT_MESSAGES).
HIDE_ENV = ("FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS", "FI_HIDE_INPUT_MESSAGES", "FI_HIDE_OUTPUT_MESSAGES")
# Values the stand-in payload carries that must never be exported in ANY mode: host,
# path, user name, email, stack trace, signed URL, a foreign project.
NEVER_NEEDLES = (
    "alice",
    "/Users/",
    "/home/",
    "example.com",
    "Traceback",
    "X-Goog-Signature",
    "someone-elses-project",
    "host.name",
    "process.executable.path",
    "exception.stacktrace",
)


def hide_all() -> TraceConfig:
    return TraceConfig(hide_inputs=True, hide_outputs=True, hide_input_messages=True, hide_output_messages=True)


@pytest.fixture(autouse=True)
def _standard_trace_config(monkeypatch):
    """Every test starts from TraceConfig defaults, whatever the developer's shell exports."""
    for name in list(os.environ):
        if name.startswith("FI_HIDE_") or name in ("FI_PII_REDACTION", "FI_BASE64_IMAGE_MAX_LENGTH"):
            monkeypatch.delenv(name, raising=False)


def load_fixture(name: str, marker: str = MARKER) -> Any:
    text = (FIXTURES / name).read_text(encoding="utf-8").replace("{{MARKER}}", marker)
    return json.loads(text)


def raw(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


class RecordingExporter:
    """In-memory exporter: what would have been sent to the collector."""

    def __init__(self, fail: Exception = None) -> None:
        self.batches: List[Dict[str, Any]] = []
        self.fail = fail

    def export(self, export_request):
        if self.fail is not None:
            raise self.fail
        self.batches.append(json.loads(json.dumps(export_request)))
        return {}

    def spans(self):
        out = []
        for batch in self.batches:
            for rs in batch["resourceSpans"]:
                for ss in rs["scopeSpans"]:
                    out.extend(ss["spans"])
        return out


def make_config(**overrides) -> ForwarderConfig:
    values = dict(
        fi_api_key=FI_API_KEY,
        fi_secret_key=FI_SECRET_KEY,
        project_name="th8231-unit",
        fi_base_url="http://127.0.0.1:9",
        webhook_secret=WEBHOOK_SECRET,
        state_path=":memory:",
    )
    values.update(overrides)
    return ForwarderConfig(**values)


@pytest.fixture
def make_forwarder():
    created = []

    def _make(exporter=None, **overrides):
        config = make_config(**overrides)
        forwarder = Forwarder(config, exporter=exporter or RecordingExporter(), store=ForwardedStore(config.state_path))
        created.append(forwarder)
        return forwarder

    yield _make
    for forwarder in created:
        forwarder.store.close()


@pytest.fixture
def log_capture(caplog):
    caplog.set_level(logging.DEBUG, logger="traceai_elevenlabs_agents")
    return caplog
