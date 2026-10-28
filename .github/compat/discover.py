#!/usr/bin/env python3
"""Registry adapters. Discovery is evidence, never a compatibility verdict.

PyPI uses PEP 440 and artifact-level age/Requires-Python/yank filtering. npm's
publisher latest tag is not its highest semver. Go paths are explicit (including
/vN); a go.mod requirement is NOT a support floor. Native execution is deferred.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.utils import parse_wheel_filename, InvalidWheelFilename
from packaging.version import InvalidVersion, Version

UTC = dt.timezone.utc
MAX_METADATA = 32 * 1024 * 1024


def timestamp(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def fetch(url, *, text=False):
    request = urllib.request.Request(url, headers={"User-Agent": "traceAI-compatibility/1"})
    with urllib.request.urlopen(request, timeout=45) as response:
        if not response.url.startswith("https://"):
            raise ValueError("Registry redirected to a non-HTTPS URL")
        raw = response.read(MAX_METADATA + 1)
    if len(raw) > MAX_METADATA:
        raise ValueError("Oversized registry response")
    return raw.decode() if text else json.loads(raw)


def pypi(name, data, seen_at):
    releases = []
    for version, files in data.get("releases", {}).items():
        try:
            parsed = Version(version)
        except InvalidVersion:
            continue
        artifacts = [{"filename": f["filename"], "published_at": f.get("upload_time_iso_8601"),
                      "requires_python": f.get("requires_python"), "yanked": bool(f.get("yanked")),
                      "sha256": f.get("digests", {}).get("sha256"), "url": f.get("url")}
                     for f in files]
        dates = [f["published_at"] for f in artifacts if f["published_at"]]
        releases.append({"version": str(parsed), "prerelease": parsed.is_prerelease or parsed.is_devrelease,
                         "published_at": min(dates, key=timestamp) if dates else None,
                         "first_seen_at": seen_at, "yanked": not files or all(f["yanked"] for f in artifacts),
                         "artifacts": artifacts})
    return {"name": name, "ecosystem": "pypi", "dist_tags": {},
            "releases": sorted(releases, key=lambda r: Version(r["version"]))}


def semver(value):
    """Strict npm/Go semver ordering (not PEP 440's rc/post normalization)."""
    match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?", value)
    if not match:
        raise ValueError(f"Invalid semver: {value}")
    major, minor, patch, pre = match.groups()
    if pre and any(p.isdigit() and len(p) > 1 and p.startswith("0") or not p for p in pre.split(".")):
        raise ValueError(f"Invalid semver prerelease: {value}")
    parts = tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split(".")) if pre else ()
    return int(major), int(minor), int(patch), pre is None, parts


def npm(name, data, seen_at):
    releases = []
    for version, metadata in data.get("versions", {}).items():
        try:
            parsed = semver(version)
        except ValueError:
            continue
        releases.append({"version": version, "prerelease": not parsed[3],
                         "published_at": data.get("time", {}).get(version), "first_seen_at": seen_at,
                         "yanked": False, "deprecated": metadata.get("deprecated"),
                         "engines": metadata.get("engines", {}), "dist": metadata.get("dist", {})})
    return {"name": name, "ecosystem": "npm", "dist_tags": data.get("dist-tags", {}),
            "releases": sorted(releases, key=lambda r: semver(r["version"]))}


def go_escape(path):
    return "".join("!" + c.lower() if c.isupper() else c for c in path)


def go_retractions(mod):
    """Parse literal retract directives, including ranges and block form."""
    clean = re.sub(r"//[^\n]*", "", mod)
    directives = re.findall(r"(?m)^\s*retract\s+([^\n(]+)", clean)
    for block in re.findall(r"(?ms)^\s*retract\s*\((.*?)\)", clean):
        directives.extend(line.strip() for line in block.splitlines() if line.strip())
    result = []
    for directive in directives:
        versions = re.findall(r"v\d+\.\d+\.\d+(?:-[\w.-]+)?", directive)
        if len(versions) not in (1, 2):
            raise ValueError("Unrecognized Go retract directive")
        result.append((versions[0], versions[-1]))
    return result


def go(name, data, seen_at):
    retractions = go_retractions(data.get("latest_go_mod", ""))
    releases = []
    for info in data.get("versions", []):
        version = info["Version"]
        parsed = semver(version)
        retracted = any(semver(lo) <= parsed <= semver(hi) for lo, hi in retractions)
        mod = info.get("go_mod", "")
        module = re.search(r'(?m)^module\s+"?([^"\s]+)"?\s*$', mod)
        if mod and (not module or module.group(1) != name):
            raise ValueError("Go module path metadata disagrees with the explicit inventory path")
        go_version = re.search(r"(?m)^go\s+([0-9.]+)\s*$", mod)
        releases.append({"version": version, "prerelease": not parsed[3], "published_at": info.get("Time"),
                         "first_seen_at": seen_at, "yanked": retracted, "retracted": retracted,
                         "go_mod": info.get("go_mod"), "module_path": name,
                         "go_version": go_version.group(1) if go_version else None})
    return {"name": name, "ecosystem": "go", "dist_tags": {}, "retractions": retractions,
            "module_path_assessment": "New /vN paths require explicit inventory review; not auto-discovered.",
            "releases": sorted(releases, key=lambda r: semver(r["version"]))}


