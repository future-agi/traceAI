#!/usr/bin/env python3
"""Clear release-please's pending label only after all release tags exist.

release-please release-pr refuses new PRs while a merged release PR is pending.
Publishers own the tags, so they must also acknowledge completed release PRs.
"""
import base64
import json
import os
import subprocess
from pathlib import Path

from release_plan import REPO_ROOT, load_config, other_packages, python_packages, typescript_packages, published_versions


def gh_json(*args):
    return json.loads(subprocess.check_output(["gh", *args], text=True))


def changed_release_paths(repo, pr):
    """{manifest path: version} for every version this merged release PR changed, or None when that cannot be read."""
    try:
        merge = pr["mergeCommit"]["oid"]
        parent = gh_json("api", f"repos/{repo}/commits/{merge}")["parents"][0]["sha"]

        def manifest(ref):
            d = gh_json("api", f"repos/{repo}/contents/.release-please-manifest.json?ref={ref}")
            return json.loads(base64.b64decode(d["content"]))

        before, after = manifest(parent), manifest(merge)
        return {path: version for path, version in after.items() if before.get(path) != version}
    except Exception:
        return None


def with_version(package, version):
    """The package as released at version: tags end in the version (name-vX, npm-...-vX, java-vX, go/x/vX)."""
    tag = package["tag"]
    if tag.endswith(package["version"]):
        tag = tag[: -len(package["version"])] + version
    return {**package, "version": version, "tag": tag}


def unreleased(packages, tags, skip):
    """The first reason these package versions are not fully released, or None."""
    missing = [p["tag"] for p in packages if p["name"] not in skip and p["tag"] not in tags]
    if missing:
        return f"{len(missing)} package releases ({', '.join(missing[:5])}); finish credentials/publishing and rerun Release"
    for package in packages:
        if package["name"] in skip:
            continue
        # A historical Java reactor is an atomic Central deployment. New modules in today's
        # inventory did not exist at the release version and cannot be uploaded retroactively.
        versions = published_versions(package, strict=False) if package["ecosystem"] == "java" else published_versions(package)
        if versions is None or package["version"] not in versions:
            return f"{package['name']} {package['version']} is not served by its registry"
    return None


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    pending = gh_json("pr", "list", "--repo", repo, "--base", "main", "--state", "merged", "--label", "autorelease: pending",
                      "--limit", "100", "--json", "number,headRefName,mergeCommit")
    pending = [p for p in pending if p["headRefName"].startswith("release-please--branches--main")]
    if not pending:
        return
    releases = gh_json("api", "--paginate", "--slurp", f"repos/{repo}/releases?per_page=100")
    tags = {r["tag_name"] for page in releases for r in page}
    skip = load_config().get("skip", {})
    packages = python_packages(REPO_ROOT) + typescript_packages(REPO_ROOT) + other_packages(REPO_ROOT)
    complete = []
    for pr in pending:
        # Each release PR is complete once the versions it changed are released, checked at the
        # version it released (a later bump on main may have moved past it). A first-time package
        # still waiting on its registry does not hold back unrelated release PRs. If a PR's changes
        # cannot be read, it stays pending: the current checkout says nothing about what it released.
        changed = changed_release_paths(repo, pr)
        if changed is None:
            print(f"Still pending: release PR #{pr['number']}: could not read the versions it released")
            continue
        known = {p.get("release_path", p["dir"]): p for p in packages}
        unknown = changed.keys() - known.keys()
        if unknown:
            print(f"Still pending: release PR #{pr['number']}: release paths missing from current inventory: {', '.join(sorted(unknown))}")
            continue
        required = [with_version(known[path], version) for path, version in changed.items()]
        try:
            reason = unreleased(required, tags, skip)
        except Exception as exc:
            # A registry outage must not acknowledge this PR or block an unrelated one.
            print(f"Still pending: release PR #{pr['number']}: registry check failed ({type(exc).__name__})")
            continue
        if reason:
            print(f"Still pending: release PR #{pr['number']}: {reason}")
        else:
            complete.append(pr)
    if not complete:
        return
    subprocess.run(["gh", "label", "create", "autorelease: tagged", "--repo", repo, "--force", "--color", "0e8a16",
                    "--description", "release-please: release PR whose versions are all tagged and released"], check=False)
    for pr in complete:
        subprocess.run(["gh", "pr", "edit", str(pr["number"]), "--repo", repo,
                        "--remove-label", "autorelease: pending", "--add-label", "autorelease: tagged"], check=True)

if __name__ == "__main__":
    main()
