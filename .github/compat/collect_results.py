#!/usr/bin/env python3
"""Fail-closed reconciliation of immutable attempt artifacts and Actions jobs.

Only jobs that were not rerun may reuse an earlier pass. Registry plan, complete
installed environment, source contract and original JUnit are separate bindings.
Artifacts are data; no artifact-supplied command or Python code is executed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import jsonschema
from packaging.utils import canonicalize_name
from packaging.version import Version

from plan import safe_path, sha256, validate_plan, write_json, semantic_validator
from run_case import empty_case, failure, junit, now, run_header, contract_command

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
    rows = sorted(data["cases"], key=lambda c: c["attempt"])
    if not rows or len(rows) > 2 or any(c["case_id"] != selected["case_id"] for c in rows):
        raise ValueError("Expected one case (at most two preserved attempts)")
    if [c["attempt"] for c in rows] != list(range(1, len(rows) + 1)):
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
        if row["status"] in ("passed", "flaky", "quarantined"):
            if not selected["resolution"] or selected["problems"]:
                raise ValueError("Pass without successful frozen resolution")
            if not row["dependency_lock"] or not contract["junit_paths"] or not contract["executed"] or contract["unexpected_skips"]:
                raise ValueError("Empty/skipped/lockless/JUnit-less pass")
            if row["status"] != "quarantined" and any(t["outcome"] != "passed" for t in tests):
                raise ValueError("Failing tests cannot pass")
            identity = selected["runtime_identity"]
            if any(row["runtime"][k] != identity[k] for k in ("version", "implementation", "os", "arch")):
                raise ValueError("Execution runtime differs from frozen patch/platform identity")
            resolved = {canonicalize_name(r["name"]): r for r in row["resolved"]}
            if len(resolved) != len(row["resolved"]):
                raise ValueError("Duplicate resolved dependency")
            for requested in selected["requested"]:
                actual = resolved.get(canonicalize_name(requested["name"]))
                if not actual or actual["role"] != requested["role"] or not requested["selected_version"] or Version(actual["version"]) != Version(requested["selected_version"]):
                    raise ValueError("requested_resolved_mismatch: cannot report latest pass")
            frozen = {canonicalize_name(r["name"]): r for r in selected["resolution"]["resolved"]}
            if resolved != frozen:
                raise ValueError("Resolved artifact identities differ from frozen plan")
            if row["status"] == "passed" and row["failure"] is not None:
                raise ValueError("False pass with failure")
    # Inspect chronology, never upload or array ordering. A failed/blocked attempt
    # cannot be erased by a successful retry or re-labelled as latest green.
    if len(rows) == 2:
        first, second = rows
        if first["status"] != "passed" and second["status"] == "passed":
            raise ValueError("Fail-then-pass must be labelled flaky")
        if first["resolved"] and second["resolved"] and first["resolved"] != second["resolved"]:
            raise ValueError("Retry changed frozen resolved tuple")
        if first["dependency_lock"] and second["dependency_lock"] and first["dependency_lock"] != second["dependency_lock"]:
            raise ValueError("Retry changed complete dependency lock")
    return rows


def frozen_environment(plan_dir, folder, selected, row):
    resolution = selected["resolution"]
    if not resolution or selected["problems"]:
        raise ValueError("Pass without successful resolution")
    for key, digest in (("requirements", "requirements_sha256"), ("report", "report_sha256")):
        content = safe_path(plan_dir, resolution[key]).read_bytes()
        copied = safe_path(folder, "locks/" + Path(resolution[key]).name).read_bytes()
        if sha256(content) != resolution[digest] or copied != content:
            raise ValueError("Frozen report/requirements digest mismatch")
    report = json.loads(safe_path(plan_dir, resolution["report"]).read_text())
    if report.get("version") != "1" or not report.get("install"):
        raise ValueError("Missing successful pip resolution report")
    expected, locks, artifacts = {}, [], {}
    for item in report["install"]:
        name = canonicalize_name(item["metadata"]["name"])
        version = item["metadata"]["version"]
        digest = item["download_info"]["archive_info"]["hashes"]["sha256"]
        if name in expected or not re.fullmatch("[0-9a-f]{64}", digest):
            raise ValueError("Invalid/duplicate frozen distribution")
        expected[name] = version
        artifacts[name] = digest
        locks.append(f"{name}=={version} --hash=sha256:{digest}")
    if safe_path(plan_dir, resolution["requirements"]).read_text() != "\n".join(sorted(locks)) + "\n":
        raise ValueError("Frozen requirements/report disagree")
    for resolved in row["resolved"]:
        name = canonicalize_name(resolved["name"])
        if expected.get(name) != resolved["version"] or artifacts.get(name) != resolved.get("artifact_sha256"):
            raise ValueError("Resolved artifact identity not in frozen report")
    lock = row["dependency_lock"]
    if lock["format"] != "pip-freeze":
        raise ValueError("Expected complete pip-freeze inventory")
    content = safe_path(folder, lock["path"]).read_bytes()
    if sha256(content) != lock["sha256"]:
        raise ValueError("Dependency lock digest mismatch")
    actual = {}
    for line in content.decode().splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9._-]+)==([^\s]+)", line)
        if not match or canonicalize_name(match[1]) in actual:
            raise ValueError("Noncanonical/duplicate installed freeze entry")
        actual[canonicalize_name(match[1])] = match[2]
    if actual != expected:
        raise ValueError("Complete installed distribution set differs from frozen resolution")


def current_job(jobs, case_id):
    matches = [j for j in jobs if j.get("name") == "Canary (" + case_id + ")"]
    if not matches:
        raise ValueError("Planned case absent from Actions job attempts")
    latest = max(matches, key=lambda j: (int(j["run_attempt"]), int(j["id"])))
    if latest.get("status") != "completed":
        raise ValueError("Current case job has not completed")
    return latest


def collect(plan_dir, artifacts, output, schema, *, source_root=None, jobs=None):
    raw = (plan_dir / "compat-plan.json").read_bytes()
    plan = validate_plan(json.loads(raw))
    registry_bytes = (plan_dir / "registry.json").read_bytes()
    if sha256(registry_bytes) != plan["registry_sha256"]:
        raise ValueError("Registry snapshot identity mismatch")
    registry = json.loads(registry_bytes)
    targets = {t["id"]: t for t in registry["integrations"]}
    if os.environ.get("GITHUB_RUN_ID") and jobs is None:
        raise ValueError("Actions job-attempt reconciliation is mandatory in CI")
    output.mkdir(parents=True, exist_ok=False)
    (output / "compat-plan.json").write_bytes(raw)
    shutil.copyfile(plan_dir / "registry.json", output / "registry.json")
    rows, blocked, expected_dirs = [], [], set()
    for selected in plan["cases"]:
        case_id = selected["case_id"]
        target = targets[selected["integration_id"]]
        cases, folders = [], {}
        wanted_attempt = 1
        try:
            job = current_job(jobs, case_id) if jobs is not None else None
            wanted_attempt = int(job["run_attempt"]) if job else int(os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
            if wanted_attempt not in (1, 2):
                raise ValueError("Only one immutable retry is permitted")
            for attempt in range(1, wanted_attempt + 1):
                name = f"compatibility-case-{case_id}-attempt-{attempt}"
                expected_dirs.add(name)
                folder = safe_path(artifacts, name)
                if not folder.is_dir() or not (folder / "results.json").is_file():
                    row = empty_case(selected, plan, target)
                    row["attempt"] = attempt
                    row["runtime"] = {k: selected["runtime_identity"][k] for k in ("version", "implementation", "os", "arch")}
                    row["failure"] = failure("automation", f"Missing planned attempt {attempt} upload; current job evidence is blocked")
                    cases.append(row)
                    continue
                check_tree(folder)
                data = json.loads((folder / "results.json").read_text())
                # Each immutable artifact contains exactly its own attempt.
                jsonschema.Draft202012Validator(schema).validate(data)
                if len(data["cases"]) != 1 or data["cases"][0]["attempt"] != attempt or data["run"]["run_attempt"] != attempt:
                    raise ValueError("Artifact attempt/name mismatch")
                for field in ("plan_sha256", "source_sha", "workflow_sha", "planned_case_ids", "self_test", "cooldown_cutoff", "run_id"):
                    if data["run"][field] != run_header(plan, raw)[field]:
                        raise ValueError("Artifact run/plan identity mismatch: " + field)
                cases.extend(data["cases"])
                folders[attempt] = folder
            data = {"schema_version": 1, "run": run_header(plan, raw), "cases": cases}
            cases = validate_case(data, plan, raw, selected, schema)
            for row in cases:
                folder = folders.get(row["attempt"])
                if folder is None:
                    continue
                if row["qualification"] != target["qualification"]["status"]:
                    raise ValueError("Artifact cannot promote qualification")
                passing = row["status"] in ("passed", "flaky", "quarantined")
                if passing:
                    if source_root is None:
                        raise ValueError("Trusted source checkout required for semantic contract validation")
                    semantic_validator(source_root).validate_contract(row, target, source_root)
                    if len(row["contract"]["junit_paths"]) != 1 or row["contract"]["command"] != contract_command(target["contract_test"], row["contract"]["junit_paths"][0]):
                        raise ValueError("Exact trusted contract command mismatch")
                    frozen_environment(plan_dir, folder, selected, row)
                references = list(row["contract"]["junit_paths"])
                validator = semantic_validator(source_root) if source_root is not None else None
                parser = validator if validator is not None and hasattr(validator, "junit_tests") else None
                observed = [t for relative in references for t in junit(safe_path(folder, relative), parser)[0]]
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
                    source, destination = safe_path(folder, relative), safe_path(output, relative)
                    if destination.exists():
                        if source.read_bytes() != destination.read_bytes():
                            raise ValueError("Conflicting immutable evidence reference")
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source, destination)
                for path in folder.rglob("*"):
                    if path.is_file() and path.name != "results.json":
                        destination = safe_path(output, f"evidence/{case_id}/attempt-{row['attempt']}/" + path.relative_to(folder).as_posix())
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, destination)
            latest = cases[-1]
            if job and latest["status"] == "passed" and job.get("conclusion") != "success":
                # Setup/checkout/upload failure must not be masked by test output.
                latest = empty_case(selected, plan, target)
                latest["attempt"] = wanted_attempt
                latest["failure"] = failure("automation", "Current job did not succeed: " + str(job.get("conclusion")))
                cases[-1] = latest
            rows.extend(cases)
            if latest["status"] != "passed" and not selected["non_blocking"]:
                blocked.append(case_id)
        except (ValueError, KeyError, OSError, ET.ParseError, jsonschema.ValidationError) as error:
            row = empty_case(selected, plan, target)
            row["attempt"] = wanted_attempt
            row["failure"] = failure("automation", str(error))
            row["completed_at"] = now()
            rows.append(row)
            if not selected["non_blocking"]:
                blocked.append(case_id)
    unexpected = [p.name for p in artifacts.iterdir() if p.name not in expected_dirs] if artifacts.exists() else []
    if unexpected:
        raise ValueError("Unplanned/duplicate artifact directories: " + ", ".join(sorted(unexpected)))
    result = {"schema_version": 1, "run": run_header(plan, raw), "cases": rows}
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(result)
    write_json(output / "results.json", result)
    return result, blocked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-dir", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--jobs", type=Path, required=True, help="Trusted REST jobs?filter=all snapshot, all pages")
    args = parser.parse_args()
    plan = json.loads((args.plan_dir / "compat-plan.json").read_text())
    source_sha = subprocess.check_output(["git", "-C", str(args.source_root), "rev-parse", "HEAD"], text=True).strip()
    if source_sha != plan["source_sha"]:
        raise ValueError("Trusted contract checkout does not match frozen source SHA")
    schema = json.loads(Path(__file__).with_name("results.schema.json").read_text())
    pages = json.loads(args.jobs.read_text())
    jobs = [j for page in pages for j in page["jobs"]]
    _, bad = collect(args.plan_dir.resolve(), args.artifacts.resolve(), args.out.resolve(), schema,
                     source_root=args.source_root.resolve(), jobs=jobs)
    shutil.copyfile(args.jobs, args.out / "actions-jobs.json")
    print(json.dumps({"blocking_cases": bad}))
    return int(bool(bad))


if __name__ == "__main__":
    sys.exit(main())
