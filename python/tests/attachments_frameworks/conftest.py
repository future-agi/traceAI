"""Framework attachment tests reuse the provider lane's loopback harness (synthetic data only).

- ``f01``: TLS loopback F01 attachment server; ``provider``: plain-HTTP loopback that
  answers like the OpenAI API, so real framework + real provider SDK paths run end to end;
- ``spans``: an OpenTelemetry provider with an in-memory exporter.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fi_instrumentation.attachments.media_capture")

sys.path.insert(0, str(Path(__file__).parent))

_spec = importlib.util.spec_from_file_location(
    "_attachments_provider_harness", Path(__file__).parent.parent / "attachments_providers" / "conftest.py")
_harness = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _harness
_spec.loader.exec_module(_harness)

# Fixtures (pytest registers them from this module's namespace) and helpers.
clean_env = _harness.clean_env
f01 = _harness.f01
provider = _harness.provider
spans = _harness.spans
PDF = _harness.PDF
b64 = _harness.b64
flush = _harness.flush
png = _harness.png
refs = _harness.refs
wait_uploads = _harness.wait_uploads
wav = _harness.wav


def jpeg(seed=0) -> bytes:
    """Bytes with a JPEG signature; only the container magic is checked by the SDK side."""
    return b"\xff\xd8\xff\xe0" + bytes((seed + i) % 256 for i in range(60)) + b"\xff\xd9"


def content_puts(f01):
    """Content PUT bodies (actual uploaded bytes) in arrival order."""
    return f01.payloads


def by_name(exporter, fragment):
    return [span for span in exporter.get_finished_spans() if fragment in span.name]


def all_refs(exporter):
    found = []
    for span in exporter.get_finished_spans():
        found.extend((span.name, ref) for ref in refs(span))
    return found
