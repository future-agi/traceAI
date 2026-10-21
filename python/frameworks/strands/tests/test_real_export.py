"""README Option 1 with the real fi_instrumentation and OpenTelemetry exporters.

register() then configure_strands_tracing(tracer_provider=...), sending spans to collectors on
127.0.0.1. Each case runs strands_export_scenario.py in a fresh interpreter: conftest.py mocks
opentelemetry, fi_instrumentation and strands for every test in this process.

The cases hold on every fi-instrumentation-otel release this package allows. On 1.1.0 and earlier,
register() marks its exporter as a default the next add_span_processor() call replaces; there
Strands' exporter is added as before and takes its place, so it is the one that reaches Future AGI.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

_SCENARIO = Path(__file__).with_name("strands_export_scenario.py")
_RESULT_PREFIX = "SCENARIO_RESULT "
_SPANS = ["span-0", "span-1", "span-2"]


def _run(mode: str, *options: str) -> Dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "_OTEL_", "FI_"))}
    result = subprocess.run(
        [sys.executable, str(_SCENARIO), mode, *options],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.startswith(_RESULT_PREFIX)]
    assert len(lines) == 1, result.stdout
    return json.loads(lines[0][len(_RESULT_PREFIX):])


def test_spans_reach_future_agi_once_when_strands_otlp_points_there_with_the_same_keys() -> None:
    # The provider and Strands' exporter both send the Future AGI keys, so the exact URL+headers
    # match means Strands' exporter would only send a duplicate.
    got = _run("future-agi", "--keys", "both")

    assert got["span_processors"] == 1
    assert got["future_agi"] == _SPANS
    assert got["other"] == []


def test_strands_exporter_is_kept_when_it_omits_provider_auth_headers() -> None:
    # The provider carries Future AGI's keys but Strands' exporter does not. Even at the same URL,
    # the header sets differ, so the exporter must stay (its copy is rejected by the collector).
    got = _run("future-agi", "--keys", "fi")

    if got["register_marks_its_exporter"]:
        pytest.skip("the installed fi-instrumentation-otel replaces register()'s exporter")

    assert got["span_processors"] == 2
    assert sorted(got["future_agi"]) == sorted(_SPANS * 2)
    assert got["future_agi_with_keys"] == _SPANS


def test_a_base_endpoint_resolving_to_future_agi_is_recognised() -> None:
    # OTEL_EXPORTER_OTLP_ENDPOINT=<FI_BASE_URL>/tracer/ resolves, in the installed upstream
    # exporter, to register()'s traces URL.
    got = _run("future-agi-base", "--keys", "both")

    assert got["span_processors"] == 1
    assert got["future_agi"] == _SPANS
    assert got["future_agi_with_keys"] == _SPANS


@pytest.mark.parametrize(
    "option", ["--console", "--processor-after"], ids=["console-exporter", "processor-after"]
)
def test_spans_still_reach_future_agi_when_a_processor_is_added_after_strands(
    option: str,
) -> None:
    # A processor added after configure_strands_tracing() (Strands' console exporter, or one the
    # application adds) must not leave Future AGI without an exporter, whichever
    # fi-instrumentation-otel is installed.
    got = _run("future-agi", "--keys", "both", option)

    assert got["future_agi"] == _SPANS
    assert got["future_agi_with_keys"] == _SPANS
    if option == "--processor-after":
        assert got["added_after"] == _SPANS


def test_strands_exporter_is_kept_when_only_it_carries_the_future_agi_keys() -> None:
    # register() ran without FI_API_KEY/FI_SECRET_KEY, so its exporter sends no keys; the keys
    # reach Strands' exporter through otlp_headers. Skipping it would leave no accepted copy.
    got = _run("future-agi", "--keys", "strands")

    assert got["future_agi_with_keys"] == _SPANS
    if not got["register_marks_its_exporter"]:
        # register()'s exporter is kept too, and sends its copies without keys.
        assert got["span_processors"] == 2
        assert sorted(got["future_agi"]) == sorted(_SPANS * 2)


def test_spans_reach_future_agi_and_a_second_collector() -> None:
    got = _run("other")
    if got["register_marks_its_exporter"]:
        pytest.skip(
            "the installed fi-instrumentation-otel drops register()'s exporter when Strands adds "
            "its own (fixed by TH-8727)"
        )

    assert got["span_processors"] == 2
    assert got["future_agi"] == _SPANS
    assert got["other"] == _SPANS
