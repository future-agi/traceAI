#!/usr/bin/env python3
"""Publisher-owned, idempotent tags/releases. Called only after registry verification."""
import argparse
import os
import subprocess
from pathlib import Path

import release_notes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", required=True)
    parser.add_argument("--dist")
    args = parser.parse_args()
    tag, name, version = (os.environ[k] for k in ("TAG", "NAME", "VERSION"))
    if subprocess.run(["gh", "release", "view", tag], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        print(f"Release {tag} already exists")
        return
    changelog = Path(os.environ.get("CHANGELOG") or "__no_changelog__")
    notes = release_notes.notes(name, version, changelog_text=changelog.read_text() if changelog.is_file() else None,
                                install=args.install, changelog_path=str(changelog) if changelog.is_file() else None)
    path = Path(os.environ["RUNNER_TEMP"]) / "release-notes.md"
    path.write_text(notes)
    command = ["gh", "release", "create", tag, "--target", os.environ["GITHUB_SHA"],
               "--title", f"{name} {version}", "--notes-file", str(path), "--latest=false"]
    # An existing registry version need not match this checkout byte-for-byte. Never attach
    # newly built artifacts to its historical release; only new uploads get asset attachments.
    if args.dist and os.environ.get("REGISTRY_PUBLISHED") != "true":
        command += [str(p) for p in sorted(Path(args.dist).glob("*")) if p.is_file()]
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
