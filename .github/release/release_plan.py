#!/usr/bin/env python3
"""Read-only release inventory and registry plan for all five SDK ecosystems.

--check validates Python runtime versions and release-please coverage without network.
--offline inventories publishable packages without registry calls. --only NAME ...
limits the plan. --root allows a separate source tree. No command publishes anything.
The release publisher, not release-please, owns tags and GitHub releases.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = Path(__file__).resolve().parent / "config.json"
PYTHON_CORE = {"fi-instrumentation-otel"}
TS_CORE = {"@traceai/fi-core", "@traceai/fi-semantic-conventions"}
ECOSYSTEMS = ("python", "typescript", "java", "go", "nuget")
VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)
MAVEN_NS = {"m": "http://maven.apache.org/POM/4.0.0"}


def pypi_normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def import_name(pyproject_text: str, pkg_dir: Path) -> str | None:
    """Find a module without executing package code."""
    data = tomllib.loads(pyproject_text)
    poetry = data.get("tool", {}).get("poetry", {})
    candidates = [p["include"] for p in poetry.get("packages", []) if "include" in p]
    hatch = data.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {})
    candidates += [p.split("/")[-1] for p in hatch.get("packages", [])]
    for candidate in candidates:
        if (pkg_dir / candidate).is_dir() or (pkg_dir / "src" / candidate).is_dir():
            return candidate
    return None


def declared_versions(pkg_dir: Path, module: str | None) -> dict[str, str]:
    out = {}
    if module:
        for base in (pkg_dir / module, pkg_dir / "src" / module):
            for f in sorted(set(base.rglob("version.py")) | set(base.rglob("__init__.py"))):
                if f.is_file() and (match := VERSION_RE.search(f.read_text())):
                    out[str(f.relative_to(pkg_dir))] = match.group(1)
    return out


def python_packages(root: Path = REPO_ROOT) -> list[dict]:
    out = []
    for f in sorted((root / "python").rglob("pyproject.toml")):
        d = f.parent
        text = f.read_text()
        data = tomllib.loads(text)
        pkg = data.get("project", data.get("tool", {}).get("poetry", {}))
        name, version = pkg.get("name"), pkg.get("version")
        if not name or not version:
            continue
        module = import_name(text, d)
        out.append({
            "ecosystem": "python", "name": name, "version": version,
            "dir": str(d.relative_to(root)), "import_name": module,
            # Core uses the non-recursive simple strategy with explicit updaters.
            # The python/ root catches manifest-only dependency/security fixes too.
            "release_path": str(d.relative_to(root)),
            "tag": f"{pypi_normalize(name)}-v{version}", "core": name in PYTHON_CORE,
            "changelog": str((d / "CHANGELOG.md").relative_to(root)) if (d / "CHANGELOG.md").is_file() else None,
            "declared": declared_versions(d, module),
        })
    return out


def typescript_packages(root: Path = REPO_ROOT) -> list[dict]:
    out = []
    files = sorted((root / "typescript" / "packages").glob("*/package.json"))
    names = {json.loads(f.read_text()).get("name") for f in files}
    for f in files:
        pkg = json.loads(f.read_text())
        if pkg.get("private") or not pkg.get("name") or not pkg.get("version"):
            continue
        name, d = pkg["name"], f.parent
        out.append({
            "ecosystem": "typescript", "name": name, "version": pkg["version"],
            "dir": str(d.relative_to(root)),
            "tag": f"npm-{name.lstrip('@').replace('/', '-')}-v{pkg['version']}",
            # Include internal edges even when declared with an ordinary semver
            # range, not only workspace:*. Dev-only edges do not affect delivery.
            "workspace_dependencies": sorted({dep for section in ("dependencies", "optionalDependencies", "peerDependencies")
                                               for dep in pkg.get(section, {}) if dep in names}),
            "core": name in TS_CORE,
            "changelog": str((d / "CHANGELOG.md").relative_to(root)) if (d / "CHANGELOG.md").is_file() else None,
        })
    return out


def other_packages(root: Path = REPO_ROOT) -> list[dict]:
    out = []
    pom = root / "java/pom.xml"
    if pom.is_file():
        p = ET.fromstring(pom.read_text())
        version = p.findtext("m:version", namespaces=MAVEN_NS)
        group = p.findtext("m:groupId", namespaces=MAVEN_NS)
        artifact = p.findtext("m:artifactId", namespaces=MAVEN_NS)
        artifacts = [artifact]
        for module in p.findall("m:modules/m:module", MAVEN_NS):
            child = ET.parse(root / "java" / module.text / "pom.xml").getroot()
            artifacts.append(child.findtext("m:artifactId", namespaces=MAVEN_NS))
        out.append({"ecosystem": "java", "name": f"{group}:{artifact}", "version": version,
                    "dir": "java", "tag": f"java-v{version}", "core": False,
                    "group": group, "artifacts": artifacts, "changelog": "java/CHANGELOG.md"})
    manifest_path = root / ".release-please-manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    for f in sorted((root / "go").glob("*/go.mod")):
        module = re.search(r"^module\s+(\S+)", f.read_text(), re.M).group(1)
        path = str(f.parent.relative_to(root))
        version = manifest.get(path, "0.1.0")
        out.append({"ecosystem": "go", "name": module, "version": version,
                    "dir": path, "tag": f"{path}/v{version}", "core": False,
                    "changelog": f"{path}/CHANGELOG.md"})
    for f in sorted((root / "csharp/src/FIInstrumentation").glob("*.csproj")):
        p = ET.fromstring(f.read_text())
        name, version = p.findtext(".//PackageId"), p.findtext(".//Version")
        out.append({"ecosystem": "nuget", "name": name, "version": version,
                    "dir": str(f.parent.relative_to(root)), "project": str(f.relative_to(root)),
                    "tag": f"nuget-{name}-v{version}", "core": False,
                    "changelog": str(f.parent.relative_to(root)) + "/CHANGELOG.md"})
    return out


def load_config(path: Path = CONFIG) -> dict:
    return json.loads(path.read_text()) if path.is_file() else {"skip": {}}


def consistency_problems(packages: list[dict]) -> list[str]:
    problems, seen = [], {}
    for p in packages:
        key = f"{p['ecosystem']}:{pypi_normalize(p['name']) if p['ecosystem'] == 'python' else p['name']}"
        if key in seen:
            problems.append(f"{p['dir']}: name {p['name']} is also used by {seen[key]}")
        seen[key] = p["dir"]
        for rel, v in p.get("declared", {}).items():
            if v != p["version"]:
                problems.append(f"{p['dir']}/{rel}: __version__ is {v} but pyproject.toml says {p['version']}")
    return problems


def coverage_problems(root: Path, packages: list[dict]) -> list[str]:
    config_file = root / "release-please-config.json"
    if not config_file.is_file():
        return []  # Small unit-test fixtures and dev trees before bootstrap.
    cfg = json.loads(config_file.read_text())["packages"]
    versions = json.loads((root / ".release-please-manifest.json").read_text())
    problems = []
    for p in packages:
        path = p.get("release_path", p["dir"])
        if path not in cfg or path not in versions:
            problems.append(f"{path}: missing release-please config or manifest entry")
        for rel in p.get("declared", {}):
            f = root / p["dir"] / rel
            scoped_rel = str(f.relative_to(root / path))
            if "x-release-please-version" not in f.read_text():
                problems.append(f"{f.relative_to(root)}: missing release-please version marker")
            if scoped_rel not in cfg.get(path, {}).get("extra-files", []):
                problems.append(f"{p['dir']}/{rel}: missing release-please extra-file")
    return problems


def _get_json(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "traceAI-release-plan"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise  # Authentication failures, rate limits and server errors must never mean 'not published'.


def published_versions(p: dict) -> set[str] | None:
    eco = p["ecosystem"]
    if eco == "python":
        d = _get_json(f"https://pypi.org/pypi/{p['name']}/json")
        return set(d["releases"]) if d else None
    if eco == "typescript":
        d = _get_json("https://registry.npmjs.org/" + p["name"].replace("/", "%2F"))
        return set(d.get("versions", {})) if d else None
    if eco == "nuget":
        d = _get_json(f"https://api.nuget.org/v3-flatcontainer/{p['name'].lower()}/index.json")
        return set(d["versions"]) if d else None
    if eco == "go":
        # Go proxy escaping: every uppercase letter becomes ! plus lowercase.
        module = re.sub(r"[A-Z]", lambda m: "!" + m.group().lower(), p["name"])
        d = _get_json(f"https://proxy.golang.org/{module}/@v/v{p['version']}.info")
        return {p["version"]} if d else None
    if eco == "java":
        # Query canonical repository files rather than the eventually consistent search index.
        # One reactor publishes together; do not skip if any module is missing.
        for artifact in p["artifacts"]:
            url = f"https://repo.maven.apache.org/maven2/{p['group'].replace('.', '/')}/{artifact}/{p['version']}/{artifact}-{p['version']}.pom"
            try:
                with urllib.request.urlopen(url, timeout=30):
                    pass
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                raise
        return {p["version"]}
    raise ValueError(f"Unknown ecosystem: {eco}")


def plan(packages: list[dict], config: dict, *, offline: bool = False, only: set[str] | None = None, include_published: bool = False) -> dict:
    out = {eco: [] for eco in ECOSYSTEMS}
    out["skipped"] = []
    skip = config.get("skip", {})
    for p in packages:
        if only and p["name"] not in only:
            continue
        if p["name"] in skip:
            out["skipped"].append({"name": p["name"], "reason": skip[p["name"]]})
            continue
        if p["ecosystem"] == "java" and (p["group"] != "com.futureagi" or "SNAPSHOT" in p["version"]):
            out["skipped"].append({"name": p["name"], "reason": "Central requires com.futureagi release coordinates; merge the Java release-profile PR first"})
            continue
        if not offline:
            have = published_versions(p)
            published = have is not None and p["version"] in have
            if published and not include_published:
                out["skipped"].append({"name": p["name"], "reason": f"{p['version']} is already published"})
                continue
            p = {**p, "first_release": have is None, "registry_published": published}
        out[p["ecosystem"]].append({k: v for k, v in p.items() if k not in ("declared", "ecosystem")})
    for eco in ECOSYSTEMS:
        out[eco].sort(key=lambda e: (not e["core"], e["name"].lower()))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--include-published", action="store_true", help="include existing registry versions for tag/release reconciliation")
    ap.add_argument("--only", nargs="*", metavar="NAME")
    ap.add_argument("--root", default=str(REPO_ROOT))
    args = ap.parse_args(argv)
    root = Path(args.root)
    packages = python_packages(root) + typescript_packages(root) + other_packages(root)
    problems = consistency_problems(packages) + coverage_problems(root, packages)
    if problems:
        for problem in problems:
            print(f"release: {problem}", file=sys.stderr)
        return 1
    if args.check:
        print(f"release: {len(packages)} packages, versions and coverage consistent", file=sys.stderr)
        return 0
    result = plan(packages, load_config(), offline=args.offline, only=set(args.only or []) or None, include_published=args.include_published)
    json.dump(result, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
