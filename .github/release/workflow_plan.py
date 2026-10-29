#!/usr/bin/env python3
"""Filter registry inventory to unfinished releases and write workflow matrices.

Keep a package until BOTH its registry version and GitHub release exist. This
allows retrying after a successful upload but failed verification/tagging.
"""
import json
import os
import sys
from pathlib import Path

from release_plan import ECOSYSTEMS


def unfinished(plan, release_tags):
    for eco in ECOSYSTEMS:
        plan[eco] = [p for p in plan[eco] if not (p.get("registry_published") and p["tag"] in release_tags)]
    return plan


def main():
    plan = unfinished(json.loads(Path(sys.argv[1]).read_text()), set(Path(sys.argv[2]).read_text().splitlines()))
    Path(sys.argv[1]).write_text(json.dumps(plan, indent=2) + "\n")
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for eco in ECOSYSTEMS:
            groups = {eco: plan[eco]}
            if eco in ("python", "typescript"):
                groups = {eco: [p for p in plan[eco] if not p["core"]],
                          eco + "_core": [p for p in plan[eco] if p["core"]]}
            if eco == "typescript":
                semantic = "@traceai/fi-semantic-conventions"
                groups["typescript_semantic"] = [p for p in groups["typescript_core"] if p["name"] == semantic]
                groups["typescript_core"] = [p for p in groups["typescript_core"] if p["name"] != semantic]
            for name, entries in groups.items():
                output.write(f"{name}={json.dumps(entries, separators=(',', ':'))}\n")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        summary.write("### Release plan\n\n| Package | Version | Registry upload needed? |\n|---|---|---|\n")
        for eco in ECOSYSTEMS:
            for p in plan[eco]:
                summary.write(f"| {p['name']} | {p['version']} | {'no (reconcile release)' if p.get('registry_published') else 'yes'} |\n")
        summary.write("\nSkipped:\n")
        for p in plan["skipped"]:
            summary.write(f"- {p['name']}: {p['reason']}\n")


if __name__ == "__main__":
    main()
