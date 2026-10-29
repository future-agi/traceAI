"""Dependency-pin regression for handoff §5.4 / Area 4.

The package pins `langchain = "^0.3.9"` (and langchain-community), capping <0.4 and
forcing customers on LangChain 1.x to `pip install --no-deps`. The fast test below
statically asserts the declared pin admits 1.x; it fails today and passes once the
pins are opened to the house `>=` style.

The required resolution test consumes an install-stage wheelhouse offline
(no `--no-deps`), without creating another runtime environment.
"""
from __future__ import annotations

import os
import pathlib

import pytest

try:
    import tomllib  # py3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

from packaging.specifiers import SpecifierSet
from packaging.version import Version

PYPROJECT = pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"
TARGET = Version("1.0.2")  # customer's langchain version


def _poetry_to_specifier(spec: str) -> SpecifierSet:
    """Translate a Poetry version spec to a PEP 440 SpecifierSet (caret only)."""
    spec = spec.strip()
    if not spec.startswith("^"):
        return SpecifierSet(spec)
    base = spec[1:]
    parts = [int(p) for p in base.split(".")]
    # Caret bumps the left-most non-zero component.
    upper = list(parts)
    for i, val in enumerate(parts):
        if val != 0 or i == len(parts) - 1:
            upper[i] = val + 1
            for j in range(i + 1, len(upper)):
                upper[j] = 0
            break
    return SpecifierSet(f">={base},<{'.'.join(map(str, upper))}")


def _dep_version(pyproject: dict, name: str):
    """Return the version spec for a dependency, or None if it is not declared."""
    dep = pyproject["tool"]["poetry"]["dependencies"].get(name)
    if dep is None:
        return None
    return dep["version"] if isinstance(dep, dict) else dep


def test_langchain_core_admits_1x():
    """GREEN guard. langchain-core is the real runtime dep and must admit 1.0.2."""
    data = tomllib.loads(PYPROJECT.read_text())
    spec = _poetry_to_specifier(_dep_version(data, "langchain-core"))
    assert TARGET in spec, f"langchain-core pin {spec} rejects {TARGET}"


def test_langchain_pin_does_not_block_1x():
    """RED — flags §5.4. If `langchain` is required at all, its pin must admit 1.0.2.
    (The fix drops it as a hard dep, since the package imports only langchain-core.)"""
    data = tomllib.loads(PYPROJECT.read_text())
    spec_str = _dep_version(data, "langchain")
    if spec_str is None:
        return  # not a required dependency → cannot block resolution
    assert TARGET in _poetry_to_specifier(spec_str), (
        f"langchain pin {spec_str} rejects {TARGET}; customers must use --no-deps"
    )


def test_langchain_community_pin_does_not_block_1x():
    """RED — flags §5.4. langchain-community 0.3.x transitively caps langchain-core
    <0.4; if required at all its pin must admit 1.0.2. (The fix drops it.)"""
    data = tomllib.loads(PYPROJECT.read_text())
    spec_str = _dep_version(data, "langchain-community")
    if spec_str is None:
        return  # not a required dependency → cannot block resolution
    assert TARGET in _poetry_to_specifier(spec_str), (
        f"langchain-community pin {spec_str} rejects {TARGET}"
    )


def test_resolves_with_langchain_1x_without_no_deps(tmp_path):
    """A real clean pip resolution, offline, with all dependencies enabled.

    Preparation is an explicit install-stage job, not a test-side network escape
    or another runtime venv. Missing/stale wheels fail rather than skip.
    """
    import hashlib
    import json
    import subprocess
    import sys

    from ..prepare_resolution_wheelhouse import source_digest

    path = os.environ.get("FI_RESOLUTION_WHEELHOUSE")
    assert path, "required install stage must set FI_RESOLUTION_WHEELHOUSE; run tests/prepare_resolution_wheelhouse.py before the sandbox"
    wheels = pathlib.Path(path)
    manifest = json.loads((wheels / "resolution-manifest.json").read_text())
    assert manifest["source_sha256"] == source_digest(), "wheelhouse is not built from this source"
    for name, digest in manifest["wheels"].items():
        assert pathlib.Path(name).name == name and name.endswith(".whl")
        assert hashlib.sha256((wheels / name).read_bytes()).hexdigest() == digest, name
    source_wheel, = wheels.glob("traceai_langchain-*.whl")
    report = tmp_path / "resolution.json"
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed",
         "--no-index", "--only-binary=:all:", "--find-links", str(wheels),
         "--report", str(report), str(source_wheel),
         "langchain==1.0.2", "langgraph==1.0.1"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"offline resolution failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    installed = {row["metadata"]["name"].lower().replace("_", "-"): row["metadata"]["version"]
                 for row in json.loads(report.read_text())["install"]}
    assert installed["langchain"] == "1.0.2"
    assert installed["langgraph"] == "1.0.1"
    assert {"traceai-langchain", "fi-instrumentation-otel", "langchain-core"} <= installed.keys()
