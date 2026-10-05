"""Packaging contract: dependency ranges, the runtime version check, Pythons."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

from traceai_parallel import ParallelInstrumentor
from traceai_parallel.package import _instruments

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _dependency(name: str) -> SpecifierSet:
    match = re.search(
        r'^{0}\s*=\s*"([^"]+)"'.format(re.escape(name)), PYPROJECT.read_text(), re.M
    )
    assert match, name
    return SpecifierSet(match.group(1))


def test_parallel_web_is_a_1x_range_not_an_exact_pin():
    (requirement,) = ParallelInstrumentor().instrumentation_dependencies()
    instruments = Requirement(requirement)
    assert instruments.name == "parallel-web"
    # pyproject and the runtime dependency check agree.
    assert instruments.specifier == _dependency("parallel-web")
    for version in ("1.0.1", "1.3.2", "1.3.5", "1.99.0"):
        assert instruments.specifier.contains(version), version
    for version in ("0.6.0", "1.0.0", "2.0.0", "2.1.0"):
        assert not instruments.specifier.contains(version), version
    assert tuple(_instruments) == tuple(ParallelInstrumentor().instrumentation_dependencies())


def _fake_installed(monkeypatch, parallel_web_version):
    dependencies = pytest.importorskip("opentelemetry.instrumentation.dependencies")
    real = dependencies.version

    def version(name):
        if name.lower().replace("_", "-") == "parallel-web":
            return parallel_web_version
        return real(name)

    monkeypatch.setattr(dependencies, "version", version)


@pytest.mark.parametrize("installed, wrapped", [("1.0.1", True), ("1.3.5", True), ("2.0.0", False), ("0.6.0", False)])
def test_instrument_honours_the_range_at_runtime(monkeypatch, installed, wrapped):
    pytest.importorskip("parallel", reason="parallel-web must be installed")
    from parallel import _client

    from _parallel_support import new_provider

    _fake_installed(monkeypatch, installed)
    before = vars(_client.Parallel)["search"]
    _, provider = new_provider()
    instrumentor = ParallelInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        assert (vars(_client.Parallel)["search"] is not before) is wrapped
    finally:
        instrumentor.uninstrument()
    assert vars(_client.Parallel)["search"] is before


def test_fi_instrumentation_floor_is_1_1_0():
    # 0.1.11/0.1.12 export to the old create_otel_span path and the published
    # 1.0.0 fails while building its headers; 1.1.0 is the first good release.
    spec = _dependency("fi-instrumentation-otel")
    assert spec.contains("1.1.0")
    assert spec.contains("1.2.0")
    for version in ("0.1.11", "0.1.12", "0.1.16", "1.0.0"):
        assert not spec.contains(version), version


def test_classifiers_and_python_range_match_the_tested_versions():
    # The suite runs on 3.10, 3.11 and 3.13 (PRD matrix 3.10/3.13, plus 3.11).
    text = PYPROJECT.read_text()
    versions = re.findall(r'"Programming Language :: Python :: (3\.\d+)"', text)
    assert versions == ["3.10", "3.11", "3.13"]
    python = _dependency("python")
    for version in ("3.10.0", "3.11.9", "3.13.1"):
        assert python.contains(version), version
    for version in ("3.9.18", "3.14.0"):
        assert not python.contains(version), version
