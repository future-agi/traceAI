"""README Option 1 with the real fi_instrumentation and OpenTelemetry exporters.

register() then configure_strands_tracing(tracer_provider=...), sending spans to collectors on
127.0.0.1. Each case runs strands_export_scenario.py in a fresh interpreter: conftest.py mocks
opentelemetry, fi_instrumentation and strands for every test in this process.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

_SCENARIO = Path(__file__).with_name("strands_export_scenario.py")


def _run(mode: str) -> Dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "FI_"))}
    result = subprocess.run(
        [sys.executable, str(_SCENARIO), mode],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_spans_reach_future_agi_once_when_strands_otlp_points_there_too() -> None:
    # The README's environment section points OTEL_EXPORTER_OTLP_* at the backend. The provider
    # already exports to Future AGI, so Strands' own OTLP exporter is not added: without that,
    # every span arrived twice on fi-instrumentation-otel releases with the TH-8727 fix.
    got = _run("future-agi")

    assert got["span_processors"] == 1
    assert got["future_agi"] == ["span-0", "span-1", "span-2"]
    assert got["other"] == []


def test_spans_reach_future_agi_and_a_second_collector() -> None:
    got = _run("other")
    if got["register_marks_its_exporter"]:
        pytest.skip(
            "the installed fi-instrumentation-otel drops register()'s exporter when Strands adds "
            "its own (fixed by TH-8727)"
        )

    assert got["span_processors"] == 2
    assert got["future_agi"] == ["span-0", "span-1", "span-2"]
    assert got["other"] == ["span-0", "span-1", "span-2"]
