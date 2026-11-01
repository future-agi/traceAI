"""Do not import mocked legacy dependencies: each case uses real installed wheels."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCENARIO = Path(__file__).with_name("scenario.py")


def run(*options, conventions="", environment=None):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("FI_", "OTEL_", "_OTEL_", "OPENAI_", "AWS_"))
    }
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    if conventions:
        env["OTEL_SEMCONV_STABILITY_OPT_IN"] = conventions
    env.update(environment or {})
    result = subprocess.run(
        [sys.executable, "-I", str(SCENARIO), *options],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"status": "PASS"' in result.stdout


@pytest.mark.parametrize("wire", ["protobuf", "json"])
@pytest.mark.parametrize("gzip", [False, True])
@pytest.mark.parametrize("capture", [False, True])
def test_real_agent_tool_repeated_turns_and_wire(wire, gzip, capture):
    run(
        "--wire",
        wire,
        *(["--gzip"] if gzip else []),
        *(["--capture"] if capture else []),
    )


@pytest.mark.parametrize(
    "conventions",
    [
        "gen_ai_latest_experimental",
        "gen_ai_latest_experimental,gen_ai_span_attributes_only",
    ],
)
def test_native_semconv_modes(conventions):
    run("--capture", "--batch", "--async-stream", conventions=conventions)


@pytest.mark.parametrize(
    "flag",
    [
        "hide_inputs",
        "hide_outputs",
        "hide_input_messages",
        "hide_output_messages",
        "hide_input_text",
        "hide_output_text",
        "hide_input_images",
        "hide_inputs,hide_outputs",
    ],
)
def test_content_flags_cover_events_and_attributes(flag):
    run(
        "--capture",
        "--hide",
        flag,
        conventions="gen_ai_latest_experimental,gen_ai_span_attributes_only",
    )


@pytest.mark.parametrize(
    "options",
    [
        [],
        ["--capture", "--hide", "hide_error_details"],
        ["--capture", "--hide", "hide_inputs"],
    ],
)
def test_error_details_hidden_by_default_or_config(options):
    run("--error", *options)


def test_environment_privacy():
    run("--capture", "--error", environment={"FI_HIDE_ERROR_DETAILS": "true"})


def test_readme_with_real_openai_model_and_loopback_transport():
    run("--readme")


def test_tool_error_body_cannot_bypass_input_privacy():
    run("--tool-error", "--capture", "--hide", "hide_inputs")


@pytest.mark.parametrize("wire", ["protobuf", "json"])
@pytest.mark.parametrize("gzip", [False, True])
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize(
    "conventions",
    [
        "",
        "gen_ai_latest_experimental",
        "gen_ai_latest_experimental,gen_ai_span_attributes_only",
    ],
)
def test_recovered_tool_errors_stay_hidden_in_history(wire, gzip, batch, conventions):
    run(
        "--tool-error",
        "--capture",
        "--hide",
        "hide_error_details",
        "--wire",
        wire,
        *(["--gzip"] if gzip else []),
        *(["--batch"] if batch else []),
        conventions=conventions,
    )


@pytest.mark.parametrize("wire", ["protobuf", "json"])
@pytest.mark.parametrize("gzip", [False, True])
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize(
    "conventions",
    [
        "",
        "gen_ai_latest_experimental",
        "gen_ai_latest_experimental,gen_ai_span_attributes_only",
    ],
)
def test_sdk_truncated_tool_errors_stay_hidden(wire, gzip, batch, conventions):
    run(
        "--tool-error",
        "--long-tool-error",
        "--capture",
        "--hide",
        "hide_error_details",
        "--wire",
        wire,
        *(["--gzip"] if gzip else []),
        *(["--batch"] if batch else []),
        conventions=conventions,
        environment={"OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT": "512"},
    )


def test_recovered_tool_error_environment_privacy():
    run(
        "--tool-error",
        "--capture",
        "--async-stream",
        environment={"FI_HIDE_ERROR_DETAILS": "true"},
    )


def test_recovered_tool_error_details_can_be_explicitly_captured():
    run("--tool-error", "--capture")


def test_tool_history_projection_is_conservative_and_does_not_mutate_native_spans():
    result = subprocess.run(
        [sys.executable, "-I", str(SCENARIO.with_name("history_scenario.py"))],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS tool history privacy" in result.stdout


def test_projection_preserves_other_instrumentation_and_exporter_lifecycle():
    result = subprocess.run(
        [sys.executable, "-I", str(SCENARIO.with_name("projection_scenario.py"))],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS boundary and lifecycle" in result.stdout
