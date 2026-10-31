#!/usr/bin/env python3
"""Read-only, reproducible audit of same-version code stranded at bootstrap.

Read package manifests from --ref, query public registry upload timestamps, then
inspect first-parent package history after that timestamp. No package code runs.
--check validates the committed one-time bumps without querying a registry.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import tomllib

from release_plan import REPO_ROOT, _get_json, load_config

BASELINE = "35f2e3dc364676fa5bdab1e71ae500daf9efc00c"
AUDIT = Path(__file__).with_name("bootstrap-audit.json")


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True)


def manifests(root: Path, ref: str) -> list[dict]:
    out = []
    for file in git(root, "ls-tree", "-r", "--name-only", ref).splitlines():
        if file.startswith("python/") and file.endswith("/pyproject.toml"):
            data = tomllib.loads(git(root, "show", f"{ref}:{file}"))
            pkg = data.get("project", data.get("tool", {}).get("poetry", {}))
            ecosystem = "python"
        elif re.fullmatch(r"typescript/packages/[^/]+/package.json", file):
            pkg = json.loads(git(root, "show", f"{ref}:{file}"))
            ecosystem = "typescript"
        else:
            continue
        if pkg.get("private") or not pkg.get("name") or not pkg.get("version"):
            continue
        directory = str(Path(file).parent)
        out.append({"ecosystem": ecosystem, "name": pkg["name"],
                    "version": pkg["version"], "manifest": file, "dir": directory,
                    "release_path": directory})
    return out


def upload_time(package: dict, registry: dict | None) -> str | None:
    if registry is None:
        return None
    version = package["version"]
    if package["ecosystem"] == "python":
        files = registry.get("releases", {}).get(version, [])
        # Empty release entries are not downloadable versions. Use the LAST file
        # upload so a wheel/sdist uploaded seconds apart is treated as one release.
        return max((f["upload_time_iso_8601"] for f in files), default=None)
    return registry.get("time", {}).get(version) if version in registry.get("versions", {}) else None


def patch_version(version: str, existing: set[str]) -> str:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise ValueError(f"Bootstrap requires a stable semantic version: {version}")
    major, minor, patch = map(int, match.groups())
    # Never suggest re-uploading a version, even if a registry is ahead of dev.
    patches = [int(m.group(1)) for v in existing
               if (m := re.fullmatch(rf"{major}\.{minor}\.(\d+)", v))]
    return f"{major}.{minor}.{max([patch, *patches]) + 1}"


def package_history(root: Path, ref: str, package: dict, since: str) -> list[dict]:
    directory = package["dir"]
    paths = [directory]
    if directory == "python":
        # Core doesn't own sibling SDKs. Retain its own source, tests, examples,
        # pyproject/lockfile and docs, rather than attributing every SDK to core.
        paths += [":(exclude)python/frameworks", ":(exclude)python/traceai"]
    # A branch may be authored (and committed) before its registry upload.
    # Compare first-parent landing/merge committer dates, not branch author dates.
    # Filter without pruning traversal when later first-parent dates go backwards.
    text = git(root, "log", "--first-parent", f"--since-as-filter={since}",
               "--format=%H%x09%cI%x09%s", ref, "--", *paths)
    return [dict(zip(("sha", "committed_at", "subject"), line.split("\t", 2)))
            for line in text.splitlines()]


def audit_package(root: Path, ref: str, package: dict, skipped: dict) -> dict:
    if package["name"] in skipped:
        return {**package, "status": "excluded", "reason": skipped[package["name"]]}
    if package["ecosystem"] == "python":
        url = f"https://pypi.org/pypi/{package['name']}/json"
    else:
        url = "https://registry.npmjs.org/" + package["name"].replace("/", "%2F")
    registry = _get_json(url)
    uploaded = upload_time(package, registry)
    result = {**package, "registry_url": url, "uploaded_at": uploaded}
    if not uploaded:
        return {**result, "status": "unpublished_current_version"}
    history = package_history(root, ref, package, uploaded)
    if not history:
        return {**result, "status": "unchanged_since_upload", "commits": []}
    existing = set(registry["releases"] if package["ecosystem"] == "python" else registry["versions"])
    return {**result, "status": "patch_required", "next_version": patch_version(package["version"], existing),
            "commits": history}


def check(root: Path, audit: dict) -> list[str]:
    """Audit is a one-time floor, not a permanent pin on future releases."""
    versions = json.loads((root / ".release-please-manifest.json").read_text())
    errors = []
    # Later explicit repairs also need a version floor, without rewriting the
    # original bootstrap snapshot or its historical registry audit.
    for package in [*audit["packages"], *audit.get("explicit_bumps", [])]:
        if package["status"] != "patch_required":
            continue
        file = root / package["manifest"]
        if package["ecosystem"] == "python":
            data = tomllib.loads(file.read_text())
            current = data.get("project", data.get("tool", {}).get("poetry", {}))["version"]
        else:
            current = json.loads(file.read_text())["version"]
        parse = lambda v: tuple(map(int, v.split(".")))
        if parse(current) < parse(package["next_version"]):
            errors.append(f"{package['name']}: {current} strands the audited bootstrap changes; expected >= {package['next_version']}")
        if versions.get(package["release_path"]) != current:
            errors.append(f"{package['name']}: release-please manifest doesn't match {current}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--ref", default=BASELINE)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=AUDIT)
    args = parser.parse_args()
    if args.check:
        problems = check(args.root, json.loads(args.output.read_text()))
        if problems:
            print("\n".join(problems))
            return 1
        print("bootstrap: all audited same-version changes have a new manifest version")
        return 0
    ref = git(args.root, "rev-parse", args.ref).strip()
    packages = manifests(args.root, ref)
    skipped = load_config().get("skip", {})
    with ThreadPoolExecutor(max_workers=6) as pool:
        entries = list(pool.map(lambda p: audit_package(args.root, ref, p, skipped), packages))
    summary = {eco: {status: sum(p["ecosystem"] == eco and p["status"] == status for p in entries)
                     for status in ("excluded", "unpublished_current_version", "unchanged_since_upload", "patch_required")}
               for eco in ("python", "typescript")}
    result = {"baseline": ref, "queried_at": datetime.now(timezone.utc).isoformat(),
              "method": "First-parent package history after the last file upload of the baseline manifest version. Explicit one-time patch bumps; no historical replay.",
              "summary": summary, "packages": entries}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
