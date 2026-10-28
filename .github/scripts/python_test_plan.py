#!/usr/bin/env python3
"""Decide which Python packages' tests the python-tests workflow runs for a change.

Reads the changed paths and prints one JSON object for $GITHUB_OUTPUT:

    {"packages": [...], "run_all": bool, "core_changed": bool}

The paths come from one of three places (see main):

* ``--diff <range>``: ``git diff --name-only --no-renames -z <range>``. Renames are off, so a
  file that moved lists both its old and its new path (the package it left is planned too),
  and ``-z`` keeps a tab, a quote or a backslash in a name exactly as written.
* ``--all``: ``git ls-files -z``, every tracked file (no diff to read).
* neither: newline-separated paths on stdin, relative to the repo root (for local use).

* A package is a directory ``python/frameworks/<name>/`` that holds a ``pyproject.toml``
  (``tests_e2e`` and ``__init__.py`` are not packages), plus ``python/traceai/`` (the
  one-liner SDK, listed in EXTRA_PACKAGE_DIRS; its name is ``traceai``).
* A package is touched by any changed path under its directory, or by a changed
  ``python/tests/test_framework_<key>.py`` that maps to it (see FRAMEWORK_TEST_PACKAGES).
* ``run_all`` is true when the PR changes the CI itself (the workflow, the known-broken list,
  or any path under ``.github/scripts/``: this script, its tests, the verdict script, a new
  helper; see CI_FILES and CI_PREFIXES): then every package is listed.
* ``core_changed`` is true when ``python/fi_instrumentation/``, ``python/pyproject.toml``
  or ``python/poetry.lock`` changed. Every package is then listed (like ``run_all``): the
  package jobs install the repo's fi_instrumentation over the one their own lock or range
  resolves, and test against it. Without a core change the core test files run with
  ``python -P`` (the repo's source is not on sys.path) and so import the
  fi-instrumentation-otel installed in the package's environment.
* A change to ``python/tests/conftest.py`` or ``python/tests/pytest.ini`` (shared by every
  core test file) touches only the packages that have core tests.

Each package entry carries ``name``, ``dir``, ``backend`` ("poetry" or "hatch"),
``own_tests`` (the package has a ``tests/`` folder with test files), ``core_tests``
(the ``python/tests/test_framework_*.py`` files that belong to the package),
``known_broken`` and ``known_broken_reason`` (from ``.github/python-tests-known-broken.txt``:
one ``<package dir name> | <reason>`` line per package; ``#`` comments and blank lines are
ignored; the reason is "" for a package that is not listed). These fields are provenance
only: package-wide failure waivers are forbidden. Every entry also joins the canonical
compatibility registry's integration_id, qualification and exception_ids. The verdict
consumes exact JUnit outcomes, signatures and the actual upstream/runtime environment.

Paths from git are used exactly as written (stdin lines are stripped). Standard library only,
plus git for --diff and --all. It fails loudly (non-zero exit, message on stderr) on a
test_framework file that maps to no package, on a build backend it does not know, on a
missing or malformed known-broken list, and on a plan that cannot be right (a skipped package
job counts as green, so a plan of 0 packages from a broken input would pass unseen):

* ``--diff RANGE`` with no commits (base == head: the workflow passed the wrong SHAs; commits
  that net to no change plan nothing, which is fine), or ``--all`` with 0 paths;
* ``run_all`` or ``core_changed`` with 0 packages (package discovery broke);
* a changed path under a ``python/frameworks/<dir>/`` that exists but has no ``pyproject.toml``
  (a package that lost its pyproject.toml would otherwise drop out of the plan).
"""

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FRAMEWORKS_DIR = "python/frameworks"
CORE_TESTS_DIR = "python/tests"
# Package directories outside python/frameworks/ that are tested like the framework packages.
EXTRA_PACKAGE_DIRS = ("python/traceai",)
KNOWN_BROKEN_FILE = ".github/python-tests-known-broken.txt"
# A change to any of these tests every package: the workflow, the known-broken list, and
# everything under .github/scripts/ (the planner, its tests, the verdict script, a new helper).
CI_FILES = (
    ".github/workflows/python-tests.yml",
    KNOWN_BROKEN_FILE,
)
CI_PREFIXES = (".github/scripts/", ".github/compat/")
# Directories under python/frameworks/ that are not packages and need no pyproject.toml.
NON_PACKAGE_DIRS = ("tests_e2e",)
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


def is_ci_path(path):
    return path in CI_FILES or path.startswith(CI_PREFIXES)


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


