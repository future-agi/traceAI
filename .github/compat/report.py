#!/usr/bin/env python3
"""Trusted compatibility reporter: validate data, render evidence, reconcile issues.

Default is dry-run. PR/branch self-tests use synthetic fixtures only. Production
code must be installed on main separately; artifact/source code is never loaded.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.request
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone

# -I excludes CWD and user site packages; only these trusted sibling modules are
# imported. Never add an artifact or a tested source checkout to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from incidents import LABELS, heartbeat, iso, name_key, reconcile, timestamp
from publish import build_index, release_notes, render_markdown

ROOT = Path(__file__).resolve().parents[2]
REPO = "future-agi/traceAI"
CANARY = ".github/workflows/compatibility-canary.yml"
MAX_ARCHIVE = 24 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_FILES = 512


def safe_path(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
        raise ValueError("invalid artifact path")
    path = PurePosixPath(relative)
    if path.is_absolute() or any(p in ("..", ".", "") for p in relative.split("/")) or ":" in path.parts[0]:
        raise ValueError("absolute/traversing artifact path")
    root = root.resolve()
    full = root.joinpath(*path.parts)
    cursor = full
    while cursor != root:
        if cursor.is_symlink():
            raise ValueError("symlink artifact path")
        cursor = cursor.parent
    if not full.resolve().is_relative_to(root):
        raise ValueError("escaping artifact path")
    return full


def extract_archive(data, destination):
    """Never call extractall on an untrusted dependency job's archive."""
    if len(data) > MAX_ARCHIVE or any(destination.iterdir()):
        raise ValueError("oversized archive or nonempty extraction destination")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_EXPANDED:
            raise ValueError("archive exceeds file/expanded-size limit")
        names = set()
        for info in infos:
            # Directory records are optional but must also be safe and unique.
            relative = info.filename.rstrip("/")
            path = safe_path(destination, relative)
            folded = unicodedata.normalize("NFC", relative).casefold()
            if folded in names or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
                raise ValueError("duplicate, symlink or encrypted archive member")
            names.add(folded)
            if info.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                content = archive.read(info)
                if len(content) != info.file_size:
                    raise ValueError("truncated archive member")
                path.write_bytes(content)


def load_json(path, limit=8 * 1024 * 1024):
    if path.stat().st_size > limit:
        raise ValueError("JSON exceeds size limit")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON number")))


def schema_validate(data, filename):
    from jsonschema import Draft202012Validator, FormatChecker
    validator = Draft202012Validator(load_json(ROOT / ".github/compat" / filename), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(data), key=lambda e: str(list(e.path)))
    if errors:
        # Do not print artifact data or failed values into logs.
        raise ValueError(f"{filename}: invalid data at {list(errors[0].path)} ({errors[0].validator})")


