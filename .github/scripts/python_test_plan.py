#!/usr/bin/env python3
"""Decide which Python packages' tests the python-tests workflow runs for a change.

Reads the changed paths (one per line, relative to the repo root) on stdin and prints
one JSON object for $GITHUB_OUTPUT:

    {"packages": [...], "run_all": bool, "core_changed": bool}

* A package is a directory ``python/frameworks/<name>/`` that holds a ``pyproject.toml``
  (``tests_e2e`` and ``__init__.py`` are not packages).
* A package is touched by any changed path under its directory, or by a changed
  ``python/tests/test_framework_<key>.py`` that maps to it (see FRAMEWORK_TEST_PACKAGES).
* ``run_all`` is true when the PR changes the CI itself (the workflow or this script):
  then every package is listed.
* ``core_changed`` is true when ``python/fi_instrumentation/``, ``python/pyproject.toml``
  or ``python/poetry.lock`` changed: the package jobs then test against the repo's
  fi_instrumentation instead of the one their own lock resolves.

Each package entry carries ``name``, ``dir``, ``backend`` ("poetry" or "hatch"),
``own_tests`` (the package has a ``tests/`` folder with test files) and ``core_tests``
(the ``python/tests/test_framework_*.py`` files that belong to the package).

Standard library only. It fails loudly (non-zero exit, message on stderr) on a
test_framework file that maps to no package, and on a build backend it does not know.
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FRAMEWORKS_DIR = "python/frameworks"
CORE_TESTS_DIR = "python/tests"
CI_FILES = (
    ".github/workflows/python-tests.yml",
    ".github/scripts/python_test_plan.py",
)
CORE_PATHS = ("python/pyproject.toml", "python/poetry.lock")
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
    for pkg_dir in sorted(frameworks.iterdir()):
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
            "dir": f"{FRAMEWORKS_DIR}/{pkg_dir.name}",
            "backend": backend,
            "own_tests": own_tests,
            "core_tests": [],
        }
    return packages


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

    run_all = any(path in CI_FILES for path in changed)
    core_changed = any(
        path.startswith(CORE_PREFIX) or path in CORE_PATHS for path in changed
    )

    if run_all:
        touched = set(packages)
    else:
        touched = set()
        for path in changed:
            if path.startswith(f"{FRAMEWORKS_DIR}/"):
                name = path[len(FRAMEWORKS_DIR) + 1 :].split("/", 1)[0]
                if name in packages:
                    touched.add(name)
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
