"""Packaging contract: dependency ranges and declared Python versions."""

from __future__ import annotations

import re
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

from traceai_exa import ExaInstrumentor
from traceai_exa.package import _instruments

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _dependency(name: str) -> SpecifierSet:
    match = re.search(
        r'^{0}\s*=\s*"([^"]+)"'.format(re.escape(name)), PYPROJECT.read_text(), re.M
    )
    assert match, name
    return SpecifierSet(match.group(1))


def test_exa_py_is_a_2x_range_not_an_exact_pin():
    (requirement,) = ExaInstrumentor().instrumentation_dependencies()
    instruments = Requirement(requirement)
    assert instruments.name == "exa-py"
    # pyproject and the runtime dependency check agree.
    assert instruments.specifier == _dependency("exa-py")
    for version in ("2.25.0", "2.25.1", "2.26.0", "2.99.0"):
        assert instruments.specifier.contains(version), version
    for version in ("2.24.0", "3.0.0", "3.1.0"):
        assert not instruments.specifier.contains(version), version
    assert _instruments == ExaInstrumentor().instrumentation_dependencies()


def test_fi_instrumentation_floor_is_1_1_0():
    # 0.1.11/0.1.12 export to the old create_otel_span path and the published
    # 1.0.0 fails while building its headers; 1.1.0 is the first good release.
    spec = _dependency("fi-instrumentation-otel")
    assert spec.contains("1.1.0")
    assert spec.contains("1.2.0")
    for version in ("0.1.11", "0.1.12", "0.1.16", "1.0.0"):
        assert not spec.contains(version), version


def test_classifiers_list_only_the_tested_python_versions():
    # The suite runs on 3.10, 3.11 and 3.13 (PRD matrix 3.10/3.13, plus 3.11).
    versions = re.findall(
        r'"Programming Language :: Python :: (3\.\d+)"', PYPROJECT.read_text()
    )
    assert versions == ["3.10", "3.11", "3.13"]
