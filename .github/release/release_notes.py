#!/usr/bin/env python3
"""The GitHub release notes of one package version: its CHANGELOG section, plus the install line.

    python3 .github/release/release_notes.py NAME VERSION [--changelog PATH] [--install "pip install ..."]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HEADING = re.compile(r"^##\s+\[?v?(?P<version>[0-9][^\]\s]*)\]?.*$", re.M)


def section(text: str, version: str) -> str | None:
    """The body under ``## [version]`` (Keep a Changelog) or ``## version ...``, or None."""
    heads = list(HEADING.finditer(text))
    for i, h in enumerate(heads):
        if h.group("version") == version:
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            return text[h.end():end].strip() or None
    return None


def notes(name: str, version: str, *, changelog_text: str | None, install: str, changelog_path: str | None = None) -> str:
    body = section(changelog_text, version) if changelog_text else None
    where = f" (`{changelog_path}`)" if changelog_path else ""
    if body is None:
        body = f"_This version has no CHANGELOG entry{where}; see the commits since the previous tag._"
    return f"{body}\n\n**Install:** `{install}`\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("name")
    ap.add_argument("version")
    ap.add_argument("--changelog", help="the package's CHANGELOG.md")
    ap.add_argument("--install", required=True, help="the install command to print")
    args = ap.parse_args(argv)
    text = Path(args.changelog).read_text() if args.changelog and Path(args.changelog).is_file() else None
    sys.stdout.write(notes(args.name, args.version, changelog_text=text, install=args.install, changelog_path=args.changelog if text else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
