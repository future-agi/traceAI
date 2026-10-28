#!/usr/bin/env python3
"""Fail-closed reconciliation of immutable attempt artifacts and Actions jobs.

The source registry, frozen plan, installed environment, invocation statuses and
original JUnit are independent bindings. Artifacts are data, never executable argv.
"""
from __future__ import annotations

import argparse
import copy
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
from run_case import empty_case, failure, junit, now, run_header, contract_command, invocation_path

MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024


def check_tree(root):
    total = count = 0
    if root.is_symlink():
        raise ValueError("Symlinks are forbidden in evidence")
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


def validate_case(data, plan, raw, selected, schema, *, independent=False):
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
    if independent:
        if len(rows) != 1 or rows[0]["attempt"] not in (1, 2):
            raise ValueError("Expected one independent attempt")
    elif [c["attempt"] for c in rows] != list(range(1, len(rows) + 1)):
        raise ValueError("Duplicate/missing attempt")
    for row in rows:
        if row["source_sha"] != plan["source_sha"] or row["integration_id"] != selected["integration_id"]:
            raise ValueError("Case source/integration mismatch")
        if row["requested"] != selected["requested"] or row["selector"] != selected["selector"] or row["mode"] != selected["mode"]:
            raise ValueError("Requested plan selection changed")
        identity = selected["runtime_identity"]
        if any(row["runtime"][k] != identity[k] for k in ("version", "implementation", "os", "arch")):
            raise ValueError("Execution runtime differs from frozen patch/platform identity")
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
    # GitHub clones successful, NOT-rerun jobs into the new attempt with new IDs
    # and run_attempt, but the old execution timestamps/steps/runner.
    trace = ("started_at", "completed_at", "steps", "runner_id", "head_sha")
    if (latest.get("conclusion") == "success" and all(latest.get(k) for k in trace)
            and latest.get("created_at") and latest["completed_at"] < latest["created_at"]):
        originals = [j for j in matches if int(j["run_attempt"]) < int(latest["run_attempt"])
                     and j.get("status") == "completed" and j.get("conclusion") == "success"
                     and j.get("created_at") and j["created_at"] <= j.get("started_at", "")
                     and all(j.get(k) == latest[k] for k in trace)]
        if originals:
            return max(originals, key=lambda j: int(j["run_attempt"]))
    return latest


def blocked_attempt(selected, plan, target, attempt, message):
    row = empty_case(selected, plan, target)
    row["attempt"] = attempt
    row["runtime"] = {k: selected["runtime_identity"][k] for k in ("version", "implementation", "os", "arch")}
    row["failure"] = failure("automation", message)
    row["completed_at"] = now()
    return row


def invocation_references(folder, row, plan, raw, validator):
    """Retain producer exit evidence separately from dependency-written JUnit."""
    relative = invocation_path(row["case_id"], row["attempt"])
    status = json.loads(safe_path(folder, relative).read_text())
    for field, expected in (("schema_version", 1), ("case_id", row["case_id"]), ("attempt", row["attempt"]),
                            ("source_sha", plan["source_sha"]), ("plan_sha256", sha256(raw))):
        if status.get(field) != expected:
            raise ValueError("Invocation identity mismatch: " + field)
    references = [relative]
    for kind in ("collection", "contract"):
        invocation = status.get(kind)
        if not isinstance(invocation, dict):
            raise ValueError("Missing " + kind + " invocation")
        code = invocation.get("exit_code")
        if type(code) is not int or invocation.get("timed_out") is not False:
            raise ValueError("Incomplete/timed-out " + kind + " invocation")
        if (kind == "collection" and code != 0) or (kind == "contract" and code not in (0, 1)):
            raise ValueError("Collection/runner failure cannot attest compatibility")
        path = invocation["evidence_path"]
        content = safe_path(folder, path).read_bytes()
        if sha256(content) != invocation["evidence_sha256"]:
            raise ValueError("Invocation evidence digest mismatch")
        sidecar = path + ".exit-code"
        if safe_path(folder, sidecar).read_text() != str(code) + "\n":
            raise ValueError("Invocation/exit-code sidecar mismatch")
        references.extend([path, sidecar])
        if kind == "collection":
            collection = json.loads(content)
            if collection["deselected"] or collection.get("errors") or collection["nodes"] != sorted(t["test_id"] for t in row["contract"]["tests"]):
                raise ValueError("Partial/deselected execution differs from full collected identities")
        else:
            if row["contract"]["junit_paths"] != [path]:
                raise ValueError("Invocation/JUnit reference mismatch")
            validator.invocation_tests([safe_path(folder, path)], [code])
    return references


