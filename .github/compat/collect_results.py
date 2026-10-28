#!/usr/bin/env python3
"""Always-run fail-closed collector for the original compatibility plan.

Artifacts are data: size/path/type/digest/identity checks precede all copying.
Missing, duplicate or invalid evidence is an explicit blocked/failed planned row.
Nothing supplied by an artifact is ever executed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import jsonschema
from packaging.utils import canonicalize_name
from packaging.version import Version

from plan import safe_path, sha256, validate_plan, write_json
from run_case import empty_case, failure, junit, now, run_header

MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024


def check_tree(root):
    total = 0
    count = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Symlinks are forbidden in evidence")
        if path.is_file():
            count += 1
            size = path.stat().st_size
            if size > MAX_FILE or count > 4096:
                raise ValueError("Oversized evidence file/count")
            total += size
            if total > MAX_TOTAL:
                raise ValueError("Oversized evidence tree")
        elif not path.is_dir():
            raise ValueError("Non-regular evidence file")


def validate_case(data, plan, raw, selected, schema):
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(data)
    header = data["run"]
    if header["plan_sha256"] != sha256(raw) or header["source_sha"] != plan["source_sha"] or header["workflow_sha"] != plan["workflow_sha"]:
        raise ValueError("Source/workflow/plan identity mismatch")
    expected_ids = [c["case_id"] for c in plan["cases"]]
    if header["planned_case_ids"] != expected_ids or header["self_test"] != plan["self_test"] or header["cooldown_cutoff"] != plan["cooldown_cutoff"]:
        raise ValueError("Run planning identity mismatch")
    if os.environ.get("GITHUB_RUN_ID") and header["run_id"] != os.environ["GITHUB_RUN_ID"]:
        raise ValueError("Wrong Actions run identity")
    rows = data["cases"]
    if not rows or len(rows) > 2 or any(c["case_id"] != selected["case_id"] for c in rows):
        raise ValueError("Expected one case (at most two preserved attempts) per artifact")
    if sorted(c["attempt"] for c in rows) != list(range(1, len(rows) + 1)):
        raise ValueError("Duplicate/missing attempt")
    for row in rows:
        if row["source_sha"] != plan["source_sha"] or row["integration_id"] != selected["integration_id"]:
            raise ValueError("Case source/integration mismatch")
        if row["requested"] != selected["requested"] or row["selector"] != selected["selector"] or row["mode"] != selected["mode"]:
            raise ValueError("Requested plan selection changed")
        contract = row["contract"]
        tests = contract["tests"]
        if len({t["test_id"] for t in tests}) != len(tests) or contract["collected"] != len(tests):
            raise ValueError("Duplicate/inconsistent test counts")
        if contract["executed"] != sum(t["outcome"] != "skipped" for t in tests) or contract["unexpected_skips"] != sum(t["outcome"] == "skipped" for t in tests):
            raise ValueError("Inconsistent execution/skip counts")
        if row["status"] in ("passed", "flaky"):
            if not row["dependency_lock"] or not contract["junit_paths"] or not contract["executed"] or contract["unexpected_skips"]:
                raise ValueError("Empty/skipped/lockless/JUnit-less pass")
            if any(t["outcome"] != "passed" for t in tests):
                raise ValueError("Failing tests cannot pass")
            if not row["runtime"]["version"].startswith(selected["runtime"] + "."):
                raise ValueError("Execution runtime differs from plan")
            resolved = {canonicalize_name(r["name"]): r for r in row["resolved"]}
            if len(resolved) != len(row["resolved"]):
                raise ValueError("Duplicate resolved dependency")
            for requested in selected["requested"]:
                actual = resolved.get(canonicalize_name(requested["name"]))
                if not actual or actual["role"] != requested["role"] or not requested["selected_version"] or Version(actual["version"]) != Version(requested["selected_version"]):
                    raise ValueError("requested_resolved_mismatch: cannot report latest pass")
            if row["status"] == "passed" and row["failure"] is not None:
                raise ValueError("False pass with failure")
    if len(rows) == 2 and rows[0]["status"] != "passed" and rows[1]["status"] == "passed":
        raise ValueError("Fail-then-pass must be labelled flaky")
    return rows


def collect(plan_dir, artifacts, output, schema):
    raw = (plan_dir / "compat-plan.json").read_bytes()
    plan = validate_plan(json.loads(raw))
    registry_bytes = (plan_dir / "registry.json").read_bytes()
    if sha256(registry_bytes) != plan["registry_sha256"]:
        raise ValueError("Registry snapshot identity mismatch")
    registry = json.loads(registry_bytes)
    targets = {t["id"]: t for t in registry["integrations"]}
    output.mkdir(parents=True, exist_ok=False)
    (output / "compat-plan.json").write_bytes(raw)
    shutil.copyfile(plan_dir / "registry.json", output / "registry.json")
    rows = []
    blocked = []
    for selected in plan["cases"]:
        case_id = selected["case_id"]
        if not case_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for ch in case_id):
            raise ValueError("Unsafe case ID")
        folder = safe_path(artifacts, "compatibility-case-" + case_id)
        try:
            if not folder.is_dir():
                raise ValueError("Missing planned case upload")
            check_tree(folder)
            data = json.loads((folder / "results.json").read_text())
            cases = validate_case(data, plan, raw, selected, schema)
            for row in cases:
                target = targets[selected["integration_id"]]
                if row["qualification"] != target["qualification"]["status"]:
                    raise ValueError("Artifact cannot promote qualification")
                if row["status"] in ("passed", "flaky") and row["contract"]["executed"] < target["contract_test"]["minimum_executed"]:
                    raise ValueError("Insufficient contract execution")
                references = list(row["contract"]["junit_paths"])
                observed = []
                for relative in references:
                    observed.extend(junit(safe_path(folder, relative))[0])
                reported = [{**t, "exception_id": None} for t in row["contract"]["tests"]]
                if observed != reported:
                    raise ValueError("Reported outcomes do not match original JUnit")
                paths = set(target["contract_test"]["paths"]) if target["contract_test"] else set()
                if any(t["test_id"].split("::", 1)[0] not in paths for t in observed):
                    raise ValueError("JUnit test ID is not repository-relative to the planned contract")
                lock = row["dependency_lock"]
                if lock:
                    path = safe_path(folder, lock["path"])
                    if not path.is_file() or sha256(path.read_bytes()) != lock["sha256"]:
                        raise ValueError("Dependency lock absent or digest mismatch")
                    references.append(lock["path"])
                for relative in references:
                    source = safe_path(folder, relative)
                    destination = safe_path(output, relative)
                    if not source.is_file() or destination.exists():
                        # Same lock for preserved attempts is allowed only when byte-identical.
                        if not source.is_file() or not destination.exists() or source.read_bytes() != destination.read_bytes():
                            raise ValueError("Missing/duplicate evidence reference")
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source, destination)
            # Preserve complete pip reports/requirements and logs, not only freeze.
            for path in folder.rglob("*"):
                if path.is_file() and path.name != "results.json":
                    destination = safe_path(output, "evidence/" + case_id + "/" + path.relative_to(folder).as_posix())
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, destination)
            rows.extend(cases)
            latest = max(cases, key=lambda c: c["attempt"])
            if latest["status"] != "passed" and not selected["non_blocking"]:
                blocked.append(case_id)
        except (ValueError, KeyError, OSError, ET.ParseError, jsonschema.ValidationError) as error:
            row = empty_case(selected, plan, targets[selected["integration_id"]])
            row["failure"] = failure("automation", str(error))
            row["completed_at"] = now()
            rows.append(row)
            if not selected["non_blocking"]:
                blocked.append(case_id)
    expected = {"compatibility-case-" + c["case_id"] for c in plan["cases"]}
    unexpected = [p.name for p in artifacts.iterdir() if p.name not in expected] if artifacts.exists() else []
    if unexpected:
        raise ValueError("Unplanned/duplicate artifact directories: " + ", ".join(sorted(unexpected)))
    header = run_header(plan, raw)
    result = {"schema_version": 1, "run": header, "cases": rows}
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(result)
    write_json(output / "results.json", result)
    return result, blocked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-dir", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    schema = json.loads(Path(__file__).with_name("results.schema.json").read_text())
    _, bad = collect(args.plan_dir.resolve(), args.artifacts.resolve(), args.out.resolve(), schema)
    print(json.dumps({"blocking_cases": bad}))
    return int(bool(bad))


if __name__ == "__main__":
    sys.exit(main())
