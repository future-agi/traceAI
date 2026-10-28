#!/usr/bin/env python3
"""Trusted compatibility reporter: validate data, render evidence, reconcile issues.

Default is dry-run. PR/branch self-tests use synthetic fixtures only. Production
code must be installed on main separately; artifact/source code is never loaded.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import importlib.util
import base64
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
import zlib
from datetime import datetime, timedelta, timezone

# -I excludes CWD and user site packages; only these trusted sibling modules are
# imported. Never add an artifact or a tested source checkout to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from incidents import LABELS, decode_json, heartbeat, iso, name_key, reconcile, timestamp
from publish import build_index, release_notes, render_markdown

ROOT = Path(__file__).resolve().parents[2]
REPO = "future-agi/traceAI"
CANARY = ".github/workflows/compatibility-canary.yml"
MAX_ARCHIVE = 24 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
# Match the producer's bounded retained aggregate count (21 weekly cells and
# both attempts already exceed 512). Byte/path/decompression limits stay strict;
# this does not admit the separate large plan wheelhouse as report evidence.
MAX_FILES = 4096
MAX_SOURCE_FILES = 512


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
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_EXPANDED:
                raise ValueError("archive exceeds file/expanded-size limit")
            names = set()
            for info in infos:
                # Only the methods used by Actions artifacts are admitted. Do not
                # instantiate additional decompressors for attacker-chosen codecs.
                if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise ValueError("unsupported archive compression")
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
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError, EOFError, zlib.error):
        # A bad run must not abort validation/rendering of unrelated healthy runs.
        raise ValueError("malformed archive or compressed member") from None


def load_json(path, limit=8 * 1024 * 1024):
    if path.stat().st_size > limit:
        raise ValueError("JSON exceeds size limit")
    return decode_json(path.read_bytes(), limit)


def collection_limits(data):
    """Bound work before JSON Schema (including uniqueItems/branch validation).

    The byte/depth limits at decoding are not enough: a tiny array of empty
    objects can produce thousands of errors. Traverse iteratively and never
    retain errors, schema contexts or copies of artifact collections.
    """
    limits = {"cases": 512, "planned_case_ids": 256, "tests": 10000,
              "requested": 4096, "resolved": 4096, "discovery": 4096,
              "junit_paths": 512, "integrations": 512}
    stack, nodes = [(data, "", 0)], 0
    while stack:
        value, key, depth = stack.pop()
        nodes += 1
        if nodes > 150000 or depth > 64:
            raise ValueError("JSON exceeds collection limit")
        if isinstance(value, (dict, list)):
            bound = limits.get(key, 16384 if isinstance(value, list) else 4096)
            if len(value) > bound:
                raise ValueError("JSON exceeds collection limit")
            if isinstance(value, dict):
                stack.extend((v, k, depth + 1) for k, v in value.items())
            else:
                stack.extend((v, "", depth + 1) for v in value)


def schema_validate(data, filename):
    from jsonschema import Draft202012Validator, FormatChecker
    collection_limits(data)
    validator = Draft202012Validator(load_json(ROOT / ".github/compat" / filename), format_checker=FormatChecker())
    error = next(validator.iter_errors(data), None)
    if error is not None:
        # Do not print artifact data or failed values into logs.
        raise ValueError(f"{filename}: invalid data at {list(error.path)} ({error.validator})")


def placeholder(case):
    """Collector metadata is not an observed execution runtime."""
    return (case["status"] == "blocked" and case["failure"] is not None
            and case["failure"]["kind"] == "automation" and not case["resolved"]
            and case["dependency_lock"] is None and not case["contract"]["tests"]
            and not case["contract"]["junit_paths"] and case["contract"]["executed"] == 0)


def validate_plan_snapshot(raw):
    """Validate the ABI fields we consume before indexing any plan content."""
    plan = decode_json(raw)
    collection_limits(plan)
    if not isinstance(plan, dict) or any(key not in plan for key in
            ("source_sha", "workflow_sha", "self_test", "cases")):
        raise ValueError("invalid retained plan shape")
    if (not isinstance(plan["cases"], list) or not plan["cases"] or len(plan["cases"]) > 256
            or not isinstance(plan["self_test"], bool)):
        raise ValueError("invalid retained plan cases/self-test")
    ids = set()
    for selected in plan["cases"]:
        if not isinstance(selected, dict) or any(key not in selected for key in
                ("case_id", "integration_id", "mode", "selector", "requested", "runtime")):
            raise ValueError("invalid retained plan case")
        if (not isinstance(selected["case_id"], str) or selected["case_id"] in ids
                or not isinstance(selected["runtime"], str) or not selected["runtime"]
                or len(selected["runtime"]) > 128 or not isinstance(selected["requested"], list)):
            raise ValueError("invalid/duplicate retained plan selection")
        identity = selected.get("runtime_identity")
        if identity is not None and (not isinstance(identity, dict) or any(
                not isinstance(identity.get(k), str) or not identity[k] for k in ("version", "implementation", "os", "arch"))
                or not (identity["version"] == selected["runtime"] or identity["version"].startswith(selected["runtime"] + "."))):
            raise ValueError("invalid retained plan runtime identity")
        ids.add(selected["case_id"])
    return plan


def lock_packages(content, format, language):
    """Read data only. Unsupported/native formats cannot certify an install tuple."""
    packages = {}
    def add(name, version):
        if not isinstance(name, str) or not isinstance(version, str) or not name or not version:
            raise ValueError("invalid lock package metadata")
        key = name_key(name, language)
        if key in packages:
            raise ValueError("duplicate lock distribution")
        packages[key] = version
    if language == "python" and format == "pip-freeze":
        from urllib.parse import unquote, urlsplit
        for line in content.decode("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pinned = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:===|==)([A-Za-z0-9][A-Za-z0-9.!+_-]*)", line)
            if pinned:
                add(*pinned.groups())
                continue
            # pip freeze records installed source wheels as direct references.
            # The wheel filename carries the version; never dereference the URL.
            direct = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*) @ (\S+)", line)
            if direct:
                name, url = direct.groups()
                wheel = unquote(urlsplit(url).path).rsplit("/", 1)[-1]
                parts = wheel[:-4].split("-") if wheel.endswith(".whl") else []
                if len(parts) in (5, 6) and name_key(parts[0], language) == name_key(name, language):
                    add(name, parts[1])
                    continue
            raise ValueError("unverifiable pip-freeze entry")
    elif language == "python" and format == "pip-report":
        data = decode_json(content)
        if not isinstance(data, dict) or not isinstance(data.get("install"), list):
            raise ValueError("invalid pip-report")
        for item in data["install"]:
            if not isinstance(item, dict) or not isinstance(item.get("metadata"), dict):
                raise ValueError("invalid pip-report metadata")
            add(item["metadata"].get("name"), item["metadata"].get("version"))
    else:
        raise ValueError("unsupported dependency lock format for evidence verification")
    return packages


def junit_outcomes(content):
    class NoDeclarations(ET.TreeBuilder):
        def doctype(self, name, pubid, system):
            # Parser-level rejection also covers UTF-16/32 encodings; scanning
            # only ASCII bytes allowed declarations to bypass this boundary.
            raise ValueError("JUnit DTD/entity declarations are forbidden")
    try:
        xml = ET.fromstring(content, parser=ET.XMLParser(target=NoDeclarations()))
    except ET.ParseError:
        raise ValueError("malformed JUnit") from None
    if Path(__file__).with_name("validate.py").is_file():
        # Once absorbed, production uses the same trusted parser as the producer,
        # not a separately evolving interpretation of pytest's class identities.
        with tempfile.TemporaryDirectory(prefix="compat-junit-") as tmp:
            path = Path(tmp) / "report.xml"
            path.write_bytes(content)
            tests = trusted_validator().junit_tests(path)
        return {test["test_id"]: test["outcome"] for test in tests}
    # Standalone foundation fixtures precede canonical absorption. Keep exact
    # canonical class splitting here too; production still requires that module.
    observed = {}
    for node in xml.iter("testcase"):
        file, name, classname = node.get("file", ""), node.get("name", ""), node.get("classname", "")
        if not name or not (file or classname):
            raise ValueError("JUnit lacks canonical test identity")
        if file:
            path = PurePosixPath(file)
            if path.is_absolute() or ".." in path.parts or "\\" in file:
                raise ValueError("noncanonical JUnit file")
            marker = path.stem
            cls = classname.rsplit(marker, 1)[-1].strip(".") if marker in classname else ""
            test_id = file + "::" + (cls.replace(".", "::") + "::" if cls else "") + name
        else:
            test_id = classname.replace(".", "/") + ".py::" + name
        outcomes = [kind for kind in ("failure", "error", "skipped") if node.find(kind) is not None]
        if len(outcomes) > 1 or test_id in observed:
            raise ValueError("duplicate/contradictory JUnit test identity")
        observed[test_id] = outcomes[0] if outcomes else "passed"
    return observed


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
        plan = validate_plan_snapshot(plan_bytes)
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
            if case["language"] == "python" and not placeholder(case) and not (
                    case["runtime"]["version"] == selection["runtime"] or
                    case["runtime"]["version"].startswith(selection["runtime"] + ".")):
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
            content = lock_path.read_bytes()
            if hashlib.sha256(content).hexdigest() != lock["sha256"]:
                raise ValueError("dependency lock digest mismatch")
            locked = lock_packages(content, lock["format"], case["language"])
            if any(locked.get(name) != package["version"] for name, package in resolved.items()):
                raise ValueError("dependency lock contradicts resolved versions")
        if artifact_root is not None:
            observed = {}
            for relative in contract["junit_paths"]:
                path = safe_path(artifact_root, relative)
                if path.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError("oversized JUnit")
                outcomes = junit_outcomes(path.read_bytes())
                if observed.keys() & outcomes.keys():
                    raise ValueError("duplicate JUnit test identity across files")
                observed.update(outcomes)
            if observed != {t["test_id"]: t["outcome"] for t in tests}:
                raise ValueError("JUnit/JSON test identity/outcome mismatch")
        if case["status"] in ("passed", "flaky"):
            minimum = (target.get("contract_test") or {}).get("minimum_executed", 1)
            if not lock or not contract["junit_paths"] or contract["executed"] < max(1, minimum):
                raise ValueError("pass lacks executed contract/lock/JUnit")
            if (case["status"] == "passed" and case["failure"]) or any(t["outcome"] != "passed" for t in tests) or contract["unexpected_skips"]:
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
            discoveries = {name_key(d["name"], case["language"]): d for d in case["discovery"]}
            tuple_names = {name for name, p in resolved.items() if p["role"] in ("upstream", "companion")}
            if len(discoveries) != len(case["discovery"]) or set(discoveries) != tuple_names or not expected_names.issubset(discoveries):
                raise ValueError("pass lacks complete discovery provenance")
            for name, discovery in discoveries.items():
                actual, wanted = resolved[name], requested.get(name)
                if (not wanted or discovery["requested_version"] != wanted["selected_version"] or
                        discovery["resolved_version"] != actual["version"] or discovery["yanked"]):
                    raise ValueError("discovery/requested/resolved mismatch or yanked pass")
                release_at = discovery["published_at"] or discovery["first_seen_at"]
                if not release_at or timestamp(release_at) > timestamp(run["cooldown_cutoff"]):
                    raise ValueError("selected release lacks proven 72-hour cooldown")
                if discovery["first_seen_at"] and timestamp(discovery["first_seen_at"]) > timestamp(run["completed_at"]):
                    raise ValueError("future discovery timestamp")
                if case["selector"] == "latest_eligible" and actual["version"] != discovery["latest_eligible"]:
                    raise ValueError("latest-eligible claim does not match tested version")
        if case["status"] in ("failed", "quarantined") and not case["failure"]:
            raise ValueError("failure status without signature")
    if {cid for cid, attempt in seen if attempt == 1} != set(planned):
        raise ValueError("missing planned result/first attempt")
    for case in bundle["cases"]:
        if case["status"] == "flaky" and case["attempt"] != 2:
            raise ValueError("flaky requires a retained failed first attempt")
        if case["attempt"] == 2:
            first = next(c for c in bundle["cases"] if c["case_id"] == case["case_id"] and c["attempt"] == 1)
            if case["requested"] != first["requested"]:
                raise ValueError("retry selection differs from original attempt")
            if case["status"] == "passed" and first["status"] != "passed":
                raise ValueError("fail-then-pass retry must remain flaky")
            if case["status"] == "flaky":
                if first["status"] not in ("failed", "blocked", "quarantined") or first["failure"] is None:
                    raise ValueError("flaky lacks retained original failure")
                if case["failure"] is not None and case["failure"] != first["failure"]:
                    raise ValueError("flaky failure differs from retained original failure")
            if first["dependency_lock"] and case["dependency_lock"] and (
                    first["dependency_lock"]["sha256"] != case["dependency_lock"]["sha256"] or
                    first["dependency_lock"]["format"] != case["dependency_lock"]["format"]):
                raise ValueError("retry changed the installed dependency lock")
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
        return content if binary else decode_json(content, MAX_ARCHIVE)

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


def verify_provenance(bundle, metadata, api, *, allow_self_test=False):
    run = bundle["run"]
    if metadata.get("repository", {}).get("full_name") != REPO or metadata.get("head_repository", {}).get("full_name") != REPO:
        raise ValueError("foreign repository run")
    if metadata.get("path") != CANARY or metadata.get("head_branch") not in ("main", "dev"):
        raise ValueError("untrusted workflow/branch")
    if metadata.get("event") not in ("schedule", "workflow_dispatch", "push") or metadata.get("pull_requests"):
        raise ValueError("PR or unexpected run cannot report incidents")
    if metadata["status"] != "completed" or (run["self_test"] and not allow_self_test):
        raise ValueError("incomplete/self-test run cannot report incidents")
    if str(metadata["id"]) != run["run_id"] or metadata["run_attempt"] != run["run_attempt"]:
        raise ValueError("run/attempt mismatch")
    if metadata["head_sha"] != run["workflow_sha"] or metadata["event"] != run["event"] or metadata["html_url"] != run["url"]:
        raise ValueError("workflow SHA/event/URL mismatch")
    attempts = {metadata["run_attempt"]: metadata}
    if run["run_attempt"] > 2:
        raise ValueError("only the original attempt and one retry are supported")
    if run["run_attempt"] > 1:
        attempts[1] = api.request(f"/repos/{REPO}/actions/runs/{run['run_id']}/attempts/1")
        first = attempts[1]
        if (str(first.get("id")) != run["run_id"] or first.get("run_attempt") != 1 or
                first.get("head_sha") != run["workflow_sha"] or first.get("event") != run["event"]):
            raise ValueError("original attempt provenance mismatch")
    original = attempts[1]
    if original.get("run_started_at") and timestamp(run["started_at"]) < timestamp(original["run_started_at"]) - timedelta(minutes=5):
        raise ValueError("artifact starts before original GitHub attempt")
    if original.get("updated_at") and timestamp(run["started_at"]) > timestamp(original["updated_at"]) + timedelta(minutes=5):
        raise ValueError("retained plan starts after original GitHub attempt")
    if metadata.get("updated_at") and timestamp(run["completed_at"]) > timestamp(metadata["updated_at"]) + timedelta(minutes=5):
        raise ValueError("artifact completes after GitHub run")
    for case in bundle["cases"]:
        if placeholder(case):
            continue  # A collector placeholder is explicitly not execution evidence.
        attempt = attempts.get(case["attempt"])
        if attempt is None:
            raise ValueError("case has no matching GitHub attempt")
        if attempt.get("run_started_at") and timestamp(case["started_at"]) < timestamp(attempt["run_started_at"]) - timedelta(minutes=5):
            raise ValueError("case starts before its GitHub attempt")
        if attempt.get("updated_at") and timestamp(case["completed_at"]) > timestamp(attempt["updated_at"]) + timedelta(minutes=5):
            raise ValueError("case completes after its GitHub attempt")
    # The scheduler can execute main code while testing a distinct immutable dev
    # SHA. Require source ancestry in a trusted branch, not equality to workflow SHA.
    comparison = api.request(f"/repos/{REPO}/compare/{run['source_sha']}...dev")
    if comparison.get("status") not in ("ahead", "identical"):
        raise ValueError("tested source is not in trusted dev ancestry")


def trusted_validator():
    """Load executable code only from the separately trusted reporter checkout."""
    path = Path(__file__).with_name("validate.py")
    if not path.is_file():
        raise ValueError("canonical validator must be absorbed before production reporting")
    try:
        spec = importlib.util.spec_from_file_location("compat_trusted_validator", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except ImportError:
        raise ValueError("canonical validator dependency is unavailable") from None


def canonical_validate(bundle, registry, artifact_root, plan_bytes, source_root):
    """Use PR1's trusted implementation, never a validator from tested source."""
    collection_limits(bundle)
    collection_limits(registry)
    try:
        module = trusted_validator()
        plan = validate_plan_snapshot(plan_bytes)
        return module.validate_results(bundle, registry, expected_source_sha=plan["source_sha"],
                                       expected_case_ids=[c["case_id"] for c in plan["cases"]],
                                       artifact_root=artifact_root, source_root=source_root,
                                       expected_plan=plan, plan_bytes=plan_bytes)
    except RecursionError:
        raise ValueError("canonical evidence exceeds nesting limit") from None
    except ImportError:
        raise ValueError("canonical validator dependency is unavailable") from None