def canonical_attempt(data, row, selected, target, plan, raw, registry, folder, source_root, validator):
    validator.reconcile_plan(data, plan, raw)
    if row["qualification"] != target["qualification"]["status"] or row["language"] != target["language"]:
        raise ValueError("Artifact cannot promote qualification/change language")
    passing = row["status"] in ("passed", "flaky", "quarantined")
    if not passing:
        return []
    validator.validate_contract(row, target, source_root)
    if len(row["contract"]["junit_paths"]) != 1 or row["contract"]["command"] != contract_command(target["contract_test"], row["contract"]["junit_paths"][0]):
        raise ValueError("Exact trusted contract command mismatch")
    verdict = validator.judge_tests(row["contract"]["tests"], target, row["runtime"], row["resolved"])
    if verdict != ("passed" if row["status"] == "flaky" else row["status"]):
        raise ValueError("False pass/quarantine status")
    references = invocation_references(folder, row, plan, raw, validator)
    # Reuse PR1's entire source-bound discovery/support/outcome semantics. A local
    # one-attempt validation envelope isolates this attempt from malformed siblings;
    # it is never published and never changes the immutable plan or reported rows.
    probe = copy.deepcopy(row)
    probe["attempt"] = 1
    if probe["status"] == "flaky":
        probe["status"] = "passed"
    peers = [probe if c["case_id"] == selected["case_id"] else
             blocked_attempt(c, plan, next(t for t in registry["integrations"] if t["id"] == c["integration_id"]), 1,
                             "Independent attempt validation placeholder") for c in plan["cases"]]
    validator.validate_results({"schema_version": 1, "run": data["run"], "cases": peers}, registry,
                               expected_source_sha=plan["source_sha"], source_root=source_root, artifact_root=folder,
                               expected_plan=plan, plan_bytes=raw)
    return references


