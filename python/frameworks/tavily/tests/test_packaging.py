"""Packaging contract: dependency ranges, the runtime version check, Pythons,
and the README's test commands."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

from traceai_tavily import TavilyInstrumentor
from traceai_tavily.package import _instruments

PACKAGE = Path(__file__).resolve().parents[1]
PYPROJECT = PACKAGE / "pyproject.toml"


def _dependency(name: str) -> SpecifierSet:
    match = re.search(
        r'^{0}\s*=\s*"([^"]+)"'.format(re.escape(name)), PYPROJECT.read_text(), re.M
    )
    assert match, name
    return SpecifierSet(match.group(1))


def test_tavily_python_is_a_range_from_the_tested_release_to_the_next_major():
    (requirement,) = TavilyInstrumentor().instrumentation_dependencies()
    instruments = Requirement(requirement)
    assert instruments.name == "tavily-python"
    # pyproject and the runtime dependency check agree.
    assert instruments.specifier == _dependency("tavily-python")
    for version in ("0.8.4", "0.8.5", "0.9.0", "0.99.0"):
        assert instruments.specifier.contains(version), version
    for version in ("0.7.27", "0.8.3", "1.0.0"):
        assert not instruments.specifier.contains(version), version
    assert tuple(_instruments) == tuple(TavilyInstrumentor().instrumentation_dependencies())


def test_fi_instrumentation_floor_is_1_1_0():
    # 0.1.11/0.1.12 export to the retired create_otel_span path; 1.1.0 is the
    # release this package was run against.
    spec = _dependency("fi-instrumentation-otel")
    assert spec.contains("1.1.0")
    assert spec.contains("1.2.0")
    for version in ("0.1.11", "0.1.16", "1.0.0"):
        assert not spec.contains(version), version


TESTED_PYTHONS = ["3.10", "3.11", "3.12", "3.13"]


def _classified_pythons() -> list:
    return re.findall(r'"Programming Language :: Python :: (3\.\d+)"', PYPROJECT.read_text())


def test_classifiers_list_only_the_tested_python_versions():
    assert _classified_pythons() == TESTED_PYTHONS


def test_python_constraint_admits_exactly_the_classified_versions():
    """poetry-core adds a classifier for every minor the ``python`` constraint
    admits, so the built wheel lists those whatever pyproject's classifiers say.
    The constraint and the classifiers must therefore name the same versions.
    """
    match = re.search(r'^python\s*=\s*"([^"]+)"', PYPROJECT.read_text(), re.M)
    assert match
    admitted = [
        "3.{0}".format(minor)
        for minor in range(6, 20)
        if SpecifierSet(match.group(1)).contains("3.{0}.0".format(minor))
    ]
    assert admitted == _classified_pythons()


@pytest.mark.parametrize(
    "installed, wraps",
    [("0.8.4", True), ("0.9.1", True), ("0.7.27", False), ("1.0.0", False)],
)
def test_instrument_only_wraps_an_in_range_tavily_python(monkeypatch, installed, wraps):
    """BaseInstrumentor checks _instruments against the installed release."""
    pytest.importorskip("tavily")
    import opentelemetry.instrumentation.dependencies as dependencies
    from tavily import TavilyClient

    original = TavilyClient.__dict__["search"]
    monkeypatch.setattr(dependencies, "version", lambda name: installed)
    instrumentor = TavilyInstrumentor()
    instrumentor.instrument()
    try:
        assert (TavilyClient.__dict__["search"] is not original) is wraps
    finally:
        instrumentor.uninstrument()
    assert TavilyClient.__dict__["search"] is original


README = PACKAGE / "README.md"
# The extras the README's second test command adds; measurement tests that
# import them are skipped by the base command.
LANGCHAIN_EXTRAS = ("langchain_core", "langchain_community", "langgraph")


def _readme_tests_section() -> str:
    match = re.search(r"^## Tests\n(.*?)(?=^## |\Z)", README.read_text(), re.M | re.S)
    assert match, "README.md has no '## Tests' section"
    return match.group(1)


def _tests_needing_langchain_extras() -> list:
    """Test functions in test_measurement.py that importorskip a LangChain extra."""
    tree = ast.parse((PACKAGE / "tests" / "test_measurement.py").read_text())
    gated = []
    for node in tree.body:
        if not (isinstance(node, ast.FunctionDef) and node.name.startswith("test_")):
            continue
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "importorskip"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and call.args[0].value in LANGCHAIN_EXTRAS
            ):
                gated.append(node.name)
                break
    return gated


def test_readme_gives_plain_pytest_and_both_pinned_test_commands():
    section = _readme_tests_section()
    commands = re.findall(r"```bash\n(.*?)```", section, re.S)
    assert len(commands) == 3, commands
    plain, base, extras = (" ".join(command.replace("\\\n", " ").split()) for command in commands)
    # Plain pytest in the package directory, as for the other packages.
    assert plain == "cd python/frameworks/tavily pip install -e . pytest pytest"
    assert "PYTHONPATH" not in plain
    for command in (base, extras):
        assert command.startswith('PYTHONPATH="python/frameworks/tavily:python:python/tests" ')
        assert "--with 'tavily-python==0.8.4'" in command
        assert command.endswith(
            " pytest python/frameworks/tavily/tests -q -p no:cacheprovider --noconftest"
            " -o addopts= -rs"
        )
    assert "--with wrapt " in base and "langchain" not in base
    assert "--with 'wrapt<2'" in extras
    for pin in ("langchain-community==0.4.2", "langchain-core==1.5.2", "langgraph==1.2.2"):
        assert "--with " + pin in extras, pin


def test_plain_pytest_collects_from_the_package_directory():
    """``pytest`` run in the package directory, as for the other traceAI Python
    packages, collects every module without a PYTHONPATH that names
    ``python/tests``: the modules that use the shared harness put it on
    ``sys.path`` themselves.
    """
    import fi_instrumentation

    # Only what an installed package provides: traceai_tavily and
    # fi_instrumentation. The shared harness directory is left out on purpose.
    fi_root = Path(fi_instrumentation.__file__).resolve().parents[1]
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "PYTHONPATH" and not key.startswith("PYTEST_")
    }
    env["PYTHONPATH"] = os.pathsep.join([str(PACKAGE), str(fi_root)])
    harness_dir = str(PACKAGE.parents[1] / "tests")
    assert harness_dir not in env["PYTHONPATH"].split(os.pathsep)

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=str(PACKAGE),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "ERROR collecting" not in output, output
    collected = {line.split("::", 1)[0] for line in result.stdout.splitlines() if "::" in line}
    for module in ("test_contract_harness.py", "test_example.py", "test_measurement.py"):
        assert "tests/" + module in collected, (module, sorted(collected))


def test_readme_counts_the_tests_that_need_the_langchain_extras():
    gated = _tests_needing_langchain_extras()
    assert gated, "no measurement test needs the LangChain extras"
    match = re.search(r"skips the (\d+) measurement tests", _readme_tests_section())
    assert match, "the Tests section does not say how many tests the extras run"
    assert int(match.group(1)) == len(gated), gated
