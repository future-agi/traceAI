"""Packaging contract: the supported firecrawl-py range and the declared metadata.

BaseInstrumentor.instrument() logs an error and instruments nothing when the
installed firecrawl-py falls outside instrumentation_dependencies(), so an
exact pin would silently turn tracing off for every other release.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

import pytest

pytest.importorskip("firecrawl", reason="firecrawl-py must be installed to test its instrumentor")

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from packaging.requirements import Requirement  # noqa: E402
from traceai_firecrawl import FirecrawlInstrumentor  # noqa: E402
from traceai_firecrawl.package import _instruments  # noqa: E402

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _requirement() -> Requirement:
    (requirement,) = [Requirement(spec) for spec in _instruments]
    assert requirement.name == "firecrawl-py"
    return requirement


@pytest.mark.parametrize(
    "candidate,supported",
    [("4.46.2", True), ("4.47.0", True), ("4.99.9", True), ("4.46.1", False), ("5.0.0", False)],
)
def test_supported_firecrawl_range(candidate: str, supported: bool) -> None:
    assert _requirement().specifier.contains(candidate) is supported


@pytest.fixture()
def installed_firecrawl(monkeypatch: pytest.MonkeyPatch) -> Iterator[list]:
    """Pretend a different firecrawl-py release is installed."""
    import opentelemetry.instrumentation.dependencies as dependencies

    reported: list = []
    real_version = dependencies.version

    def fake_version(name: str) -> str:
        if name == "firecrawl-py" and reported:
            return reported[0]
        return real_version(name)

    monkeypatch.setattr(dependencies, "version", fake_version)
    yield reported


@pytest.mark.parametrize("candidate,instrumented", [("4.47.0", True), ("5.0.0", False)])
def test_instrument_follows_the_supported_range(
    installed_firecrawl: list, candidate: str, instrumented: bool
) -> None:
    from firecrawl.v2.client import FirecrawlClient

    original = FirecrawlClient.__dict__["scrape"]
    installed_firecrawl.append(candidate)
    instrumentor = FirecrawlInstrumentor()
    instrumentor.instrument(tracer_provider=TracerProvider())
    try:
        wrapped = FirecrawlClient.__dict__["scrape"] is not original
    finally:
        instrumentor.uninstrument()
    assert wrapped is instrumented
    assert FirecrawlClient.__dict__["scrape"] is original


def _poetry_value(name: str) -> str:
    match = re.search(r'^{0}\s*=\s*"([^"]+)"'.format(re.escape(name)), PYPROJECT.read_text(), re.M)
    assert match, name
    return match.group(1)


def test_pyproject_matches_the_supported_range() -> None:
    declared = Requirement("firecrawl-py" + _poetry_value("firecrawl-py"))
    assert declared.specifier == _requirement().specifier
    assert _poetry_value("fi-instrumentation-otel") == ">=1.1.0"


def test_classifiers_list_only_tested_pythons() -> None:
    classifiers = re.findall(r'"Programming Language :: Python :: (3\.\d+)"', PYPROJECT.read_text())
    assert classifiers == ["3.10", "3.11", "3.13"]
    assert _poetry_value("python") == ">=3.10,<3.14"
