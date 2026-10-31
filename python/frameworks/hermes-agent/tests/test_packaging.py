"""The Python range the package and the directory-plugin wrapper declare.

Hermes' package manager refuses a directory plugin whose requires-python excludes
the Python Hermes runs on, and current Hermes runs on 3.14.
"""

import pathlib
import tomllib

import pytest
from packaging.specifiers import SpecifierSet

ROOT = pathlib.Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "hermes-plugin" / "futureagi"


def _package_python() -> SpecifierSet:
    document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return SpecifierSet(document["tool"]["poetry"]["dependencies"]["python"])


def _wrapper_python() -> SpecifierSet:
    document = tomllib.loads((WRAPPER / "pyproject.toml").read_text(encoding="utf-8"))
    return SpecifierSet(document["project"]["requires-python"])


def test_package_and_wrapper_declare_the_same_python_range():
    assert _package_python() == _wrapper_python()


@pytest.mark.parametrize("version", ["3.11.0", "3.12.10", "3.13.7", "3.14.4"])
def test_python_versions_hermes_runs_on_are_admitted(version):
    assert version in _package_python()


@pytest.mark.parametrize("version", ["3.10.17", "3.15.0"])
def test_untested_python_versions_are_refused(version):
    assert version not in _package_python()


def test_hermes_package_manager_accepts_the_wrapper_on_python_3_14():
    from pm import plugin_declarations as declarations
    declaration = declarations.read_python_declaration(WRAPPER)
    # Upstream bf0203a names the membership predicate installs_packages.
    # It still must recognize a genuinely installable wrapper, not an empty plugin.
    assert declaration.installs_packages
    assert declaration.python_error("3.14.4") is None
