"""Shared helpers: the provider-attachments harness, loaded by path (one F01 loopback for every lane)."""

import importlib.util
import json
import sys
from pathlib import Path

_NAME = "_attachments_providers_harness"
if _NAME in sys.modules:
    harness = sys.modules[_NAME]
else:
    _path = Path(__file__).resolve().parent.parent / "attachments_providers" / "conftest.py"
    _spec = importlib.util.spec_from_file_location(_NAME, _path)
    harness = importlib.util.module_from_spec(_spec)
    sys.modules[_NAME] = harness
    _spec.loader.exec_module(harness)

png, wav, b64, refs, wait_uploads, flush, PDF = (harness.png, harness.wav, harness.b64, harness.refs,
                                                 harness.wait_uploads, harness.flush, harness.PDF)


def data_uri(mime, data):
    return "data:%s;base64,%s" % (mime, b64(data))


def uploaded(f01):
    """(mime_type, direction, purpose, role, tool_call_id) of each initialize body, sorted."""
    return sorted((u.get("mime_type"), u.get("direction"), u.get("purpose"), u.get("role"),
                   (u.get("correlation") or {}).get("tool_call_id") or u.get("tool_call_id"))
                  for u in f01.uploads)


def span_named(exporter, name):
    found = [s for s in exporter.get_finished_spans() if s.name == name]
    assert found, [s.name for s in exporter.get_finished_spans()]
    return found[-1]


def no_bytes(exporter, data):
    """No exported span attribute carries the media's base64 text."""
    text = b64(data)[:40]
    for span in exporter.get_finished_spans():
        for value in (span.attributes or {}).values():
            assert text not in json.dumps(value, default=str), span.name
