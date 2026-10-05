"""Packaging contract: dependency ranges, the runtime version check, Pythons, README."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

from traceai_discoveryengine import DiscoveryEngineInstrumentor, _wrappers
from traceai_discoveryengine.package import _instruments
from traceai_discoveryengine.version import __version__

PACKAGE = Path(__file__).resolve().parents[1]
PYPROJECT = PACKAGE / "pyproject.toml"
README = PACKAGE / "README.md"
CHANGELOG = PACKAGE / "CHANGELOG.md"


def _dependency(name: str) -> SpecifierSet:
    match = re.search(
        r'^{0}\s*=\s*"([^"]+)"'.format(re.escape(name)), PYPROJECT.read_text(), re.M
    )
    assert match, name
    return SpecifierSet(match.group(1))


def test_discoveryengine_is_a_range_from_the_measured_pin_not_an_exact_pin():
    (requirement,) = DiscoveryEngineInstrumentor().instrumentation_dependencies()
    instruments = Requirement(requirement)
    assert instruments.name == "google-cloud-discoveryengine"
    # pyproject and the runtime dependency check agree.
    assert instruments.specifier == _dependency("google-cloud-discoveryengine")
    for version in ("0.20.5", "0.20.9", "0.21.0", "0.99.0"):
        assert instruments.specifier.contains(version), version
    for version in ("0.19.0", "0.20.4", "1.0.0"):
        assert not instruments.specifier.contains(version), version
    assert tuple(_instruments) == tuple(DiscoveryEngineInstrumentor().instrumentation_dependencies())


def _fake_installed(monkeypatch, installed_version):
    dependencies = pytest.importorskip("opentelemetry.instrumentation.dependencies")
    real = dependencies.version

    def version(name):
        if name.lower().replace("_", "-") == "google-cloud-discoveryengine":
            return installed_version
        return real(name)

    monkeypatch.setattr(dependencies, "version", version)


@pytest.mark.parametrize(
    "installed, wrapped",
    [("0.20.5", True), ("0.21.3", True), ("0.20.4", False), ("1.0.0", False)],
)
def test_instrument_honours_the_range_at_runtime(monkeypatch, installed, wrapped):
    from google.cloud.discoveryengine_v1 import SearchServiceClient

    from _discoveryengine_support import new_provider

    _fake_installed(monkeypatch, installed)
    before = vars(SearchServiceClient)["search"]
    _, provider = new_provider()
    instrumentor = DiscoveryEngineInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        assert (vars(SearchServiceClient)["search"] is not before) is wrapped
    finally:
        instrumentor.uninstrument()
    assert vars(SearchServiceClient)["search"] is before


def test_fi_instrumentation_floor_is_1_1_0():
    # 0.1.11/0.1.12 export to the old create_otel_span path and the published
    # 1.0.0 fails while building its headers; 1.1.0 is the first good release.
    spec = _dependency("fi-instrumentation-otel")
    assert spec.contains("1.1.0")
    assert spec.contains("1.2.0")
    for version in ("0.1.11", "0.1.12", "0.1.16", "1.0.0"):
        assert not spec.contains(version), version


def test_classifiers_and_python_range_match_the_tested_versions():
    # google-cloud-discoveryengine 0.20.5 requires Python >=3.10 and
    # fi-instrumentation-otel 1.1.0 stops before 3.14. The suite runs on
    # every minor in between.
    text = PYPROJECT.read_text()
    versions = re.findall(r'"Programming Language :: Python :: (3\.\d+)"', text)
    assert versions == ["3.10", "3.11", "3.12", "3.13"]
    python = _dependency("python")
    for version in ("3.10.0", "3.11.9", "3.12.10", "3.13.1"):
        assert python.contains(version), version
    for version in ("3.9.18", "3.14.0"):
        assert not python.contains(version), version
    readme = README.read_text()
    assert "Python 3.10, 3.11, 3.12 and 3.13" in readme
    assert "`google-cloud-discoveryengine` 0.20.5" in readme


def test_versions_agree():
    assert re.search(r'^version = "{0}"$'.format(re.escape(__version__)), PYPROJECT.read_text(), re.M)
    assert CHANGELOG.read_text().splitlines()[0] == "# Changelog"
    assert "## {0}".format(__version__) in CHANGELOG.read_text()


def _attribute_constants():
    return {
        value
        for name, value in vars(_wrappers).items()
        if name.isupper() and isinstance(value, str) and value.startswith("discoveryengine.")
    }


def test_the_readme_documents_every_attribute_the_code_can_write():
    documented = set(re.findall(r"`(discoveryengine\.[a-z_.]+)`", README.read_text()))
    assert _attribute_constants() <= documented
    # And nothing in the README is an attribute the code never writes.
    assert documented - _attribute_constants() == set()


def test_the_readme_states_the_caps_the_code_applies():
    readme = README.read_text()
    assert _wrappers.MAX_VALUE_BYTES == 1024
    assert _wrappers.MAX_STACKTRACE_BYTES == 16 * 1024
    assert "1 KB" in readme and "16 KB" in readme
