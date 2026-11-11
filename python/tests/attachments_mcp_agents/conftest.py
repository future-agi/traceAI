"""Loopback harness for MCP and agent tool-media tests: synthetic data only.

Reuses the provider-attachments harness (``tests/attachments_providers``):
``f01`` is a TLS loopback F01 attachment server and ``spans`` an in-memory
OpenTelemetry provider. Nothing here reaches a network beyond 127.0.0.1.
"""

import sys
from pathlib import Path

import pytest

pytest.importorskip("fi_instrumentation.attachments.tool_media")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import harness  # noqa: E402

# Fixtures, registered by being attributes of this conftest.
clean_env = harness.clean_env
f01 = harness.f01
spans = harness.spans


@pytest.fixture(autouse=True)
def mock_all_http_requests():
    """Overrides python/tests/conftest.py: these suites talk to real loopback servers through requests."""
    yield None