def validate_bundle(bundle, registry, now, *, artifact_root=None, plan_bytes=None):
    schema_validate(bundle, "results.schema.json")
    run = bundle["run"]
    targets = {t["id"]: t for t in registry["integrations"]}
    if run["workflow"] not in ("Compatibility canary", CANARY, "compatibility-canary.yml"):
        raise ValueError("unexpected canary workflow")
    if run["url"] != f"https://github.com/{REPO}/actions/runs/{run['run_id']}":
        raise ValueError("run URL does not match identity")
    if not re.fullmatch(r"[1-9][0-9]*", run["run_id"]):
        raise ValueError("invalid GitHub run ID")
    if timestamp(run["completed_at"]) > now + timedelta(minutes=5) or timestamp(run["started_at"]) > timestamp(run["completed_at"]):
        raise ValueError("invalid/future run timestamps")
    if timestamp(run["cooldown_cutoff"]) > timestamp(run["started_at"]) - timedelta(hours=72) + timedelta(minutes=5):
        raise ValueError("release age cutoff is less than 72 hours")
    planned = run["planned_case_ids"]
    if not planned or any(not re.fullmatch(r"[a-zA-Z0-9._-]+", cid) for cid in planned):
        raise ValueError("empty/unsafe planned case IDs")
    planned_cases = {}
    if plan_bytes is not None:
        if hashlib.sha256(plan_bytes).hexdigest() != run["plan_sha256"]:
            raise ValueError("plan digest mismatch")
        plan = json.loads(plan_bytes)
        if plan["source_sha"] != run["source_sha"] or plan["workflow_sha"] != run["workflow_sha"]:
            raise ValueError("plan SHA identity mismatch")
        if plan["self_test"] != run["self_test"] or [c["case_id"] for c in plan["cases"]] != planned:
            raise ValueError("plan case/self-test identity mismatch")
        planned_cases = {c["case_id"]: c for c in plan["cases"]}
        if "cooldown_cutoff" in plan and plan["cooldown_cutoff"] != run["cooldown_cutoff"]:
            raise ValueError("plan cooldown mismatch")
    seen = set()
    for case in bundle["cases"]:
        ident = (case["case_id"], case["attempt"])
        if ident in seen or case["case_id"] not in planned:
            raise ValueError("duplicate/unplanned case")
        seen.add(ident)
        if planned_cases:
            selection = planned_cases[case["case_id"]]
            if any(selection.get(key) != case[key] for key in ("integration_id", "mode", "selector", "requested")):
                raise ValueError("case selection differs from retained plan")
            if case["language"] == "python" and not (case["runtime"]["version"] == selection.get("runtime") or
                    case["runtime"]["version"].startswith(str(selection.get("runtime")) + ".")):
                raise ValueError("actual runtime differs from plan")
        target = targets.get(case["integration_id"])
        if not target or case["language"] != target["language"] or case["source_sha"] != run["source_sha"]:
            raise ValueError("unknown target or case SHA/language mismatch")
        if timestamp(case["completed_at"]) > timestamp(run["completed_at"]) or timestamp(case["started_at"]) < timestamp(run["started_at"]):
            raise ValueError("case outside run time bounds")
        if timestamp(case["completed_at"]) < timestamp(case["started_at"]):
            raise ValueError("negative case duration")
        resolved, requested = {}, {}
        for field, output in (("resolved", resolved), ("requested", requested)):
            for package in case[field]:
                key = name_key(package["name"], case["language"])
                if key in output:
                    raise ValueError("duplicate distribution in result")
                if case["language"] == "python":
                    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", package["name"]):
                        raise ValueError("invalid Python distribution name")
                    version = package.get("version", package.get("selected_version"))
                    if version is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*", version):
                        raise ValueError("unsafe Python version")
                output[key] = package
        contract = case["contract"]
        tests = contract["tests"]
        test_ids = [t["test_id"] for t in tests]
        if len(test_ids) != len(set(test_ids)) or contract["collected"] != len(tests):
            raise ValueError("duplicate test IDs or collection count mismatch")
        if contract["executed"] != sum(t["outcome"] != "skipped" for t in tests):
            raise ValueError("executed count mismatch")
        if contract["unexpected_skips"] > sum(t["outcome"] == "skipped" for t in tests):
            raise ValueError("skip count mismatch")
        lock = case["dependency_lock"]
        for path in contract["junit_paths"] + ([lock["path"]] if lock else []):
            full = safe_path(artifact_root or Path(tempfile.gettempdir()), path)
            if artifact_root is not None and not full.is_file():
                raise ValueError("missing referenced artifact file")
        if artifact_root is not None and lock:
            lock_path = safe_path(artifact_root, lock["path"])
            if lock_path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("oversized dependency lock")
            if hashlib.sha256(lock_path.read_bytes()).hexdigest() != lock["sha256"]:
                raise ValueError("dependency lock digest mismatch")
        if artifact_root is not None and contract["junit_paths"]:
            observed = {outcome: 0 for outcome in ("passed", "failure", "error", "skipped")}
            for relative in contract["junit_paths"]:
                path = safe_path(artifact_root, relative)
                if path.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError("oversized JUnit")
                content = path.read_bytes()
                if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
                    raise ValueError("JUnit DTD/entities are forbidden")
                try:
                    xml = ET.fromstring(content)
                except ET.ParseError:
                    raise ValueError("malformed JUnit") from None
                for node in xml.iter("testcase"):
                    outcome = next((k for k in ("error", "failure", "skipped") if node.find(k) is not None), "passed")
                    observed[outcome] += 1
            if any(observed[k] != sum(t["outcome"] == k for t in tests) for k in observed):
                raise ValueError("JUnit/JSON outcome count mismatch")
        if case["status"] == "passed":
            minimum = (target.get("contract_test") or {}).get("minimum_executed", 1)
            if not lock or not contract["junit_paths"] or contract["executed"] < max(1, minimum):
                raise ValueError("pass lacks executed contract/lock/JUnit")
            if case["failure"] or any(t["outcome"] != "passed" for t in tests) or contract["unexpected_skips"]:
                raise ValueError("false pass with failed/skipped tests")
            roles = {p["role"] for p in case["resolved"]}
            if not {"instrumentation", "core", "upstream"}.issubset(roles):
                raise ValueError("pass lacks actual instrumentation/core/upstream")
            instrument = resolved.get(name_key(target["distribution"], case["language"]))
            core_names = {name_key(t["distribution"], case["language"]) for t in targets.values()
                          if t["language"] == case["language"] and t["role"] == "core"}
            if not instrument or instrument["role"] != "instrumentation" or not any(
                    name in resolved and resolved[name]["role"] == "core" for name in core_names):
                raise ValueError("wrong instrumentation/core distribution")
            expected_names = {name_key(p["name"], case["language"]) for p in target["upstream"]}
            if not expected_names.issubset(resolved):
                raise ValueError("pass lacks complete companion tuple")
            for name, package in requested.items():
                actual = resolved.get(name)
                if not actual or actual["role"] != package["role"] or (package["selected_version"] is not None and
                        package["selected_version"] != actual["version"]):
                    raise ValueError("requested/resolved mismatch")
            for discovery in case["discovery"]:
                actual = resolved.get(name_key(discovery["name"], case["language"]))
                if not actual or discovery["resolved_version"] != actual["version"] or discovery["yanked"]:
                    raise ValueError("discovery/resolved mismatch or yanked pass")
                if case["selector"] == "latest_eligible" and actual["version"] != discovery["latest_eligible"]:
                    raise ValueError("latest-eligible claim does not match tested version")
        if case["status"] in ("failed", "flaky", "quarantined") and not case["failure"]:
            raise ValueError("failure status without signature")
    if {cid for cid, attempt in seen if attempt == 1} != set(planned):
        raise ValueError("missing planned result/first attempt")
    for case in bundle["cases"]:
        if case["attempt"] == 2 and case["status"] == "passed":
            first = next(c for c in bundle["cases"] if c["case_id"] == case["case_id"] and c["attempt"] == 1)
            if first["status"] != "passed":
                raise ValueError("fail-then-pass retry must remain flaky")
    return bundle


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise ValueError("insecure API redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.remove_header("Authorization")
        return redirected


class GitHub:
    def __init__(self, token=None):
        self.token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        self.opener = urllib.request.build_opener(SafeRedirect())

    def request(self, path, data=None, method="GET", binary=False):
        if not path.startswith(f"/repos/{REPO}/"):
            raise ValueError("API path outside fixed repository")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "traceai-compat-reporter"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        payload = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request("https://api.github.com" + path, data=payload, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=60) as response:
                content = response.read(MAX_ARCHIVE + 1)
        except urllib.error.HTTPError as exc:
            raise ValueError(f"GitHub API {method} failed with HTTP {exc.code}") from None
        if len(content) > MAX_ARCHIVE:
            raise ValueError("oversized API response")
        return content if binary else json.loads(content)

    def pages(self, path, key=None, max_pages=20):
        rows = []
        sep = "&" if "?" in path else "?"
        for page in range(1, max_pages + 1):
            value = self.request(f"/repos/{REPO}/{path}{sep}per_page=100&page={page}")
            batch = value[key] if key else value
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise ValueError("API pagination bound reached; refusing partial incident/history state")


def verify_provenance(bundle, metadata, api):
    run = bundle["run"]
    if metadata.get("repository", {}).get("full_name") != REPO or metadata.get("head_repository", {}).get("full_name") != REPO:
        raise ValueError("foreign repository run")
    if metadata.get("path") != CANARY or metadata.get("head_branch") not in ("main", "dev"):
        raise ValueError("untrusted workflow/branch")
    if metadata.get("event") not in ("schedule", "workflow_dispatch", "push") or metadata.get("pull_requests"):
        raise ValueError("PR or unexpected run cannot report incidents")
    if metadata["status"] != "completed" or run["self_test"]:
        raise ValueError("incomplete/self-test run cannot report incidents")
    if str(metadata["id"]) != run["run_id"] or metadata["run_attempt"] != run["run_attempt"]:
        raise ValueError("run/attempt mismatch")
    if metadata["head_sha"] != run["workflow_sha"] or metadata["event"] != run["event"] or metadata["html_url"] != run["url"]:
        raise ValueError("workflow SHA/event/URL mismatch")
    if metadata.get("run_started_at") and timestamp(run["started_at"]) < timestamp(metadata["run_started_at"]) - timedelta(minutes=5):
        raise ValueError("artifact starts before GitHub run")
    if metadata.get("updated_at") and timestamp(run["completed_at"]) > timestamp(metadata["updated_at"]) + timedelta(minutes=5):
        raise ValueError("artifact completes after GitHub run")
    # The scheduler can execute main code while testing a distinct immutable dev
    # SHA. Require source ancestry in a trusted branch, not equality to workflow SHA.
    comparison = api.request(f"/repos/{REPO}/compare/{run['source_sha']}...dev")
    if comparison.get("status") not in ("ahead", "identical"):
        raise ValueError("tested source is not in trusted dev ancestry")


def github_results(api, registry, now, selected_run=None):
    since = (now - timedelta(days=16)).date().isoformat()
    runs = api.pages("actions/workflows/compatibility-canary.yml/runs?status=completed&created=%3E%3D" + since,
                     "workflow_runs", max_pages=5)
    if selected_run and not any(str(r["id"]) == str(selected_run) for r in runs):
        runs.insert(0, api.request(f"/repos/{REPO}/actions/runs/{int(selected_run)}"))
    bundles, errors = [], []
    for meta in runs:
        if timestamp(meta["created_at"]) < now - timedelta(days=16):
            continue
        if meta.get("head_branch") not in ("main", "dev") or meta.get("event") == "pull_request":
            continue
        if meta.get("head_repository", {}).get("full_name") != REPO or meta.get("path") != CANARY:
            continue
        artifacts = api.pages(f"actions/runs/{meta['id']}/artifacts", "artifacts", max_pages=2)
        matches = [a for a in artifacts if a["name"] == "compatibility-results" and not a["expired"]]
        if not matches:
            # Early rollout has self-test-only dev runs with no live artifact;
            # heartbeat detects missing expected live sweeps instead of inventing a pass.
            continue
        try:
            if len(matches) != 1 or matches[0]["size_in_bytes"] > MAX_ARCHIVE:
                raise ValueError("duplicate/oversized aggregate artifact")
            archive = api.request(f"/repos/{REPO}/actions/artifacts/{matches[0]['id']}/zip", binary=True)
            with tempfile.TemporaryDirectory(prefix="compat-results-") as tmp:
                root = Path(tmp)
                extract_archive(archive, root)
                bundle = load_json(safe_path(root, "results.json"))
                if bundle["run"].get("self_test"):
                    continue
                validate_bundle(bundle, registry, now, artifact_root=root,
                                plan_bytes=safe_path(root, "compat-plan.json").read_bytes())
                verify_provenance(bundle, meta, api)
                bundles.append(bundle)
        except (ValueError, KeyError, OSError, zipfile.BadZipFile) as exc:
            # Failure text contains no artifact payload. Preserve the broken run
            # as a health incident; malformed evidence must not disappear silently.
            errors.append({"id": f"invalid-run:{meta['id']}",
                           "summary": f"Invalid canary artifact for run {meta['id']}: {type(exc).__name__}. "
                                      "Inspect the trusted reporter validation log and rerun the retained plan."})
    return bundles, errors


def apply_actions(api, actions):
    for name, color in LABELS.items():
        try:
            api.request(f"/repos/{REPO}/labels", {"name": name, "color": color}, "POST")
        except ValueError as exc:
            if "HTTP 422" not in str(exc):
                raise
    for action in actions:
        number = action["number"]
        payload = {k: action[k] for k in ("title", "body", "state")}
        if number:
            # Preserve unrelated human labels while replacing only our namespace.
            issue = api.request(f"/repos/{REPO}/issues/{number}")
            labels = [l["name"] for l in issue["labels"] if not l["name"].startswith("compat:") and l["name"] != "compatibility"]
            payload["labels"] = labels + action["labels"]
            api.request(f"/repos/{REPO}/issues/{number}", payload, "PATCH")
        else:
            payload.pop("state")
            payload["labels"] = action["labels"]
            api.request(f"/repos/{REPO}/issues", payload, "POST")


def write_outputs(output, runs, registry, now, issues=(), extra_health=(), fixture=False, baseline=False):
    output.mkdir(parents=True, exist_ok=True)
    index = build_index(runs, registry, now, fixture=fixture)
    index["health"] += list(extra_health)
    incidents = reconcile(runs, issues, registry, now, index["health"])
    outputs = {"results-index.json": index, "incident-actions.json": {"dry_run": True, "actions": incidents["actions"]},
               "remediation-handoff.json": incidents["handoffs"], "release-notes.json": release_notes(index)}
    schema_validate(index, "results-index.schema.json")
    for filename, value in outputs.items():
        (output / filename).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    (output / "compatibility.md").write_text(render_markdown(index, baseline=baseline))
    return incidents


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--now", default=None)
    parser.add_argument("--fixtures", action="store_true")
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--github", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    now = timestamp(args.now) if args.now else datetime.now(timezone.utc)
    registry = load_json(ROOT / ".github/compat/registry.json")
    runs, issues, errors = [], [], []
    api = None
    if args.fixtures:
        runs = load_json(ROOT / ".github/compat/fixtures/reporter-results.json")["runs"]
        issues = load_json(ROOT / ".github/compat/fixtures/incident-history.json")["issues"]
        now = timestamp("2026-10-28T12:00:00Z")
        for bundle in runs:
            validate_bundle(bundle, registry, now)
    elif args.artifact_root:
        root = args.artifact_root.resolve()
        bundle = load_json(safe_path(root, "results.json"))
        validate_bundle(bundle, registry, now, artifact_root=root, plan_bytes=safe_path(root, "compat-plan.json").read_bytes())
        runs = [bundle]
    elif args.github:
        api = GitHub()
        runs, errors = github_results(api, registry, now, args.run_id)
        if args.apply:
            if (os.environ.get("COMPAT_REPORT_ALLOW_WRITES") != "true" or
                    os.environ.get("GITHUB_REF") != "refs/heads/main" or
                    os.environ.get("GITHUB_REPOSITORY") != REPO or
                    os.environ.get("GITHUB_EVENT_NAME") not in ("workflow_run", "workflow_dispatch", "schedule")):
                raise ValueError("issue writes require the trusted default-branch workflow guard")
            issues = api.pages("issues?state=all")
    elif not args.baseline:
        parser.error("choose --fixtures, --artifact-root, --github or --baseline")
    if args.apply and (not args.github or args.fixtures or args.artifact_root or args.baseline):
        raise ValueError("fixtures/branch artifacts are dry-run only")
    incidents = write_outputs(args.output, runs, registry, now, issues, errors, args.fixtures, args.baseline)
    if args.apply:
        apply_actions(api, incidents["actions"])
        (args.output / "applied.json").write_text(json.dumps({"actions_applied": len(incidents["actions"])}) + "\n")
    print(json.dumps({"dry_run": not args.apply, "validated_runs": len(runs), "health_errors": len(errors),
                      "incident_actions": len(incidents["actions"]), "output": str(args.output)}))


if __name__ == "__main__":
    main()