def live(ecosystem, name, seen_at):
    if ecosystem == "pypi":
        return pypi(name, fetch(f"https://pypi.org/pypi/{urllib.parse.quote(name, safe='')}/json"), seen_at)
    if ecosystem == "npm":
        return npm(name, fetch(f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='')}"), seen_at)
    if ecosystem == "go":
        base = "https://proxy.golang.org/" + go_escape(name)
        versions = fetch(base + "/@v/list", text=True).splitlines()
        latest = fetch(base + "/@latest")
        mod = fetch(base + "/@v/" + latest["Version"] + ".mod", text=True)
        # Retracts are defined by the latest version's go.mod, not by the selected old version.
        data = {"latest_go_mod": mod, "versions": []}
        for v in sorted(set(versions + [latest["Version"]]), key=semver):
            info = fetch(base + "/@v/" + v + ".info")
            info["go_mod"] = fetch(base + "/@v/" + v + ".mod", text=True)
            data["versions"].append(info)
        return go(name, data, seen_at)
    raise ValueError(f"Discovery deferred for {ecosystem}")


def usable_artifact(artifact, cutoff, runtime):
    """Binary-only execution: age/yank/Python eligibility applies to a usable wheel.

    A minor string cannot prove Requires-Python eligibility; selection needs the
    exact interpreter patch and its actual PEP 425 platform/ABI tags.
    """
    if not isinstance(runtime, dict) or not runtime.get("tags") or len(runtime["version"].split(".")) != 3:
        raise ValueError("Exact runtime identity and platform tags required")
    try:
        _, _, _, tags = parse_wheel_filename(artifact["filename"])
    except InvalidWheelFilename:
        return False
    return (not artifact["yanked"] and bool(artifact["published_at"])
            and timestamp(artifact["published_at"]) <= cutoff
            and bool({str(t) for t in tags} & set(runtime["tags"]))
            and (not artifact["requires_python"] or SpecifierSet(artifact["requires_python"]).contains(runtime["version"])))


def eligible(release, cutoff, runtime=None):
    if release["yanked"]:
        return False
    if "artifacts" in release:
        return any(usable_artifact(f, cutoff, runtime) for f in release["artifacts"])
    date = release["published_at"] or release.get("first_seen_at")
    return bool(date and timestamp(date) <= cutoff)


def summarize(package, cutoff, runtime=None, specifier=None):
    versions = package["releases"]
    stable = [v for v in versions if not v["prerelease"] and not v["yanked"]]
    allowed = [v for v in stable if eligible(v, cutoff, runtime)]
    if package["ecosystem"] == "pypi" and specifier is not None:
        allowed = [v for v in allowed if SpecifierSet(specifier).contains(v["version"])]
    return {"absolute_latest": stable[-1]["version"] if stable else None,
            "latest_eligible": allowed[-1]["version"] if allowed else None,
            "dist_tags": package.get("dist_tags", {})}


def discover(registry, now, fixtures=None):
    """Only enabled qualified/pending pilots float; inventory remains the authority."""
    names = set()
    for target in registry["integrations"]:
        if target["canary"]["enabled"]:
            eco = {"python": "pypi", "typescript": "npm", "go": "go"}.get(target["language"])
            if eco is None:
                raise ValueError("Enabled discovery adapter is deferred")
            for dependency in target["upstream"]:
                names.add((eco, dependency["name"]))
            if "published" in target["canary"]["modes"]:
                names.add((eco, target["distribution"]))
    # Published instrumentation and core must both be selected, never source core.
    core = next(t for t in registry["integrations"] if t["language"] == "python" and t["role"] == "core")
    names.add(("pypi", core["distribution"]))
    result = {"schema_version": 1, "discovered_at": iso(now), "packages": []}
    for ecosystem, name in sorted(names):
        if fixtures is not None:
            raw = json.loads((fixtures / f"{ecosystem}.json").read_text())[name]
            package = {"pypi": pypi, "npm": npm, "go": go}[ecosystem](name, raw, iso(now))
        else:
            package = live(ecosystem, name, iso(now))
        result["packages"].append(package)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--now")
    args = parser.parse_args()
    now = timestamp(args.now) if args.now else dt.datetime.now(UTC)
    registry = json.loads((args.root / ".github/compat/registry.json").read_text())
    output = discover(registry, now, args.fixtures)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
