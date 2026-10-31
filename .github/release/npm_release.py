#!/usr/bin/env python3
"""Stage exact workspace tarballs, clean-install them, then deliver in DAG order.

The three phases keep credentials out of build and install scripts. Only publish
mutates a registry or GitHub; stage and verify work with unpublished dependencies.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from release_plan import REPO_ROOT, typescript_packages


def topological(packages: list[dict]) -> list[dict]:
    """Stable dependency-first order, with a hard failure for a dependency cycle."""
    pending = {p["name"]: p for p in packages}
    if len(pending) != len(packages):
        raise ValueError("Duplicate npm package in release plan")
    result = []
    while pending:
        ready = sorted(name for name, p in pending.items()
                       if not (set(p.get("workspace_dependencies", [])) & pending.keys()))
        if not ready:
            raise ValueError("Cyclic npm workspace dependencies: " + ", ".join(sorted(pending)))
        for name in ready:
            result.append(pending.pop(name))
    return result


def dependency_closure(packages: list[dict], inventory: list[dict]) -> list[dict]:
    by_name = {p["name"]: p for p in inventory}
    needed = set()

    def visit(name):
        if name in needed:
            return
        if name not in by_name:
            raise ValueError(f"Workspace dependency is not a publishable package: {name}")
        needed.add(name)
        for dep in by_name[name].get("workspace_dependencies", []):
            visit(dep)

    for package in packages:
        visit(package["name"])
        if by_name[package["name"]]["version"] != package["version"]:
            raise ValueError(f"Planned npm version differs from package.json: {package['name']}")
    return topological([by_name[name] for name in sorted(needed)])


def tarball_dir(dist: Path, package: dict) -> Path:
    return dist / package["name"].lstrip("@").replace("/", "-")


def tarball(dist: Path, package: dict) -> Path:
    files = list(tarball_dir(dist, package).glob("*.tgz"))
    if len(files) != 1:
        raise ValueError(f"Expected exactly one staged tarball for {package['name']}, found {len(files)}")
    return files[0].resolve()


def stage(root: Path, packages: list[dict], dist: Path) -> None:
    closure = dependency_closure(packages, typescript_packages(root))
    filters = [arg for p in packages for arg in ("--filter", p["name"] + "...")]
    subprocess.run(["pnpm", *filters, "run", "build"], cwd=root / "typescript", check=True)
    for package in closure:
        dest = tarball_dir(dist, package)
        dest.mkdir(parents=True, exist_ok=False)
        subprocess.run(["pnpm", "--filter", package["name"], "pack", "--pack-destination", str(dest)],
                       cwd=root / "typescript", check=True)
        tarball(dist, package)


def verify(root: Path, packages: list[dict], dist: Path, smoke: Path, *, offline: bool = False) -> None:
    inventory = typescript_packages(root)
    for package in packages:
        if package.get("registry_published"):
            # Already on npm, so publish never uploads it: there is nothing to gate.
            print(f"{package['name']}@{package['version']}: already on npm, not uploaded; skipping the clean-install check", flush=True)
            continue
        closure = dependency_closure([package], inventory)
        directory = tarball_dir(smoke, package)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "package.json").write_text('{"private":true}\n')
        command = ["npm", "install", "--no-audit", "--no-fund"]
        if offline:
            command.append("--offline")
        # Install all exact dependency tarballs together, not merely the target.
        # npm still checks semver ranges and installs real external dependencies.
        subprocess.run([*command, *(str(tarball(dist, p)) for p in closure)], cwd=directory, check=True)
        for staged in closure:
            installed = json.loads((directory / "node_modules" / staged["name"] / "package.json").read_text())
            if installed["version"] != staged["version"]:
                raise ValueError(f"Installed {staged['name']} != staged workspace version {staged['version']}")
        env = {**os.environ, "NAME": package["name"]}
        subprocess.run(["node", "-e", "const m = require(process.env.NAME); console.log('require ok:', Object.keys(m).length, 'exports')"],
                       cwd=directory, env=env, check=True)
        subprocess.run(["node", "--input-type=module", "-e", "import(process.env.NAME).then(m => console.log('import ok:', Object.keys(m).length, 'exports'))"],
                       cwd=directory, env=env, check=True)
        if package["name"] in {"@traceai/agent-core", "@traceai/agents"}:
            subprocess.run(["node", str(root / ".github/release/agent_smoke.cjs"), str(directory), package["name"]],
                           cwd=directory, env=env, check=True)


def wait_until_served(name: str, version: str) -> None:
    for attempt in range(20):
        result = subprocess.run(["npm", "view", f"{name}@{version}", "version"], text=True, capture_output=True)
        if result.returncode == 0 and result.stdout.strip() == version:
            return
        if attempt < 19:
            print(f"{name}@{version}: not served yet (attempt {attempt + 1}/20)", flush=True)
            time.sleep(30)
    raise RuntimeError(f"npm did not serve {name}@{version}; include unfinished dependencies in the release plan")


def publish(root: Path, packages: list[dict], dist: Path) -> None:
    inventory = {p["name"]: p for p in typescript_packages(root)}
    for package in topological(packages):
        # This also protects a manually selected consumer whose dependency was
        # omitted from --only: never upload it against a missing dependency.
        for dep in inventory[package["name"]].get("workspace_dependencies", []):
            wait_until_served(dep, inventory[dep]["version"])
        if not package.get("registry_published"):
            subprocess.run(["npm", "publish", str(tarball(dist, package)), "--access", "public", "--provenance"],
                           cwd=root / "typescript", check=True)
        wait_until_served(package["name"], package["version"])
        env = {**os.environ, **{key: str(package.get(field) or "") for key, field in
                               (("NAME", "name"), ("VERSION", "version"), ("TAG", "tag"), ("CHANGELOG", "changelog"))},
               "REGISTRY_PUBLISHED": str(bool(package.get("registry_published"))).lower()}
        subprocess.run([sys.executable, str(Path(__file__).with_name("github_release.py")),
                        "--install", f"npm install {package['name']}@{package['version']}",
                        "--dist", str(tarball_dir(dist, package))], cwd=root, env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("stage", "verify", "publish"))
    parser.add_argument("--packages", required=True, help="JSON array from the read-only release planner")
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--smoke", type=Path)
    args = parser.parse_args()
    packages = topological(json.loads(args.packages))
    if not packages:
        return
    if args.phase == "verify":
        if args.smoke is None:
            parser.error("verify requires --smoke")
        verify(args.root, packages, args.dist, args.smoke)
    else:
        {"stage": stage, "publish": publish}[args.phase](args.root, packages, args.dist)


if __name__ == "__main__":
    main()
