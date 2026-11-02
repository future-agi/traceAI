"""Installed distribution and documented setup must support the public examples."""

from importlib.metadata import requires, version
from pathlib import Path

import litellm
from packaging.requirements import Requirement
from packaging.version import Version


def test_required_core_conventions_are_in_the_declared_floor():
    core = next(
        Requirement(value)
        for value in requires("traceAI-litellm")
        if Requirement(value).name == "fi-instrumentation-otel"
    )
    assert Version("0.1.11") not in core.specifier
    assert Version("1.2.2") in core.specifier


def test_retry_helper_works_with_runtime_dependencies():
    dependencies = [Requirement(value) for value in requires("traceAI-litellm")]
    assert any(dep.name == "tenacity" and dep.marker is None for dep in dependencies)
    assert Version(version("tenacity")) >= Version("8.2.3")
    response = litellm.completion_with_retries(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hello"}],
        api_key="synthetic-key",
        mock_response="hello",
    )
    assert response.choices[0].message.content == "hello"


def test_readme_separates_provider_and_telemetry_credentials():
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    setup = readme.split("## Quickstart")[0]
    assert "export OPENAI_API_KEY=" in setup
    assert "export FI_API_KEY=" in setup
    assert "export FI_SECRET_KEY=" in setup
    assert "not" in setup and "inference" in setup