def snapshot_registry(api, source_sha, destination):
    """Authenticate historical declaration data, but execute today's validator.

    Changes in current qualification/contract configuration cannot rewrite what
    an older producer was actually asked to execute. Customer qualification is
    still taken from the current registry by the publisher, never from artifacts.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("invalid source registry commit")
    relative = ".github/compat/registry.json"
    blob = api.request(f"/repos/{REPO}/contents/{relative}?ref={source_sha}")
    if (blob.get("encoding") != "base64" or blob.get("type") != "file" or blob.get("path") != relative
            or not re.fullmatch(r"[0-9a-f]{40}", blob.get("sha", ""))
            or not isinstance(blob.get("size"), int) or not 0 < blob["size"] <= 8 * 1024 * 1024):
        raise ValueError("invalid historical registry blob")
    content = base64.b64decode(blob["content"].replace("\n", ""), validate=True)
    actual = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
    if actual != blob["sha"] or len(content) != blob["size"]:
        raise ValueError("historical registry blob identity mismatch")
    registry = decode_json(content)
    schema_validate(registry, "registry.schema.json")
    trusted_validator().validate_registry(registry)
    path = safe_path(destination, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return registry


def snapshot_source(api, bundle, registry, destination):
    """Fetch only registered suite data at the authenticated source commit.

    AST/digest inspection is data-only: never import or run this source, never
    put it on sys.path, and never use commands/paths supplied by the artifact.
    Historical suite hashes cannot be compared to today's changed checkout.
    """
    targets = {t["id"]: t for t in registry["integrations"]}
    # No suite was executed for resolution/install/missing-upload placeholders.
    # Missing registered files can be the failure itself; do not discard that row
    # (or unrelated healthy cells) before canonical failure validation can run.
    # Any actual execution/success still needs its complete historical suite.
    needs_suite = lambda c: (c["status"] in ("passed", "flaky", "quarantined") or
                             c["contract"]["tests"] or c["contract"]["junit_paths"] or
                             c["contract"]["collected"] or c["contract"]["executed"])
    contracts = [(targets[c["integration_id"]].get("contract_test") or {})
                 for c in bundle["cases"] if needs_suite(c)]
    paths = {p for contract in contracts for p in contract.get("paths", [])}
    prefixes, configs = set(), set()
    for relative in paths:
        safe_path(destination, relative)
        path = PurePosixPath(relative)
        prefixes.add(path.parent.as_posix() + "/")
        for parent in [path.parent, *path.parent.parents]:
            for name in ("conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "__init__.py"):
                configs.add((parent / name).as_posix())
    if not paths:
        return destination
    tree = api.request(f"/repos/{REPO}/git/trees/{bundle['run']['source_sha']}?recursive=1")
    if tree.get("truncated") or not isinstance(tree.get("tree"), list):
        raise ValueError("unavailable/truncated trusted source tree")
    selected = [row for row in tree["tree"] if row.get("type") != "tree" and
                (row.get("path") in configs or any(row.get("path", "").startswith(p) for p in prefixes))
                and "__pycache__" not in PurePosixPath(row.get("path", "")).parts and not row.get("path", "").endswith(".pyc")]
    if len(selected) > MAX_SOURCE_FILES or sum(row.get("size", 0) for row in selected) > MAX_EXPANDED:
        raise ValueError("trusted source suite exceeds size/file limit")
    seen = set()
    for row in selected:
        path = safe_path(destination, row["path"])
        folded = unicodedata.normalize("NFC", row["path"]).casefold()
        if folded in seen or row.get("mode") not in ("100644", "100755") or row.get("type") != "blob":
            raise ValueError("duplicate/symlink/non-file trusted suite path")
        seen.add(folded)
        if not re.fullmatch(r"[0-9a-f]{40}", row.get("sha", "")) or row.get("size", MAX_ARCHIVE) > 8 * 1024 * 1024:
            raise ValueError("invalid/oversized source blob")
        blob = api.request(f"/repos/{REPO}/git/blobs/{row['sha']}")
        if blob.get("encoding") != "base64":
            raise ValueError("unexpected trusted source encoding")
        content = base64.b64decode(blob["content"].replace("\n", ""), validate=True)
        actual = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        if actual != row["sha"] or len(content) != row.get("size"):
            raise ValueError("trusted source blob identity mismatch")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    if any(not safe_path(destination, p).is_file() for p in paths):
        raise ValueError("missing registered trusted suite")
    return destination


def github_results(api, registry, now, selected_run=None, *, plans=None, diagnostics=None):
    since = (now - timedelta(days=16)).date().isoformat()
    runs = api.pages("actions/workflows/compatibility-canary.yml/runs?status=completed&created=%3E%3D" + since,
                     "workflow_runs", max_pages=5)
    bundles, errors = [], []
    # The run list's created filter refers to the ORIGINAL execution, even when
    # a month-old ID was rerun today. Artifact upload times are independent of
    # that date. Discover recent aggregates too, then authenticate each run in
    # exactly the same path as an explicit selection. No durable browser cache
    # or unauthenticated remembered run IDs are involved.
    discovered = {str(r["id"]) for r in runs}
    for artifact in api.pages("actions/artifacts?name=compatibility-results", "artifacts", max_pages=5):
        if (artifact.get("name") != "compatibility-results" or artifact.get("expired", True)
                or not artifact.get("created_at") or timestamp(artifact["created_at"]) < now - timedelta(days=16)):
            continue
        ident = str((artifact.get("workflow_run") or {}).get("id", ""))
        if not re.fullmatch(r"[1-9][0-9]*", ident) or ident in discovered:
            continue
        discovered.add(ident)
        try:
            meta = api.request(f"/repos/{REPO}/actions/runs/{ident}")
            if str(meta.get("id")) != ident:
                raise ValueError("discovered run identity mismatch")
            # During a rerun the previous aggregate still exists. It does not
            # attest the running attempt; wait for its completion notification.
            if meta.get("status") == "completed":
                runs.append(meta)
        except (ValueError, KeyError, TypeError, OSError):
            errors.append({"id": f"requested-run:{ident}",
                           "summary": f"Recently uploaded aggregate run {ident} is unavailable; no execution was inferred."})
    if selected_run:
        if not re.fullmatch(r"[1-9][0-9]*", str(selected_run)):
            raise ValueError("requested run ID must be a positive integer")
        if not any(str(r["id"]) == str(selected_run) for r in runs):
            try:
                selected = api.request(f"/repos/{REPO}/actions/runs/{selected_run}")
                if str(selected.get("id")) != str(selected_run):
                    raise ValueError("requested run identity mismatch")
                runs.insert(0, selected)
            except (ValueError, KeyError, TypeError, OSError):
                errors.append({"id": f"requested-run:{selected_run}",
                               "summary": f"Requested canary run {selected_run} is unavailable or rejected; no execution was inferred."})
    for meta in runs:
        explicit = str(meta["id"]) == str(selected_run)
        try:
            if meta.get("status") != "completed":
                if explicit and diagnostics is not None:
                    diagnostics["trigger"] = "attempt_not_completed"
                continue
            # created_at belongs to the original run, not its latest attempt.
            # Explicit selection has no history-window cutoff; stale observations
            # remain visible as stale and cannot supply fresh recovery evidence.
            executed_at = meta.get("run_started_at") or meta["created_at"]
            if not explicit and timestamp(executed_at) < now - timedelta(days=16):
                continue
            if (meta.get("head_branch") not in ("main", "dev") or meta.get("event") == "pull_request"
                    or meta.get("head_repository", {}).get("full_name") != REPO or meta.get("path") != CANARY):
                if explicit:
                    raise ValueError("requested run is outside trusted provenance")
                continue
            artifacts = api.pages(f"actions/runs/{meta['id']}/artifacts", "artifacts", max_pages=2)
            matches = [a for a in artifacts if a["name"] == "compatibility-results" and not a["expired"]]
            if not matches:
                if explicit:
                    raise ValueError("requested aggregate is unavailable/expired")
                # Heartbeat detects a missing expected sweep, never invent a pass.
                continue
            if len(matches) != 1 or matches[0]["size_in_bytes"] > MAX_ARCHIVE:
                raise ValueError("duplicate/oversized aggregate artifact")
            archive = api.request(f"/repos/{REPO}/actions/artifacts/{matches[0]['id']}/zip", binary=True)
            with tempfile.TemporaryDirectory(prefix="compat-results-") as tmp:
                root = Path(tmp)
                extract_archive(archive, root)
                bundle = load_json(safe_path(root, "results.json"))
                schema_validate(bundle, "results.schema.json")
                raw = safe_path(root, "compat-plan.json").read_bytes()
                if bundle["run"]["self_test"]:
                    # Authenticate the completed trigger, original plan and flag
                    # before ignoring it. A self-test is a diagnostic, never an
                    # invalid-production-run incident (nor a heartbeat trigger).
                    verify_provenance(bundle, meta, api, allow_self_test=True)
                    plan = validate_plan_snapshot(raw)
                    if (hashlib.sha256(raw).hexdigest() != bundle["run"]["plan_sha256"] or
                            plan["source_sha"] != bundle["run"]["source_sha"] or
                            plan["workflow_sha"] != bundle["run"]["workflow_sha"] or not plan["self_test"]):
                        raise ValueError("self-test plan identity mismatch")
                    if explicit and diagnostics is not None:
                        diagnostics["trigger"] = "authenticated_self_test"
                    continue
                verify_provenance(bundle, meta, api)
                with tempfile.TemporaryDirectory(prefix="compat-trusted-suite-") as source:
                    historical = snapshot_registry(api, bundle["run"]["source_sha"], Path(source))
                    validate_bundle(bundle, historical, now, artifact_root=root, plan_bytes=raw)
                    source_root = snapshot_source(api, bundle, historical, Path(source))
                    canonical_validate(bundle, historical, root, raw, source_root)
                if plans is not None:
                    plans[bundle["run"]["run_id"]] = validate_plan_snapshot(raw)
                bundles.append(bundle)
                if explicit and diagnostics is not None:
                    diagnostics["trigger"] = "production"
        except (ValueError, KeyError, TypeError, AttributeError, IndexError, OSError, RecursionError, OverflowError, zipfile.BadZipFile) as exc:
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


def write_outputs(output, runs, registry, now, issues=(), extra_health=(), fixture=False, baseline=False, *, plans=None):
    output.mkdir(parents=True, exist_ok=True)
    health = heartbeat(runs, registry, now) + list(extra_health)
    incidents = reconcile(runs, issues, registry, now, health, plans=plans)
    index = build_index(runs, registry, now, fixture=fixture, states=incidents["states"], issues=issues)
    index["health"] = health + incidents["health"]
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
    parser.add_argument("--source-root", type=Path, default=ROOT,
                        help="Trusted static source snapshot for offline strict artifact replay (never executed)")
    parser.add_argument("--github", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--trigger-run-id", help="workflow_run trigger; fail closed on mutations unless this exact run is validated production evidence")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.trigger_run_id and (not args.github or args.run_id or args.fixtures or args.baseline or args.artifact_root):
        parser.error("--trigger-run-id requires exclusive --github mode")
    now = timestamp(args.now) if args.now else datetime.now(timezone.utc)
    registry = load_json(ROOT / ".github/compat/registry.json")
    runs, issues, errors, plans = [], [], [], {}
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
        raw = safe_path(root, "compat-plan.json").read_bytes()
        historical = load_json(safe_path(args.source_root.resolve(), ".github/compat/registry.json"))
        schema_validate(historical, "registry.schema.json")
        trusted_validator().validate_registry(historical)
        validate_bundle(bundle, historical, now, artifact_root=root, plan_bytes=raw)
        canonical_validate(bundle, historical, root, raw, args.source_root.resolve())
        plans[bundle["run"]["run_id"]] = validate_plan_snapshot(raw)
        runs = [bundle]
    elif args.github:
        api = GitHub()
        diagnostics = {}
        runs, errors = github_results(api, registry, now, args.trigger_run_id or args.run_id, plans=plans, diagnostics=diagnostics)
        if args.apply:
            if (os.environ.get("COMPAT_REPORT_ALLOW_WRITES") != "true" or
                    os.environ.get("GITHUB_REF") != "refs/heads/main" or
                    os.environ.get("GITHUB_REPOSITORY") != REPO or
                    os.environ.get("GITHUB_EVENT_NAME") not in ("workflow_run", "workflow_dispatch", "schedule")):
                raise ValueError("issue writes require the trusted default-branch workflow guard")
        trigger = diagnostics.get("trigger", "unverified_trigger")
        production = trigger == "production"
        # workflow_run of the default self_test=true dispatch must not drive any
        # issue lifecycle, including unrelated heartbeat creation/closure. Unknown
        # trigger intent is fail-closed too; retain diagnostics for operator review.
        # An explicitly selected authenticated self-test is equally non-actionable.
        if args.trigger_run_id or trigger == "authenticated_self_test":
            args.output.mkdir(parents=True, exist_ok=True)
            decision = {"run_id": args.trigger_run_id or args.run_id, "production": production, "reason": trigger}
            (args.output / "trigger-decision.json").write_text(json.dumps(decision, indent=2) + "\n")
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                    output.write("production=" + str(production).lower() + "\n")
            if not production:
                (args.output / "incident-actions.json").write_text(json.dumps({"dry_run": True, "actions": []}) + "\n")
                (args.output / "trigger-diagnostics.json").write_text(json.dumps({"errors": errors}, indent=2) + "\n")
                print(json.dumps({"dry_run": True, "trigger": trigger, "incident_actions": 0, "output": str(args.output)}))
                return
        elif os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                output.write("production=true\n")  # Explicit production schedule/manual reporter invocation.
        # Read-only incident/repair-PR context belongs to normal artifacts too.
        # Only --apply below can mutate labels/issues. Fixture and branch paths
        # never construct an API client or read/write repository issue state.
        issues = api.pages("issues?state=all")
    elif not args.baseline:
        parser.error("choose --fixtures, --artifact-root, --github or --baseline")
    if args.apply and (not args.github or args.fixtures or args.artifact_root or args.baseline):
        raise ValueError("fixtures/branch artifacts are dry-run only")
    incidents = write_outputs(args.output, runs, registry, now, issues, errors, args.fixtures, args.baseline, plans=plans)
    if args.apply:
        apply_actions(api, incidents["actions"])
        (args.output / "incident-actions.json").write_text(json.dumps(
            {"dry_run": False, "actions": incidents["actions"]}, indent=2, sort_keys=True) + "\n")
        (args.output / "applied.json").write_text(json.dumps({"actions_applied": len(incidents["actions"])}) + "\n")
    print(json.dumps({"dry_run": not args.apply, "validated_runs": len(runs), "health_errors": len(errors),
                      "incident_actions": len(incidents["actions"]), "output": str(args.output)}))


if __name__ == "__main__":
    main()