def copy_reference(folder, output, relative):
    source, destination = safe_path(folder, relative), safe_path(output, relative)
    if destination.exists():
        if source.read_bytes() != destination.read_bytes():
            raise ValueError("Conflicting immutable evidence reference")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def collect(plan_dir, artifacts, output, schema, *, source_root=None, jobs=None):
    check_tree(plan_dir)
    raw = (plan_dir / "compat-plan.json").read_bytes()
    plan = validate_plan(json.loads(raw))
    registry_bytes = (plan_dir / "registry.json").read_bytes()
    if sha256(registry_bytes) != plan["registry_sha256"]:
        raise ValueError("Registry snapshot identity mismatch")
    if source_root is None:
        raise ValueError("Trusted source checkout required")
    # Bind COMPLETE bytes before taking any target/contract/exception from them.
    if registry_bytes != safe_path(source_root, ".github/compat/registry.json").read_bytes():
        raise ValueError("Registry snapshot differs from immutable source registry")
    validator = semantic_validator(source_root)
    registry = validator.validate_registry(json.loads(registry_bytes), source_root)
    targets = {t["id"]: t for t in registry["integrations"]}
    if os.environ.get("GITHUB_RUN_ID") and jobs is None:
        raise ValueError("Actions job-attempt reconciliation is mandatory in CI")
    output.mkdir(parents=True, exist_ok=False)
    (output / "compat-plan.json").write_bytes(raw)
    shutil.copyfile(plan_dir / "registry.json", output / "registry.json")
    if (plan_dir / "discovery.json").is_file():
        shutil.copyfile(plan_dir / "discovery.json", output / "discovery.json")
    # Enumerate every permitted directory BEFORE parsing a single attempt. A bad
    # first artifact must not prevent recognition/retention of a healthy retry.
    metadata, expected_dirs = {}, set()
    for selected in plan["cases"]:
        cid = selected["case_id"]
        try:
            job = current_job(jobs, cid) if jobs is not None else None
            wanted = int(job["run_attempt"]) if job else int(os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
            if wanted not in (1, 2):
                raise ValueError("Only one immutable retry is permitted")
            metadata[cid] = (job, wanted, None)
        except (ValueError, KeyError, TypeError) as error:
            wanted = 2 if os.environ.get("GITHUB_RUN_ATTEMPT") == "2" else 1
            metadata[cid] = (None, wanted, str(error))
        expected_dirs.update(f"compatibility-case-{cid}-attempt-{a}" for a in range(1, wanted + 1))
    rows, blocked = [], []
    for selected in plan["cases"]:
        cid = selected["case_id"]
        target = targets[selected["integration_id"]]
        job, wanted, job_error = metadata[cid]
        cases = []
        for attempt in range(1, wanted + 1):
            folder = safe_path(artifacts, f"compatibility-case-{cid}-attempt-{attempt}")
            try:
                if not folder.is_dir() or not (folder / "results.json").is_file():
                    raise ValueError(f"Missing planned attempt {attempt} upload; current job evidence is blocked")
                check_tree(folder)
                # Keep original raw history even if its claims are malformed.
                for path in folder.rglob("*"):
                    if path.is_file():
                        destination = safe_path(output, f"evidence/{cid}/attempt-{attempt}/" + path.relative_to(folder).as_posix())
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, destination)
                data = json.loads((folder / "results.json").read_text())
                row = validate_case(data, plan, raw, selected, schema, independent=True)[0]
                if row["attempt"] != attempt or data["run"]["run_attempt"] != attempt:
                    raise ValueError("Artifact attempt/name mismatch")
                for field in ("plan_sha256", "source_sha", "workflow_sha", "planned_case_ids", "self_test", "cooldown_cutoff", "run_id"):
                    if data["run"][field] != run_header(plan, raw)[field]:
                        raise ValueError("Artifact run/plan identity mismatch: " + field)
                references = canonical_attempt(data, row, selected, target, plan, raw, registry, folder, source_root, validator)
                if row["status"] in ("passed", "flaky", "quarantined"):
                    frozen_environment(plan_dir, folder, selected, row)
                observed = [t for relative in row["contract"]["junit_paths"] for t in junit(safe_path(folder, relative), validator)[0]]
                if observed != [{**t, "exception_id": None} for t in row["contract"]["tests"]]:
                    raise ValueError("Reported outcomes do not match original JUnit")
                paths = set(target["contract_test"]["paths"]) if target["contract_test"] else set()
                if any(t["test_id"].split("::", 1)[0] not in paths for t in observed):
                    raise ValueError("JUnit test ID is not repository-relative to the planned contract")
                references.extend(row["contract"]["junit_paths"])
                references.extend(p + ".exit-code" for p in row["contract"]["junit_paths"])
                lock = row["dependency_lock"]
                if lock:
                    if sha256(safe_path(folder, lock["path"]).read_bytes()) != lock["sha256"]:
                        raise ValueError("Dependency lock absent or digest mismatch")
                    references.append(lock["path"])
                # Failure evidence also retains invocation metadata at the root.
                status_path = invocation_path(cid, attempt)
                if safe_path(folder, status_path).is_file():
                    references.append(status_path)
                    status = json.loads(safe_path(folder, status_path).read_text())
                    for kind in ("collection", "contract"):
                        if isinstance(status.get(kind), dict) and status[kind].get("evidence_path"):
                            references.append(status[kind]["evidence_path"])
                            if safe_path(folder, status[kind]["evidence_path"] + ".exit-code").is_file():
                                references.append(status[kind]["evidence_path"] + ".exit-code")
                for relative in sorted(set(references)):
                    copy_reference(folder, output, relative)
                if attempt == wanted and job_error:
                    raise ValueError(job_error)
                if attempt == wanted and job and row["status"] == "passed" and job.get("conclusion") != "success":
                    raise ValueError("Current job did not succeed: " + str(job.get("conclusion")))
                if cases and cases[0]["status"] != "passed" and row["status"] == "passed":
                    # A malformed/missing original is a failed history, not green.
                    row = copy.deepcopy(row)
                    row["status"] = "flaky"
                    row["notes"].append("Earlier attempt was not a validated pass.")
                cases.append(row)
            except (ValueError, KeyError, TypeError, OSError, ET.ParseError, jsonschema.ValidationError) as error:
                cases.append(blocked_attempt(selected, plan, target, attempt, str(error)))
        rows.extend(cases)
        if cases[-1]["status"] != "passed" and not selected["non_blocking"]:
            blocked.append(cid)
    result = {"schema_version": 1, "run": run_header(plan, raw), "cases": rows}
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(result)
    write_json(output / "results.json", result)
    unexpected = [p.name for p in artifacts.iterdir() if p.name not in expected_dirs] if artifacts.exists() else []
    if unexpected:
        raise ValueError("Unplanned/duplicate artifact directories: " + ", ".join(sorted(unexpected)))
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
