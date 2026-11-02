"""The built distribution must reject interpreters unsupported by the tracing core."""

from importlib.metadata import metadata

import pytest
from packaging.specifiers import SpecifierSet


@pytest.mark.parametrize(
    ("python_version", "supported"),
    [
        ("3.9.0", False),
        ("3.9.22", False),
        ("3.10.0", True),
        ("3.10.17", True),
        ("3.11.0", True),
        ("3.12.0", True),
        ("3.13.0", True),
        ("3.14.0", False),
    ],
)
def test_python_requirement(python_version, supported):
    distribution = metadata("traceAI-chromadb")
    requirement = SpecifierSet(distribution["Requires-Python"])
    assert (python_version in requirement) is supported


def test_python_classifiers_match_requirement():
    distribution = metadata("traceAI-chromadb")
    requirement = SpecifierSet(distribution["Requires-Python"])
    classifiers = distribution.get_all("Classifier")
    for minor in range(9, 15):
        advertised = f"Programming Language :: Python :: 3.{minor}" in classifiers
        assert advertised == (f"3.{minor}.0" in requirement)
    assert "Programming Language :: Python :: 3.9" not in classifiers
