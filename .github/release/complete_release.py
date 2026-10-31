#!/usr/bin/env python3
"""Clear release-please's pending label only after all release tags exist.

release-please release-pr refuses new PRs while a merged release PR is pending.
Publishers own the tags, so they must also acknowledge completed release PRs.
"""
import json
import os
import subprocess
from pathlib import Path

from release_plan import REPO_ROOT, load_config, other_packages, python_packages, typescript_packages, published_versions


def gh_json(*args):
    return json.loads(subprocess.check_output(["gh", *args], text=True))


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    pending = gh_json("pr", "list", "--repo", repo, "--base", "main", "--state", "merged", "--label", "autorelease: pending",
                      "--limit", "100", "--json", "number,headRefName")
    pending = [p for p in pending if p["headRefName"].startswith("release-please--branches--main")]
    if not pending:
        return
    releases = gh_json("api", "--paginate", "--slurp", f"repos/{repo}/releases?per_page=100")
    tags = {r["tag_name"] for page in releases for r in page}
    skip = load_config().get("skip", {})
    packages = python_packages(REPO_ROOT) + typescript_packages(REPO_ROOT) + other_packages(REPO_ROOT)
    missing = [p["tag"] for p in packages if p["name"] not in skip and p["tag"] not in tags]
    if missing:
        print(f"Still pending: {len(missing)} package releases; finish credentials/publishing and rerun Release.")
        return
    for package in packages:
        if package["name"] in skip:
            continue
        versions = published_versions(package)
        if versions is None or package["version"] not in versions:
            print(f"Still pending: {package['name']} {package['version']} is not served by its registry")
            return
    for pr in pending:
        subprocess.run(["gh", "pr", "edit", str(pr["number"]), "--repo", repo,
                        "--remove-label", "autorelease: pending", "--add-label", "autorelease: tagged"], check=True)


if __name__ == "__main__":
    main()
