"""Test the installed distribution, not just the source tree's metadata."""

from importlib.metadata import metadata

import pytest
from packaging.specifiers import SpecifierSet


@pytest.mark.parametrize("python_version", ["3.10", "3.11", "3.12", "3.13"])
def test_supported_python_versions_are_installable(python_version):
    package = metadata("traceAI-haystack")

    assert python_version in SpecifierSet(package["Requires-Python"])
    assert f"Programming Language :: Python :: {python_version}" in package.get_all(
        "Classifier"
    )
