"""Every LiteLLM install selector in the repo leaves out the 1.107.0 release."""

import ast
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet
from packaging.requirements import Requirement

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


PYTHON = Path(__file__).resolve().parents[1]
FRAMEWORK = PYTHON / "frameworks" / "litellm"


def _declared_constraints():
    project = tomllib.loads((FRAMEWORK / "pyproject.toml").read_text())
    yield "package dependency", project["tool"]["poetry"]["dependencies"]["litellm"]

    package = ast.parse((FRAMEWORK / "traceai_litellm" / "package.py").read_text())
    instruments = next(
        ast.literal_eval(node.value)
        for node in package.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_instruments" for target in node.targets)
    )
    yield "instrumentation compatibility", str(Requirement(instruments[0]).specifier)

    requirements = (PYTHON / "e2e_tests" / "requirements.txt").read_text().splitlines()
    litellm = [Requirement(line.strip()) for line in requirements if line.strip().lower().startswith("litellm")]
    assert len(litellm) == 1
    yield "E2E installation", str(litellm[0].specifier)


@pytest.mark.parametrize("name,constraint", list(_declared_constraints()))
def test_litellm_selectors_exclude_1_107_0(name, constraint):
    permitted = SpecifierSet(constraint)
    assert "1.107.0" not in permitted, name
    assert "1.107.0+cached" not in permitted, name
    assert "1.84.10" in permitted, name


@pytest.mark.parametrize("name,constraint", list(_declared_constraints()))
@pytest.mark.parametrize("version", ("1.43.0", "1.74.0.post1", "1.84.10", "1.107.1"))
def test_litellm_selectors_keep_supported_versions(name, constraint, version):
    # The published package selectors leave out one release, not every newer one.
    # The E2E requirements are narrower on purpose: they pin one known-good
    # release so every E2E install gets the same LiteLLM. Versions here are only
    # compared, never installed.
    allowed = version in SpecifierSet(constraint)
    expected = version == "1.84.10" if name == "E2E installation" else True
    assert allowed == expected, (name, version)


@pytest.mark.parametrize("name,constraint", list(_declared_constraints()))
@pytest.mark.parametrize("version", ("1.107.0+linux.1", "1.107.0+repacked"))
def test_litellm_selectors_exclude_1_107_0_with_local_labels(name, constraint, version):
    assert version not in SpecifierSet(constraint), (name, version)


def test_litellm_framework_lock_uses_an_allowed_version():
    lock = tomllib.loads((FRAMEWORK / "poetry.lock").read_text())
    versions = [entry["version"] for entry in lock["package"] if entry["name"] == "litellm"]
    assert len(versions) == 1
    assert versions[0] in SpecifierSet(">=1.43.0,!=1.107.0")
