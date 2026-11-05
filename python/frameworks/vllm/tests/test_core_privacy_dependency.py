"""Installed metadata must require the core that protects duplicate content."""

from importlib.metadata import PackageNotFoundError, requires, version

import pytest
from packaging.requirements import Requirement
from packaging.version import Version


@pytest.mark.parametrize("package", ["traceAI-vllm", "traceAI-openai"])
def test_instrumentor_requires_privacy_fixed_core(package):
    try:
        dependencies = requires(package)
    except PackageNotFoundError:
        pytest.skip(
            "Shared instrumentor is intentionally absent at the legacy SDK floor"
        )
    core = next(
        Requirement(value)
        for value in dependencies
        if Requirement(value).name == "fi-instrumentation-otel"
    )
    assert Version("1.2.2") not in core.specifier
    assert Version("1.2.3") in core.specifier
    assert Version(version("fi-instrumentation-otel")) in core.specifier