def load_registry(root):
    spec = importlib.util.spec_from_file_location('compat_validation', REPO_ROOT / '.github/compat/validate.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module.load_registry(root)
    except (ValueError, OSError) as exc:
        raise PlanError(f'compatibility registry: {exc}') from exc


def plan(changed, root=REPO_ROOT):
    changed = [path for path in changed if path]
    packages = discover_packages(root)
    registry = load_registry(root)
    by_dir = {target['package_dir']: target for target in registry['integrations']}
    for package in packages.values():
        target = by_dir.get(package['dir'])
        if target is None:
            raise PlanError(f'{package["dir"]}: missing compatibility registry entry')
        contract = target['contract_test'] or {}
        paths = contract.get('paths', [])
        # Real-SDK pilot contracts can live inside their package. Their location
        # does not relax the registered loopback-only boundary.
        isolated = [p for p in paths if contract.get('network') == 'loopback-only' and
                    (target['canary']['pilot'] or p.startswith('.github/compat/contracts/'))]
        own_files = sorted({p.relative_to(root).as_posix()
                            for p in (root / package['dir'] / 'tests').rglob('*.py')
                            if p.name.startswith('test_') or p.name.endswith('_test.py')})
        own_paths = [p for p in own_files if not any(p == c or p.startswith(c + '/') for c in isolated)]
        package.update(integration_id=target['id'], qualification=target['qualification']['status'],
                       exception_ids=[ex['id'] for ex in target['exceptions']],
                       contract_tests=isolated, own_test_paths=own_paths, own_tests=bool(own_paths))

    # Every test_framework file on disk belongs to a package (and fails loudly if not),
    # whether or not this change touches it.
    for rel, name in core_tests_by_package(root, packages).items():
        packages[name]["core_tests"].append(rel)

    for name, reason in load_known_broken(packages, root).items():
        packages[name]["known_broken"] = True
        packages[name]["known_broken_reason"] = reason

    # A package that lost its pyproject.toml is no longer discovered, so its changes would plan
    # nothing and the job would stay green. The directory still exists: fail. A directory that
    # is gone (the package was removed entirely) is fine.
    for path in changed:
        parts = path.split("/")
        if len(parts) > 3 and path.startswith(f"{FRAMEWORKS_DIR}/"):
            directory = parts[2]
            if (
                directory not in NON_PACKAGE_DIRS
                and directory not in packages
                and (root / FRAMEWORKS_DIR / directory).is_dir()
            ):
                raise PlanError(
                    f"{FRAMEWORKS_DIR}/{directory}/ changed but has no pyproject.toml; "
                    "a package must keep its pyproject.toml (or be removed entirely)"
                )

    run_all = any(is_ci_path(path) for path in changed)
    core_changed = any(
        path.startswith(CORE_PREFIX) or path in CORE_PATHS for path in changed
    )

    if run_all or core_changed:
        # A core change is installed into every package's environment (the core_install step).
        touched = set(packages)
    else:
        touched = set()
        if any(path in CORE_TEST_SHARED for path in changed):
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

    # There is no blanket "plans nothing" failure: a change that touches no package is normal
    # (README, docs/, java/, typescript/, csharp/, python/e2e_tests/, python/frameworks/tests_e2e/,
    # a core test file that is not test_framework_*; fi_instrumentation's job covers the last).
    # Only these are impossible: the whole tree (run_all) or the core (core_changed) is a change
    # to every package, so 0 packages there means discovery broke.
    if (run_all or core_changed) and not touched:
        raise PlanError(
            f"{'run_all' if run_all else 'core_changed'} is set but 0 packages were found; "
            f"check that {FRAMEWORKS_DIR}/<name>/pyproject.toml files exist and are read"
        )

    return {
        "packages": [packages[name] for name in sorted(touched)],
        "run_all": run_all,
        "core_changed": core_changed,
    }


def changed_paths(rev_range, root=REPO_ROOT):
    """Return the changed paths of a git range, or every tracked file when rev_range is None.

    Renames are off (--no-renames): a moved file lists its old and its new path, so the
    package it left is planned too. -z keeps tabs, quotes and backslashes in a name as written.
    """
    if rev_range is None:
        cmd = ["git", "ls-files", "-z"]
    else:
        cmd = ["git", "-c", "core.quotepath=off", "diff", "--name-only", "--no-renames", "-z", rev_range]
    try:
        done = subprocess.run(cmd, cwd=root, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", "replace").strip()
        raise PlanError(f"{' '.join(cmd)} failed (exit {exc.returncode}): {stderr}") from exc
    except OSError as exc:
        raise PlanError(f"{' '.join(cmd)}: cannot run git ({exc.strerror or exc})") from exc
    return [item.decode("utf-8", "surrogateescape") for item in done.stdout.split(b"\0") if item]


def commit_count(rev_range, root=REPO_ROOT):
    """Return the number of commits the head side of an A..B or A...B range adds over its base."""
    base, sep, head = rev_range.partition("...")
    if not sep:
        base, sep, head = rev_range.partition("..")
    if not sep:
        raise PlanError(f"{rev_range}: expected a range A..B or A...B")
    cmd = ["git", "rev-list", "--count", f"{base}..{head}"]
    try:
        done = subprocess.run(cmd, cwd=root, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise PlanError(f"{' '.join(cmd)} failed (exit {exc.returncode}): {exc.stderr.strip()}") from exc
    return int(done.stdout.strip())


def main(argv=None, root=REPO_ROOT):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--diff", metavar="RANGE", help="plan from the paths `git diff RANGE` changes")
    source.add_argument("--all", action="store_true", help="plan from every tracked file")
    args = parser.parse_args(argv)
    try:
        if args.diff is not None:
            changed = changed_paths(args.diff, root)
            if not changed:
                # Commits that net to no change (an empty commit, a change and its revert) are a
                # real, if rare, PR or push: nothing to plan. No commits at all means the workflow
                # passed the wrong SHAs (base == head), which would plan nothing for every PR.
                commits = commit_count(args.diff, root)
                if commits == 0:
                    raise PlanError(
                        f"no commits in {args.diff}; a PR or push always has some, "
                        "so check the base and head SHAs the workflow passes"
                    )
                print(f"{args.diff}: {commits} commit(s) but no changed files; nothing to plan", file=sys.stderr)
        elif args.all:
            changed = changed_paths(None, root)
            if not changed:
                raise PlanError("--all: git lists 0 tracked files; check that the checkout is a git repository with files")
        else:
            changed = [line.strip() for line in sys.stdin.read().splitlines()]
        print(f"Changed files: {len([path for path in changed if path])}", file=sys.stderr)
        result = plan(changed, root)
    except PlanError as exc:
        print(f"python_test_plan: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
