#!/usr/bin/env python3
"""Execute ONE frozen plan cell, offline in a clean installed-package consumer.

No downloaded artifact commands are executed: argv comes only from the checked-out
source registry, whose bytes and immutable source SHA must match the original plan.
All failure paths emit a schema-v1 row, including missing suite/JUnit and install.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from discover import UTC, iso
from plan import clean_env, safe_path, sha256, validate_plan, write_json, semantic_validator, runtime_identity


COLLECTION_CODE = r'''import json,pathlib,pytest,re,sys
class Collection:
 def __init__(self): self.deselected=[]; self.errors=[]
 def pytest_deselected(self,items): self.deselected.extend(i.nodeid for i in items)
 def pytest_collectreport(self,report):
  if report.failed and len(self.errors)<64:
   text=str(report.longrepr)
   lines=[line.strip() for line in text.splitlines() if re.match(r'^(?:E\s+)?[\w.]+(?:Error|Exception|Failure):',line.strip())]
   self.errors.append(dict(test_id=report.nodeid,diagnostic=('\n'.join(lines) or text)[-16384:]))
 def write(self,session):
  pathlib.Path(sys.argv[1]).write_text(json.dumps(dict(nodes=sorted(i.nodeid for i in session.items),deselected=self.deselected,errors=self.errors)))
 def pytest_collection_finish(self,session): self.write(session)
 @pytest.hookimpl(trylast=True)
 def pytest_sessionfinish(self,session,exitstatus):
  self.write(session)
  if exitstatus == 0 and not session.testsfailed and session.items and not self.deselected and not self.errors: session.exitstatus=0
raise SystemExit(pytest.main(sys.argv[2:],plugins=[Collection()]))
'''


def now():
    return iso(dt.datetime.now(UTC))


def normalize(text):
    text = re.sub(r"https?://[^\s]+", "<url>", text)
    text = re.sub(r"(?:/(?:home|tmp|private|Users|Volumes|opt|usr)/)[^\s'\"]+", "<path>", text)
    text = re.sub(r"\b0x[0-9a-fA-F]+\b", "<address>", text)
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}[T ][\d:.+Z-]+", "<time>", text)
    text = re.sub(r"(?i)(api[_-]?key|token|authorization|password|secret)(\s*[=:]\s*)[^\s,;]+", r"\1\2<redacted>", text)
    return " ".join(text.split())


def failure(kind, summary, ids=()):
    summary = normalize(summary) or kind
    test_ids = sorted(set(ids))
    # Hash the complete normalized behavior; truncate only the human-facing text.
    # Otherwise large multi-test failures can collapse distinct later assertions.
    signature = sha256(json.dumps([kind, test_ids, summary], separators=(",", ":")).encode())
    return {"kind": kind, "signature": signature, "summary": summary[:2000], "test_ids": test_ids}


def aggregate_failure(details, ids, fallback="pytest failed"):
    result = failure("contract", " | ".join(d["summary"] for d in details) or fallback, ids)
    # Never hash the truncated display summaries. Per-test signatures retain the
    # complete normalized diagnostics and exact identity, in deterministic order.
    result["signature"] = sha256(json.dumps(["contract", sorted(set(ids)),
        sorted(d["signature"] for d in details)], separators=(",", ":")).encode())
    return result


def contract_command(registered, junit_path):
    return ["python", "-I", *registered["command"][1:], "--import-mode=importlib", "--rootdir=.",
            "-p", "pytest_asyncio.plugin", "-o", "junit_family=legacy", "--junitxml=" + junit_path]


def sandbox(root, output, toolchain, consumer, command):
    return ["bash", str(root / "block_network.sh"), str(root), str(output), str(toolchain), str(consumer), *command]


def run_header(plan, plan_bytes):
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    repository = os.environ.get("GITHUB_REPOSITORY", "future-agi/traceAI")
    return {"repository": repository, "run_id": run_id, "run_attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")),
            "workflow": "Compatibility canary", "event": os.environ.get("GITHUB_EVENT_NAME", "self_test"),
            "url": f"https://github.com/{repository}/actions/runs/{run_id}",
            "source_sha": plan["source_sha"], "workflow_sha": plan["workflow_sha"],
            "plan_sha256": sha256(plan_bytes), "planned_case_ids": [c["case_id"] for c in plan["cases"]],
            "started_at": plan["generated_at"], "completed_at": now(), "cooldown_hours": 72,
            "cooldown_cutoff": plan["cooldown_cutoff"], "self_test": plan["self_test"]}


def empty_case(case, plan, target):
    return {"case_id": case["case_id"], "integration_id": case["integration_id"], "language": target["language"],
            "source_sha": plan["source_sha"], "mode": case["mode"], "selector": case["selector"],
            "qualification": target["qualification"]["status"],
            "runtime": {k: case["runtime_identity"][k] for k in ("implementation", "version", "os", "arch")},
            "requested": copy.deepcopy(case["requested"]), "resolved": [], "discovery": copy.deepcopy(case["discovery"]), "dependency_lock": None,
            "contract": {"suite_sha256": sha256(b""), "command": ["python", "-m", "pytest"], "scenarios": [],
                         "junit_paths": [], "tests": [], "collected": 0, "executed": 0, "unexpected_skips": 0},
            "status": "blocked", "failure": None, "started_at": now(), "completed_at": now(), "attempt": 1,
            "reproduction": ["python", ".github/compat/run_case.py", "--root", "SOURCE", "--plan-dir", "compatibility-plan",
                             "--case-id", case["case_id"], "--out", "reproduction"],
            "notes": ["Pending-pilot evidence is not a qualification claim."] if target["qualification"]["status"] != "qualified" else []}


def suite_files(root, contract):
    """Snapshot test/helper/fixture directories and ancestor conftest/config files.

    Never copy source modules into the consumer. The digest covers every copied
    file, even helpers that are not individually named in contract.paths.
    """
    files = set()
    for relative in contract["paths"]:
        path = safe_path(root, relative)
        if not path.is_file():
            raise ValueError(f"Missing real-SDK contract: {relative}")
        directory = path.parent
        for candidate in directory.rglob("*"):
            if candidate.is_file() and "__pycache__" not in candidate.parts and not candidate.name.endswith(".pyc"):
                safe_path(root, candidate.relative_to(root).as_posix())
                files.add(candidate.relative_to(root).as_posix())
        for parent in [directory, *directory.parents]:
            if not parent.is_relative_to(root):
                break
            for name in ("conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "__init__.py"):
                candidate = parent / name
                if candidate.is_file():
                    files.add(candidate.relative_to(root).as_posix())
    return sorted(files)


def copy_suite(root, consumer, files):
    digest = hashlib.sha256()
    for relative in files:
        content = safe_path(root, relative).read_bytes()
        digest.update(relative.encode() + b"\0" + content + b"\0")
        destination = safe_path(consumer, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    return digest.hexdigest()


def junit(path, validator=None):
    if path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("Oversized JUnit")
    raw = path.read_bytes()
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise ValueError("Unsafe JUnit declarations")
    root = ET.fromstring(raw)
    tests = []
    details = []
    for node in root.iter("testcase"):
        file = node.get("file", "")
        name = node.get("name", "unknown")
        classname = node.get("classname", "")
        if file:
            if Path(file).is_absolute() or ".." in Path(file).parts or "\\" in file:
                raise ValueError("Noncanonical JUnit file path")
            stem = file.removesuffix(".py").replace("/", ".")
            cls = classname[len(stem) + 1:] if classname.startswith(stem + ".") else ""
            test_id = file + "::" + (cls + "::" if cls else "") + name
        else:
            test_id = classname.replace(".", "/") + ".py::" + name
        outcome = "passed"
        sig = None
        for tag in ("failure", "error", "skipped"):
            child = node.find(tag)
            if child is not None:
                outcome = tag
                if tag != "skipped":
                    lines = (child.text or child.get("message", tag)).splitlines()
                    assertion = next((x.strip() for x in lines if x.startswith("E ")), child.get("message", tag))
                    detail = failure("collection" if "collection" in child.get("message", "").lower() else "contract", assertion, [test_id])
                    sig = detail["signature"]
                    details.append(detail)
                break
        tests.append({"test_id": test_id, "outcome": outcome, "duration_seconds": max(0.0, float(node.get("time", "0"))),
                      "failure_signature": sig, "exception_id": None})
    if len({t["test_id"] for t in tests}) != len(tests):
        raise ValueError("Duplicate JUnit test IDs")
    if validator is not None:
        # PR1 owns canonical diagnostics/identity semantics. In particular, use
        # its complete exception diagnostics rather than a truncated display.
        canonical = validator.junit_tests(path)
        by_id = {t["test_id"]: t for t in canonical}
        for detail in details:
            detail["signature"] = by_id[detail["test_ids"][0]]["failure_signature"]
        tests = canonical
    return tests, details


def apply_exceptions(tests, target, resolved, runtime, validator):
    """PR1 owns expiry, completeness, overlap, outcome/signature and attribution."""
    for exception in target["exceptions"]:
        validator.validate_exception(exception, target)
    verdict = validator.judge_tests(tests, target, runtime, resolved)
    # Only attach attribution AFTER the canonical verdict accepted every node.
    for exception in target["exceptions"]:
        if validator.applicable(exception, runtime, resolved):
            for test in tests:
                if test["test_id"] in exception["test_ids"]:
                    test["exception_id"] = exception["id"]
    return verdict


def invocation_path(case_id, attempt):
    return f"invocations/{case_id}-attempt-{attempt}.json"


def invoke(command, *, cwd, log, evidence, retained, output, timeout=600):
    """The parent records the sandbox process exit; dependencies cannot write it.

    Output/JUnit alone cannot prove that pytest (or the sandbox) exited cleanly.
    Retain even timeout/crash outcomes; never synthesize a successful exit.
    """
    record = dict(exit_code=None, timed_out=False, started_at=now(), completed_at=None,
                  evidence_path=None, evidence_sha256=None)
    try:
        with log.open("w") as stream:
            process = subprocess.run(command, cwd=cwd, env=clean_env(), stdout=stream,
                                     stderr=subprocess.STDOUT, timeout=timeout)
        record["exit_code"] = process.returncode
    except subprocess.TimeoutExpired:
        record["timed_out"] = True
    finally:
        record["completed_at"] = now()
        if evidence.is_file() and not evidence.is_symlink():
            retained.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(evidence, retained)
            record["evidence_path"] = retained.relative_to(output).as_posix()
            record["evidence_sha256"] = sha256(retained.read_bytes())
            # PR1's additive public convention: one immutable parent-written
            # exit-code sidecar per original JUnit (also retained for collection).
            code = record["exit_code"]
            shell_code = 124 if record["timed_out"] else (128 - code if code is not None and code < 0 else code)
            if shell_code is not None:
                Path(str(retained) + ".exit-code").write_text(str(shell_code) + "\n")
    return record


def collection_failure(collection, log):
    """Bounded actual terminal diagnostics, including affected module/node IDs."""
    errors = collection.get("errors", [])
    ids = sorted({e["test_id"] for e in errors if e.get("test_id")})
    diagnostics = [e["diagnostic"] for e in errors if e.get("diagnostic")]
    if not diagnostics:
        # A crash before the plugin can emit structured errors still retains its
        # actual terminal diagnostic, not one generic 'collection failed' key.
        content = log.read_bytes()[-32768:].decode(errors="replace") if log.is_file() else ""
        diagnostics = [line.strip() for line in content.splitlines()
                       if re.match(r"^(?:E\\s+)?[\\w.]+(?:Error|Exception|Failure):", line.strip())]
    return failure("collection", " | ".join(diagnostics) or "Collection produced no terminal diagnostic; see collection.log", ids)


def finish_contract(row, target, root, collection, exit_code, details):
    """Completeness/scenario coverage is independent of pass versus quarantine."""
    contract = target["contract_test"]
    tests = row["contract"]["tests"]
    if sorted(t["test_id"] for t in tests) != collection["nodes"] or collection["deselected"] or collection.get("errors"):
        raise ValueError("Partial/deselected contract execution differs from full collection")
    if row["contract"]["executed"] < contract["minimum_executed"] or row["contract"]["unexpected_skips"]:
        raise ValueError("Empty/insufficient/skipped contract execution")
    validator = semantic_validator(root)
    candidate = {**row, "contract": {**row["contract"], "scenarios": contract["scenarios"]}}
    validator.validate_contract(candidate, target, root)
    row["contract"]["scenarios"] = list(contract["scenarios"])
    bad = [t for t in tests if t["outcome"] in ("failure", "error")]
    if type(exit_code) is not int or exit_code not in (0, 1) or (exit_code == 0) != (not bad):
        raise ValueError(f"Runner/JUnit outcome mismatch (exit {exit_code}); not waivable")
    try:
        row["status"] = apply_exceptions(tests, target, row["resolved"], row["runtime"], validator)
    except ValueError as error:
        row["status"] = "failed"
        row["notes"].append(normalize(str(error))[:2000])
        row["failure"] = aggregate_failure(details, [t["test_id"] for t in bad], str(error))
        return
    if bad:
        row["failure"] = aggregate_failure(details, [t["test_id"] for t in bad])


def execute(root, plan_dir, case, plan, target, output, blocker, attempt=1):
    row = empty_case(case, plan, target)
    row["attempt"] = attempt
    raw = (plan_dir / "compat-plan.json").read_bytes() if (plan_dir / "compat-plan.json").is_file() else b""
    invocations = dict(schema_version=1, case_id=case["case_id"], attempt=attempt,
                       source_sha=plan["source_sha"], plan_sha256=sha256(raw), collection=None, contract=None)
    try:
        if case["problems"] or not case["resolution"]:
            raise ValueError("Resolution blocked: " + "; ".join(case["problems"] or ["No complete frozen resolution"]))
        if runtime_identity(sys.executable) != case["runtime_identity"]:
            raise ValueError("Execution runtime does not match plan")
        current = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        if current != plan["source_sha"]:
            raise ValueError("Source SHA does not match frozen plan")
        if sha256((root / ".github/compat/registry.json").read_bytes()) != plan["registry_sha256"]:
            raise ValueError("Registry snapshot does not match source")
        resolution = case["resolution"]
        for key, digest in (("requirements", "requirements_sha256"), ("report", "report_sha256")):
            if sha256(safe_path(plan_dir, resolution[key]).read_bytes()) != resolution[digest]:
                raise ValueError("Frozen lock digest mismatch")
        lock_dir = output / "locks"
        lock_dir.mkdir()
        for key in ("report", "requirements"):
            shutil.copyfile(safe_path(plan_dir, resolution[key]), lock_dir / Path(resolution[key]).name)
        contract = target["contract_test"]
        files = suite_files(root, contract)
        stage = "install"
        with tempfile.TemporaryDirectory(prefix="traceai-consumer-") as temporary:
            temporary = Path(temporary)
            # Dependency code sees only these disposable mounts and a clean
            # interpreter/toolchain; every post-install invocation uses sandbox().
            temporary.chmod(0o755)
            isolated_blocker = temporary / "block_network.sh"
            shutil.copyfile(blocker, isolated_blocker)
            consumer = temporary / "consumer"
            consumer.mkdir()
            row["contract"]["suite_sha256"] = copy_suite(root, consumer, files)
            venv = temporary / "venv"
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, env=clean_env(), timeout=120)
            python = str(venv / "bin/python")
            install = [sys.executable, "-I", "-m", "pip", "--python", python, "install", "--no-index", "--no-deps", "--require-hashes", "--find-links",
                       str(safe_path(plan_dir, resolution["wheelhouse"])), "-r", str(safe_path(plan_dir, resolution["requirements"]))]
            with (output / "install.log").open("w") as log:
                subprocess.run(install, cwd=consumer, env=clean_env(), stdout=log, stderr=subprocess.STDOUT, check=True, timeout=600)
            shared_output = temporary / "junit-output"
            shared_output.mkdir(mode=0o777)
            shared_output.chmod(0o777)
            toolchain = Path(sys.base_prefix).resolve()
            inventory = [python, "-I", "-c", "import importlib.metadata as m,json; print(json.dumps({d.metadata['Name']: d.version for d in m.distributions()}))"]
            stage = "infrastructure"
            with (output / "inventory-sandbox.log").open("w") as log:
                actual = json.loads(subprocess.check_output(sandbox(temporary, shared_output, toolchain, consumer, inventory),
                    cwd=consumer, env=clean_env(), stderr=log, text=True, timeout=120))
                installed = {canonicalize_name(n): v for n, v in actual.items()}
                freeze = subprocess.check_output(sandbox(temporary, shared_output, toolchain, consumer,
                    [python, "-I", "-m", "pip", "freeze", "--all"]), cwd=consumer, env=clean_env(), stderr=log, timeout=120)
            # Retry identity includes the lock's relative path, not only its digest.
            freeze_path = lock_dir / (case["case_id"] + ".freeze.txt")
            freeze_path.write_bytes(freeze)
            row["dependency_lock"] = {"format": "pip-freeze", "path": freeze_path.relative_to(output).as_posix(), "sha256": sha256(freeze)}
            stage = "requested_resolved_mismatch"
            # Compare every distribution in the complete report, not only direct pins.
            report = json.loads(safe_path(plan_dir, resolution["report"]).read_text())
            expected = {canonicalize_name(item["metadata"]["name"]): item["metadata"]["version"] for item in report["install"]}
            # A fresh --without-pip consumer has no bootstrapped distributions.
            # The frozen resolution includes pip itself for inventory tooling.
            if installed != expected:
                raise ValueError("Frozen complete dependency resolution changed")
            for wanted in row["requested"]:
                version = installed.get(canonicalize_name(wanted["name"]))
                if version is not None:
                    digest = next((r.get("artifact_sha256") for r in resolution["resolved"] if canonicalize_name(r["name"]) == canonicalize_name(wanted["name"])), None)
                    row["resolved"].append({"name": wanted["name"], "role": wanted["role"], "version": version, "artifact_sha256": digest})
                if version is None or wanted["selected_version"] is None or Version(version) != Version(wanted["selected_version"]):
                    raise ValueError("Requested/resolved mismatch: " + wanted["name"])
            # Preserve the resolver's frozen tuple order as well as identities;
            # PR1's strict plan reconciliation binds the complete original list.
            order = {canonicalize_name(r["name"]): i for i, r in enumerate(resolution["resolved"])}
            row["resolved"].sort(key=lambda r: order[canonicalize_name(r["name"])])
            for discovered in row["discovery"]:
                discovered["resolved_version"] = installed.get(canonicalize_name(discovered["name"]))
            stage = "infrastructure"
            junit_dir = output / "junit"
            junit_dir.mkdir()
            path = junit_dir / (case["case_id"] + f"-attempt{attempt}.xml")
            command = contract["command"]
            if command[:3] != ["python", "-m", "pytest"]:
                raise ValueError("Pilot runner accepts only trusted Python pytest contracts")
            isolated_junit = shared_output / path.name
            argv = [python, *contract_command(contract, str(isolated_junit))[1:]]
            row["contract"]["command"] = contract_command(contract, path.relative_to(output).as_posix())
            # Independently collect the full exact parametrized identity set in
            # the same frozen environment. Partial/deselected execution cannot
            # inherit scenario claims from a single executed function/parameter.
            collected_path = shared_output / "collection.json"
            collect_argv = [python, "-I", "-c", COLLECTION_CODE, str(collected_path), *command[3:],
                            "--import-mode=importlib", "--rootdir=.", "-p", "pytest_asyncio.plugin", "--collect-only"]
            collection_evidence = lock_dir / (case["case_id"] + f"-attempt{attempt}.collection.json")
            invocations["collection"] = invoke(sandbox(temporary, shared_output, toolchain, consumer, collect_argv),
                cwd=consumer, log=output / "collection.log", evidence=collected_path, retained=collection_evidence, output=output)
            write_json(output / invocation_path(case["case_id"], attempt), invocations)
            stage = "collection"
            collection = json.loads(collection_evidence.read_text()) if collection_evidence.is_file() else {}
            if invocations["collection"]["exit_code"] != 0 or invocations["collection"]["timed_out"] or not collection:
                row["status"] = "failed"
                row["failure"] = collection_failure(collection, output / "collection.log")
                return row
            if collection["deselected"] or collection.get("errors") or not collection["nodes"] or len(collection["nodes"]) != len(set(collection["nodes"])):
                raise ValueError("Empty/deselected/duplicate/errored full contract collection")
            stage = "infrastructure"
            invocations["contract"] = invoke(sandbox(temporary, shared_output, toolchain, consumer, argv),
                cwd=consumer, log=output / "contract.log", evidence=isolated_junit, retained=path, output=output)
            write_json(output / invocation_path(case["case_id"], attempt), invocations)
            if isolated_junit.is_symlink():
                raise ValueError("Symlink JUnit is forbidden")
            if not path.is_file():
                raise ValueError(f"Missing JUnit (runner/namespace exit {invocations['contract']['exit_code']}); see contract.log")
            stage = "collection"
            tests, details = junit(path, semantic_validator(root))
            row["contract"].update(tests=tests, collected=len(tests), executed=sum(t["outcome"] != "skipped" for t in tests),
                                   unexpected_skips=sum(t["outcome"] == "skipped" for t in tests),
                                   junit_paths=[path.relative_to(output).as_posix()])
            stage = "contract"
            finish_contract(row, target, root, collection, invocations["contract"]["exit_code"], details)
    except (ValueError, OSError, subprocess.SubprocessError, ET.ParseError, KeyError) as error:
        kind = locals().get("stage", "resolution")
        row["status"] = "blocked" if kind in ("resolution", "install", "infrastructure") else "failed"
        row["failure"] = failure(kind, str(error), [t["test_id"] for t in row["contract"]["tests"] if t["outcome"] in ("error", "failure")])
    finally:
        row["completed_at"] = now()
        write_json(output / invocation_path(case["case_id"], attempt), invocations)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--plan-dir", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--previous", type=Path, help="Original case artifact, mandatory on the one permitted retry")
    args = parser.parse_args()
    root, folder, output = args.root.resolve(), args.plan_dir.resolve(), args.out.resolve()
    output.mkdir(parents=True, exist_ok=False)
    raw = (folder / "compat-plan.json").read_bytes()
    plan = validate_plan(json.loads(raw))
    case = next(c for c in plan["cases"] if c["case_id"] == args.case_id)
    registry_raw = (folder / "registry.json").read_bytes()
    if sha256(registry_raw) != plan["registry_sha256"]:
        raise ValueError("Registry artifact digest mismatch")
    registry = json.loads(registry_raw)
    target = next(t for t in registry["integrations"] if t["id"] == case["integration_id"])
    history = []
    if int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")) > 1:
        if int(os.environ["GITHUB_RUN_ATTEMPT"]) > 2:
            raise ValueError("Only one immutable-plan retry is permitted; start a new run instead")
        if args.previous is None:
            raise ValueError("Retry must preserve previous evidence")
        from collect_results import check_tree, validate_case
        try:
            check_tree(args.previous)
            schema = json.loads(Path(__file__).with_name("results.schema.json").read_text())
            history = validate_case(json.loads((args.previous / "results.json").read_text()), plan, raw, case, schema)
            if len(history) != 1:
                raise ValueError("Previous artifact already contains a retry")
        except (ValueError, KeyError, TypeError, OSError) as error:
            # The immutable original remains untouched. Run the retry, but its
            # success can only be flaky until the original history is valid.
            missing = empty_case(case, plan, target)
            missing["failure"] = failure("automation", "Original planned case upload missing/invalid: " + str(error))
            history = [missing]
    # The first artifact is immutable. A killed retry cannot replace it. Its
    # independent result is merged chronologically by the collector.
    row = execute(root, folder, case, plan, target, output, Path(__file__).with_name("block_network.sh"), 2 if history else 1)
    if history and row["status"] == "passed" and history[-1]["status"] != "passed":
        row["status"] = "flaky"
        row["notes"].append("Original failure preserved in the immutable attempt-1 artifact.")
    header = run_header(plan, raw)
    header["completed_at"] = row["completed_at"]
    write_json(output / "results.json", {"schema_version": 1, "run": header, "cases": [row]})
    print(json.dumps({"case_id": case["case_id"], "status": row["status"], "failure": row["failure"]}))
    return 0 if row["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
