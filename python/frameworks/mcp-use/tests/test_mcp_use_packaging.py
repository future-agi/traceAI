"""Packaging contract: dependency ranges, the tested versions, Pythons, version."""

from __future__ import annotations

import importlib.metadata
import re
import sys
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import Version

import traceai_mcp_use

PACKAGE = Path(__file__).resolve().parents[1]
PYPROJECT = PACKAGE / "pyproject.toml"
README = PACKAGE / "README.md"
CHANGELOG = PACKAGE / "CHANGELOG.md"

# The architecture's CI pin (mcp-use 1.7.1, commit 1e14ac9d). The README and
# CHANGELOG claim exactly this version; the suite runs on nothing else.
TESTED_MCP_USE = "1.7.1"
TESTED_PYTHONS = ["3.11", "3.12", "3.13"]


def _dependency(name: str) -> SpecifierSet:
    match = re.search(
        r'^{0}\s*=\s*"([^"]+)"'.format(re.escape(name)), PYPROJECT.read_text(), re.M
    )
    assert match, name
    return SpecifierSet(match.group(1))


def test_the_suite_runs_on_the_tested_mcp_use_version():
    assert importlib.metadata.version("mcp-use") == TESTED_MCP_USE


def test_mcp_use_is_a_1x_range_from_the_tested_version():
    spec = _dependency("mcp-use")
    assert spec.contains(TESTED_MCP_USE)
    for version in ("1.7.2", "1.99.0"):
        assert spec.contains(version), version
    # 1.7.0 and earlier are not tested; 2.x drops the deprecated paths.
    for version in ("1.7.0", "1.6.0", "2.0.0"):
        assert not spec.contains(version), version


def test_langchain_core_range_admits_the_installed_version():
    spec = _dependency("langchain-core")
    assert spec.contains(importlib.metadata.version("langchain-core"))
    assert not spec.contains("0.3.80")
    assert not spec.contains("2.0.0")


def test_fi_instrumentation_floor_is_1_1_0():
    # 0.1.11/0.1.12 export to the old create_otel_span path and the published
    # 1.0.0 fails while building its headers; 1.1.0 is the first good release.
    spec = _dependency("fi-instrumentation-otel")
    assert spec.contains("1.1.0")
    for version in ("0.1.11", "0.1.12", "0.1.16", "1.0.0"):
        assert not spec.contains(version), version


def test_classifiers_and_python_range_match_the_tested_versions():
    # mcp-use requires Python >=3.11 (its Requires-Python), so the floor is
    # 3.11, not the 3.9/3.10 floor of the rest of the family.
    text = PYPROJECT.read_text()
    assert re.findall(r'"Programming Language :: Python :: (3\.\d+)"', text) == TESTED_PYTHONS
    python = _dependency("python")
    for version in ("3.11.0", "3.12.10", "3.13.1"):
        assert python.contains(version), version
    for version in ("3.10.14", "3.14.0"):
        assert not python.contains(version), version
    assert "{0}.{1}".format(*sys.version_info[:2]) in TESTED_PYTHONS


def test_version_matches_pyproject_and_changelog():
    version = re.search(r'^version\s*=\s*"([^"]+)"', PYPROJECT.read_text(), re.M).group(1)
    assert traceai_mcp_use.__version__ == version == "0.1.0"
    assert re.search(r"^## (\S+)", CHANGELOG.read_text(), re.M).group(1) == version
    assert re.search(r'^name\s*=\s*"traceAI-mcp-use"', PYPROJECT.read_text(), re.M)


def test_readme_and_changelog_claim_only_the_tested_versions():
    for path in (README, CHANGELOG):
        text = " ".join(path.read_text().split())
        assert "mcp-use {0}".format(TESTED_MCP_USE) in text, path.name
        claimed = set(re.findall(r"mcp-use (\d+\.\d+\.\d+)", text))
        assert claimed == {TESTED_MCP_USE}, (path.name, claimed)
        assert "3.11, 3.12 and 3.13" in text, path.name


def test_package_source_imports_neither_langfuse_nor_traceai_mcp_nor_patches():
    source = "\n".join(path.read_text() for path in (PACKAGE / "traceai_mcp_use").glob("*.py"))
    for forbidden in (
        r"^\s*(from|import)\s+langfuse",
        r"^\s*(from|import)\s+lmnr",
        r"^\s*(from|import)\s+traceai_mcp\b",
        r"^\s*(from|import)\s+wrapt",
        r"os\.environ\[[^\]]+\]\s*=",
        r"os\.(putenv|environ\.setdefault|environ\.update)",
        r"setattr\(",
    ):
        assert not re.search(forbidden, source, re.M), forbidden
    assert Version(traceai_mcp_use.__version__) == Version("0.1.0")
