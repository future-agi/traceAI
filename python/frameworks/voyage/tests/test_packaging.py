"""Packaging contract: ranges, declared Pythons, license, and no base_url from us."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

pytest.importorskip("voyageai", reason="voyageai must be installed to test its instrumentor")

import voyageai  # noqa: E402
from traceai_voyage import VoyageInstrumentor  # noqa: E402
from traceai_voyage.package import _instruments  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1]
PYPROJECT = PACKAGE / "pyproject.toml"
SOURCE = PACKAGE / "traceai_voyage"


def _dependency(name: str) -> SpecifierSet:
    match = re.search(r'^{0}\s*=\s*"([^"]+)"'.format(re.escape(name)), PYPROJECT.read_text(), re.M)
    assert match, name
    return SpecifierSet(match.group(1))


def test_voyageai_is_a_range_from_the_atlas_floor_to_the_next_major():
    (requirement,) = VoyageInstrumentor().instrumentation_dependencies()
    instruments = Requirement(requirement)
    assert instruments.name == "voyageai"
    # pyproject and the runtime dependency check agree.
    assert instruments.specifier == _dependency("voyageai")
    for version in ("0.3.7", "0.4.0", "0.4.1", "0.5.0", "0.9.0"):
        assert instruments.specifier.contains(version), version
    for version in ("0.3.6", "0.3.5", "1.0.0", "1.2.0"):
        assert not instruments.specifier.contains(version), version
    assert tuple(_instruments) == tuple(VoyageInstrumentor().instrumentation_dependencies())


@pytest.mark.parametrize(
    ("installed", "wrapped"), [("0.4.1", True), ("0.3.6", False), ("1.0.0", False)]
)
def test_the_runtime_dependency_check_accepts_only_the_range(monkeypatch, installed, wrapped):
    import opentelemetry.instrumentation.dependencies as dependencies

    real_version = dependencies.version

    def version(name):
        return installed if name == "voyageai" else real_version(name)

    monkeypatch.setattr(dependencies, "version", version)
    instrumentor = VoyageInstrumentor()
    instrumentor.instrument()
    try:
        assert hasattr(voyageai.Client.__dict__["embed"], "__wrapped__") is wrapped
    finally:
        instrumentor.uninstrument()
    assert not hasattr(voyageai.Client.__dict__["embed"], "__wrapped__")


def test_fi_instrumentation_floor_is_1_1_0():
    # 0.1.x export to the retired create_otel_span path and the published
    # 1.0.0 fails while building its headers; 1.1.0 is the first good release.
    spec = _dependency("fi-instrumentation-otel")
    assert spec.contains("1.1.0")
    assert spec.contains("1.2.0")
    for version in ("0.1.11", "0.1.16", "1.0.0"):
        assert not spec.contains(version), version


def test_classifiers_list_only_the_tested_python_versions():
    text = PYPROJECT.read_text()
    versions = re.findall(r'"Programming Language :: Python :: (3\.\d+)"', text)
    # Every version the suite runs on (3.10 to 3.13), and nothing else.
    assert versions == ["3.10", "3.11", "3.12", "3.13"]
    assert _dependency("python") == SpecifierSet(">=3.10,<3.14")


def test_distribution_name_and_license():
    text = PYPROJECT.read_text()
    assert re.search(r'^name = "traceAI-voyage"$', text, re.M)
    assert re.search(r'^license = "Apache-2.0"$', text, re.M)
    assert '"License :: OSI Approved :: Apache Software License"' in text


def test_the_package_never_sets_base_url():
    # D14: the client picks the host from the key; the instrumentor never does.
    for path in SOURCE.glob("*.py"):
        source = path.read_text()
        assert not re.search(r"base_url\s*=", source), path
        assert not re.search(r"""["']base_url["']\s*\]\s*=""", source), path
        for host in ("api.voyageai.com", "ai.mongodb.com"):
            assert host not in source, (path, host)


def test_instrumenting_leaves_the_clients_host_choice_alone():
    keys = ("pa-placeholder-legacy-key", "al-placeholder-atlas-key")
    before = {key: voyageai.Client(api_key=key)._params["base_url"] for key in keys}
    instrumentor = VoyageInstrumentor()
    instrumentor.instrument()
    try:
        after = {key: voyageai.Client(api_key=key)._params["base_url"] for key in keys}
        explicit = voyageai.Client(api_key=keys[0], base_url="https://example.invalid/v1")
    finally:
        instrumentor.uninstrument()
    assert after == before
    assert explicit._params["base_url"] == "https://example.invalid/v1"
