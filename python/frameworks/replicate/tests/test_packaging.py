"""Packaging contract: dependency ranges, Python versions and license."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

from traceai_replicate import ReplicateInstrumentor
from traceai_replicate.package import _instruments

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _dependency(name: str) -> SpecifierSet:
    match = re.search(
        r'^{0}\s*=\s*"([^"]+)"'.format(re.escape(name)), PYPROJECT.read_text(), re.M
    )
    assert match, name
    return SpecifierSet(match.group(1))


def test_replicate_is_a_1x_range_not_an_exact_pin():
    (requirement,) = ReplicateInstrumentor().instrumentation_dependencies()
    instruments = Requirement(requirement)
    assert instruments.name == "replicate"
    # pyproject and the runtime dependency check agree.
    assert instruments.specifier == _dependency("replicate")
    for version in ("1.0.0", "1.0.4", "1.0.7", "1.9.0"):
        assert instruments.specifier.contains(version), version
    for version in ("0.34.2", "2.0.0", "2.0.0b4"):
        assert not instruments.specifier.contains(version, prereleases=True), version
    assert tuple(_instruments) == tuple(ReplicateInstrumentor().instrumentation_dependencies())


@pytest.mark.parametrize(
    "installed, conflict", [("1.0.0", False), ("1.0.7", False), ("0.34.2", True), ("2.0.0", True)]
)
def test_the_runtime_dependency_check_uses_the_range(monkeypatch, installed, conflict):
    import opentelemetry.instrumentation.dependencies as dependencies

    monkeypatch.setattr(dependencies, "version", lambda name: installed)
    found = dependencies.get_dependency_conflicts(list(_instruments))
    assert (found is not None) is conflict


def test_fi_instrumentation_floor_is_1_1_0():
    # 0.1.11/0.1.12 export to the retired create_otel_span path and 1.0.0
    # fails while building its headers; 1.1.0 is the first good release.
    spec = _dependency("fi-instrumentation-otel")
    assert spec.contains("1.1.0")
    assert spec.contains("1.2.0")
    for version in ("0.1.11", "0.1.16", "1.0.0"):
        assert not spec.contains(version), version


def test_classifiers_list_only_the_tested_python_versions():
    text = PYPROJECT.read_text()
    versions = re.findall(r'"Programming Language :: Python :: (3\.\d+)"', text)
    assert versions == ["3.10", "3.11", "3.13"]
    assert 'python = ">=3.10,<3.14"' in text


def test_license_is_apache_2():
    text = PYPROJECT.read_text()
    assert 'license = "Apache-2.0"' in text
    assert "License :: OSI Approved :: Apache Software License" in text
