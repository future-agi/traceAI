#!/usr/bin/env python3
"""Decide which Python packages' tests the python-tests workflow runs for a change.

Reads the changed paths (one per line, relative to the repo root) on stdin and prints
one JSON object for $GITHUB_OUTPUT:

    {"packages": [...], "run_all": bool, "core_changed": bool}

* A package is a directory ``python/frameworks/<name>/`` that holds a ``pyproject.toml``
  (``tests_e2e`` and ``__init__.py`` are not packages), plus ``python/traceai/`` (the
  one-liner SDK, listed in EXTRA_PACKAGE_DIRS; its name is ``traceai``).
* A package is touched by any changed path under its directory, or by a changed
  ``python/tests/test_framework_<key>.py`` that maps to it (see FRAMEWORK_TEST_PACKAGES).
* ``run_all`` is true when the PR changes the CI itself (the workflow, this script or the
  known-broken list): then every package is listed.
* ``core_changed`` is true when ``python/fi_instrumentation/``, ``python/pyproject.toml``
  or ``python/poetry.lock`` changed: the package jobs then install the repo's
  fi_instrumentation over the one their own lock or range resolves. Otherwise the core test
  files run with ``python -P`` (the repo's source is not on sys.path) and so import the
  fi-instrumentation-otel installed in the package's environment.
* A change to ``python/tests/conftest.py`` or ``python/tests/pytest.ini`` (shared by every
  core test file) touches every package that has core tests. So does ``core_changed``: the
  core test files then run against the changed core.

Each package entry carries ``name``, ``dir``, ``backend`` ("poetry" or "hatch"),
``own_tests`` (the package has a ``tests/`` folder with test files), ``core_tests``
(the ``python/tests/test_framework_*.py`` files that belong to the package),
``known_broken`` and ``known_broken_reason`` (from ``.github/python-tests-known-broken.txt``:
one ``<package dir name> | <reason>`` line per package; ``#`` comments and blank lines are
ignored; the reason is "" for a package that is not listed). The workflow lets a listed
package fail without failing the check, and fails when a listed package passes.

Paths are read as written (the workflow's git diff runs with ``core.quotepath=off``).
Standard library only. It fails loudly (non-zero exit, message on stderr) on a
test_framework file that maps to no package, on a build backend it does not know, and on a
missing or malformed known-broken list.
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FRAMEWORKS_DIR = "python/frameworks"
CORE_TESTS_DIR = "python/tests"
# Package directories outside python/frameworks/ that are tested like the framework packages.
EXTRA_PACKAGE_DIRS = ("python/traceai",)
KNOWN_BROKEN_FILE = ".github/python-tests-known-broken.txt"
CI_FILES = (
    ".github/workflows/python-tests.yml",
    ".github/scripts/python_test_plan.py",
    KNOWN_BROKEN_FILE,
)
CORE_PATHS = ("python/pyproject.toml", "python/poetry.lock")
# Shared by every python/tests/test_framework_*.py file.
CORE_TEST_SHARED = (f"{CORE_TESTS_DIR}/conftest.py", f"{CORE_TESTS_DIR}/pytest.ini")
CORE_PREFIX = "python/fi_instrumentation/"

BACKENDS = {
    "poetry.core.masonry.api": "poetry",
    "hatchling.build": "hatch",
}

# python/tests/test_framework_<key>.py -> python/frameworks/<package dir>.
# Explicit on purpose: a new test_framework file must be added here, or the plan fails.
FRAMEWORK_TEST_PACKAGES = {
    "anthropic": "anthropic",
    "autogen": "autogen",
    "bedrock": "bedrock",
    "crewai": "crewai",
    "dspy": "dspy",
    "google_adk": "google-adk",
    "groq": "groq",
    "guardrails": "guardrails",
    "haystack": "haystack",
    "instructor": "instructor",
    "langchain": "langchain",
    "litellm": "litellm",
    "llamaindex": "llama_index",
    "mcp": "mcp",
    "mistralai": "mistralai",
    "openai": "openai",
    "openai_agents": "openai-agents",
    "portkey": "portkey",
    "smolagents": "smolagents",
    "vertexai": "vertexai",
}

_TEST_FILE = re.compile(rf"^{re.escape(CORE_TESTS_DIR)}/test_framework_(?P<key>[A-Za-z0-9_]+)\.py$")
_BACKEND = re.compile(r'^\s*build-backend\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


class PlanError(Exception):
    pass


def discover_packages(root=REPO_ROOT):
    """Return {dir name: package dict without core_tests} for every package directory."""
    packages = {}
    frameworks = root / FRAMEWORKS_DIR
    pkg_dirs = sorted(frameworks.iterdir()) + [root / extra for extra in EXTRA_PACKAGE_DIRS]
    for pkg_dir in pkg_dirs:
        pyproject = pkg_dir / "pyproject.toml"
        if not pkg_dir.is_dir() or not pyproject.is_file():
            continue
        match = _BACKEND.search(pyproject.read_text(encoding="utf-8"))
        if not match:
            raise PlanError(f"{pyproject.relative_to(root)}: no [build-system] build-backend")
        backend = BACKENDS.get(match.group(1))
        if backend is None:
            raise PlanError(
                f"{pyproject.relative_to(root)}: unknown build-backend {match.group(1)!r}; "
                f"known: {sorted(BACKENDS)}"
            )
        tests_dir = pkg_dir / "tests"
        own_tests = tests_dir.is_dir() and (
            any(tests_dir.rglob("test_*.py")) or any(tests_dir.rglob("*_test.py"))
        )
        packages[pkg_dir.name] = {
            "name": pkg_dir.name,
            "dir": pkg_dir.relative_to(root).as_posix(),
            "backend": backend,
            "own_tests": own_tests,
            "core_tests": [],
            "known_broken": False,
            "known_broken_reason": "",
        }
    return packages


def load_known_broken(packages, root=REPO_ROOT):
    """Return {package dir name: reason} from the known-broken list; fail on a bad line."""
    path = root / KNOWN_BROKEN_FILE
    listed = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanError(f"{KNOWN_BROKEN_FILE}: cannot read the known-broken list ({exc.strerror or exc}); the file must exist") from exc
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        where = f"{KNOWN_BROKEN_FILE}:{number}: {line!r}"
        if " | " not in line:
            raise PlanError(f"{where}: expected '<package dir name> | <reason>'")
        name, reason = (part.strip() for part in line.split(" | ", 1))
        if not name or not reason:
            raise PlanError(f"{where}: the package name and the reason must both be non-empty")
        if name not in packages:
            raise PlanError(f"{where}: {name!r} is not a package")
        if name in listed:
            raise PlanError(f"{where}: {name!r} is listed twice")
        listed[name] = reason
    return listed


def core_tests_by_package(root=REPO_ROOT, packages=None):
    """Map every python/tests/test_framework_*.py on disk to its package; fail on a gap."""
    packages = packages if packages is not None else discover_packages(root)
    mapped = {}
    for path in sorted((root / CORE_TESTS_DIR).glob("test_framework_*.py")):
        rel = f"{CORE_TESTS_DIR}/{path.name}"
        mapped[rel] = package_for_test_file(rel, packages)
    return mapped


def package_for_test_file(rel_path, packages):
    match = _TEST_FILE.match(rel_path)
    if not match:
        raise PlanError(f"{rel_path}: not a python/tests/test_framework_<key>.py path")
    key = match.group("key")
    name = FRAMEWORK_TEST_PACKAGES.get(key)
    if name is None:
        raise PlanError(
            f"{rel_path}: key {key!r} is not in FRAMEWORK_TEST_PACKAGES in "
            f"{Path(__file__).name}; add the package it tests"
        )
    if name not in packages:
        raise PlanError(
            f"{rel_path}: key {key!r} maps to {FRAMEWORKS_DIR}/{name}, "
            "which is not a package directory with a pyproject.toml"
        )
    return name


def plan(changed, root=REPO_ROOT):
    changed = [line.strip() for line in changed if line.strip()]
    packages = discover_packages(root)

    # Every test_framework file on disk belongs to a package (and fails loudly if not),
    # whether or not this change touches it.
    for rel, name in core_tests_by_package(root, packages).items():
        packages[name]["core_tests"].append(rel)

    for name, reason in load_known_broken(packages, root).items():
        packages[name]["known_broken"] = True
        packages[name]["known_broken_reason"] = reason

    run_all = any(path in CI_FILES for path in changed)
    core_changed = any(
        path.startswith(CORE_PREFIX) or path in CORE_PATHS for path in changed
    )

    if run_all:
        touched = set(packages)
    else:
        touched = set()
        if core_changed or any(path in CORE_TEST_SHARED for path in changed):
            touched.update(pkg["name"] for pkg in packages.values() if pkg["core_tests"])
        for path in changed:
            owner = next(
                (pkg["name"] for pkg in packages.values() if path.startswith(f"{pkg['dir']}/")),
                None,
            )
            if owner is not None:
                touched.add(owner)
            elif _TEST_FILE.match(path):
                touched.add(package_for_test_file(path, packages))

    return {
        "packages": [packages[name] for name in sorted(touched)],
        "run_all": run_all,
        "core_changed": core_changed,
    }


def main():
    try:
        result = plan(sys.stdin.read().splitlines())
    except PlanError as exc:
        print(f"python_test_plan: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
