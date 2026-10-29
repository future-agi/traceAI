#!/usr/bin/env python3
"""Which packages of this repository are ready to publish.

A package is "ready" when the version in its ``pyproject.toml`` / ``package.json`` is not on
its registry yet (PyPI or npm). Releasing is therefore still a version bump plus a changelog
entry in a normal pull request; the workflow in ``.github/workflows/release.yml`` does the
building, checking, publishing, tagging and the GitHub release.

    python3 .github/release/release_plan.py                 plan JSON on stdout (asks PyPI and npm)
    python3 .github/release/release_plan.py --offline       every package, no registry calls (dry runs, tests)
    python3 .github/release/release_plan.py --check         exit 1 when a package's __version__ disagrees
                                                            with its pyproject.toml, or a name is duplicated
    python3 .github/release/release_plan.py --only NAME..   limit the plan to these package names

Packages: ``python/pyproject.toml`` (the core, fi-instrumentation-otel), ``python/traceai/``,
every ``python/frameworks/<dir>/pyproject.toml``, and every non-private
``typescript/packages/<dir>/package.json``. ``config.json`` next to this file lists packages
that are never published, with the reason. Java (JitPack from tags) and C# are not covered.

Plan shape::

    {"python": [{"name", "version", "dir", "import_name", "tag", "core", "changelog"}, ...],
     "typescript": [{"name", "version", "dir", "tag", "core", "changelog"}, ...]}

``core`` marks the packages the others depend on (fi-instrumentation-otel, @traceai/fi-core,
@traceai/fi-semantic-conventions); the workflow publishes those first.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = Path(__file__).resolve().parent / "config.json"
PYTHON_CORE = {"fi-instrumentation-otel"}
TS_CORE = {"@traceai/fi-core", "@traceai/fi-semantic-conventions"}
VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)


def _toml_str(text: str, key: str) -> str | None:
    m = re.search(rf'^{key}\s*=\s*"([^"]*)"', text, re.M)
    return m.group(1) if m else None


def pypi_normalize(name: str) -> str:
    """PEP 503 name normalisation: traceAI-DSPy -> traceai-dspy."""
    return re.sub(r"[-_.]+", "-", name).lower()


def import_name(pyproject_text: str, pkg_dir: Path) -> str | None:
    """The top-level module a wheel installs, from the poetry or hatch package list."""
    m = re.search(r'include\s*=\s*"([^"/]+)"', pyproject_text)  # poetry: packages = [{ include = "x" }]
    candidates = [m.group(1)] if m else []
    m = re.search(r"^packages\s*=\s*\[([^\]]*)\]", pyproject_text, re.M)  # hatch: packages = ["src/x"]
    if m:
        candidates += [p.strip().strip('"\'').split("/")[-1] for p in m.group(1).split(",") if p.strip()]
    for c in candidates:
        if (pkg_dir / c).is_dir() or (pkg_dir / "src" / c).is_dir():
            return c
    return None


def declared_versions(pkg_dir: Path, module: str | None) -> dict[str, str]:
    """``__version__`` strings the package itself carries: {relative file: version}."""
    out: dict[str, str] = {}
    if not module:
        return out
    for base in (pkg_dir / module, pkg_dir / "src" / module):
        for f in (base / "version.py", base / "__init__.py"):
            if f.is_file():
                m = VERSION_RE.search(f.read_text())
                if m:
                    out[str(f.relative_to(pkg_dir))] = m.group(1)
    return out


def python_packages(root: Path = REPO_ROOT) -> list[dict]:
    dirs = [root / "python", root / "python" / "traceai"] + sorted(p for p in (root / "python" / "frameworks").iterdir() if p.is_dir())
    out = []
    for d in dirs:
        f = d / "pyproject.toml"
        if not f.is_file():
            continue
        text = f.read_text()
        name, version = _toml_str(text, "name"), _toml_str(text, "version")
        if not name or not version:
            continue
        module = import_name(text, d)
        out.append({
            "ecosystem": "python",
            "name": name,
            "version": version,
            "dir": str(d.relative_to(root)),
            "import_name": module,
            "tag": f"{pypi_normalize(name)}-v{version}",
            "core": name in PYTHON_CORE,
            "changelog": str((d / "CHANGELOG.md").relative_to(root)) if (d / "CHANGELOG.md").is_file() else None,
            "declared": declared_versions(d, module),
        })
    return out


def typescript_packages(root: Path = REPO_ROOT) -> list[dict]:
    out = []
    for d in sorted(p for p in (root / "typescript" / "packages").iterdir() if p.is_dir()):
        f = d / "package.json"
        if not f.is_file():
            continue
        pkg = json.loads(f.read_text())
        if pkg.get("private") or not pkg.get("name") or not pkg.get("version"):
            continue
        name = pkg["name"]
        out.append({
            "ecosystem": "typescript",
            "name": name,
            "version": pkg["version"],
            "dir": str(d.relative_to(root)),
            "tag": f"npm-{name.lstrip('@').replace('/', '-')}-v{pkg['version']}",
            "core": name in TS_CORE,
            "changelog": str((d / "CHANGELOG.md").relative_to(root)) if (d / "CHANGELOG.md").is_file() else None,
        })
    return out


def load_config(path: Path = CONFIG) -> dict:
    return json.loads(path.read_text()) if path.is_file() else {"skip": {}}


def consistency_problems(packages: list[dict]) -> list[str]:
    """A __version__ that disagrees with the package version, or two packages with one name."""
    problems = []
    seen: dict[str, str] = {}
    for p in packages:
        key = f"{p['ecosystem']}:{pypi_normalize(p['name']) if p['ecosystem'] == 'python' else p['name']}"
        if key in seen:
            problems.append(f"{p['dir']}: name {p['name']} is also used by {seen[key]}")
        seen[key] = p["dir"]
        for rel, v in p.get("declared", {}).items():
            if v != p["version"]:
                problems.append(f"{p['dir']}/{rel}: __version__ is {v} but pyproject.toml says {p['version']}")
    return problems


def _get_json(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "traceAI-release-plan"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def published_versions(p: dict) -> set[str] | None:
    """Versions the registry has for this package; None when the name does not exist there."""
    if p["ecosystem"] == "python":
        d = _get_json(f"https://pypi.org/pypi/{p['name']}/json")
        return set(d["releases"]) if d else None
    d = _get_json("https://registry.npmjs.org/" + p["name"].replace("/", "%2F"))
    return set(d.get("versions", {})) if d else None


def plan(packages: list[dict], config: dict, *, offline: bool = False, only: set[str] | None = None) -> dict:
    out: dict = {"python": [], "typescript": [], "skipped": []}
    skip = config.get("skip", {})
    for p in packages:
        if only and p["name"] not in only:
            continue
        if p["name"] in skip:
            out["skipped"].append({"name": p["name"], "reason": skip[p["name"]]})
            continue
        if not offline:
            have = published_versions(p)
            if have is not None and p["version"] in have:
                out["skipped"].append({"name": p["name"], "reason": f"{p['version']} is already published"})
                continue
            p = {**p, "first_release": have is None}
        entry = {k: v for k, v in p.items() if k not in ("declared", "ecosystem")}
        out[p["ecosystem"]].append(entry)
    for eco in ("python", "typescript"):
        out[eco].sort(key=lambda e: (not e["core"], e["name"].lower()))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="list every package without asking the registries")
    ap.add_argument("--check", action="store_true", help="only check version consistency; exit 1 on a problem")
    ap.add_argument("--only", nargs="*", metavar="NAME", help="limit the plan to these package names")
    ap.add_argument("--root", default=str(REPO_ROOT), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    root = Path(args.root)
    packages = python_packages(root) + typescript_packages(root)
    problems = consistency_problems(packages)
    if problems:
        for pr in problems:
            print(f"release: {pr}", file=sys.stderr)
        return 1
    if args.check:
        print(f"release: {len(packages)} packages, versions consistent", file=sys.stderr)
        return 0
    result = plan(packages, load_config(), offline=args.offline, only=set(args.only or []) or None)
    json.dump(result, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
