#!/usr/bin/env python3
"""Bounded registry-derived plans and one-time, hash-locked Python resolution.

A plan is written once after discovery/build/resolution. Jobs and retries consume
those exact bytes and wheel hashes; they never ask a registry for 'latest'.
Native npm/Go floating execution remains disabled pending qualification.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 boundary runner
    import tomli as tomllib
import urllib.parse
import urllib.request
from pathlib import Path

import jsonschema

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from discover import UTC, eligible, fetch, iso, summarize, timestamp, usable_artifact


def runtime_identity(python):
    # The clean setup interpreter has pip, not necessarily packaging installed.
    code = ("import json,platform; from pip._vendor.packaging.tags import sys_tags; "
            "print(json.dumps(dict(version=platform.python_version(),implementation=platform.python_implementation().lower(),"
            "os=platform.system().lower(),arch=platform.machine(),tags=[str(t) for t in sys_tags()])))")
    return json.loads(subprocess.check_output([python, "-I", "-c", code], text=True, env=clean_env()))


def semantic_validator(root):
    """Use PR1's current semantic contract interface, not a divergent validator.

    Source is an immutable trusted repository checkout, never artifact code. The
    pending foundation lacks this interface and is explicitly blocked.
    """
    import importlib.util
    path = root / ".github/compat/validate.py"
    if not path.is_file():
        raise ValueError("PR1 semantic contract validator is required")
    if sys.version_info < (3, 11):
        sys.modules.setdefault("tomllib", tomllib)
    spec = importlib.util.spec_from_file_location("compat_source_validation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

TOOLS = ["pip==25.0.1", "pytest==8.3.5", "pytest-asyncio==0.25.3", "httpx==0.28.1"]
MAX_CASES = 48


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def safe_path(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("Invalid relative artifact path")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Unsafe relative path")
    full = root / path
    for part in [full, *full.parents]:
        if part == root.parent:
            break
        if part.is_symlink():
            raise ValueError("Symlink paths are forbidden")
    if not full.resolve().is_relative_to(root.resolve()):
        raise ValueError("Escaping path")
    return full


def clean_env():
    # No GitHub/vendor/publish credentials, PYTHONPATH, pip config or proxy secrets.
    result = {k: os.environ[k] for k in ("PATH", "HOME", "SYSTEMROOT", "TMPDIR") if k in os.environ}
    result.update(PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_CACHE_DIR="1",
                  PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    return result


def run(command, cwd, log):
    completed = subprocess.run(command, cwd=cwd, env=clean_env(), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, timeout=600)
    log.write_text(completed.stdout)
    if completed.returncode:
        # Do not inject installation log content into a public failure signature.
        raise ValueError(f"{Path(command[0]).name} {command[1]} failed (exit {completed.returncode}); see {log.name}")
    return completed.stdout


def validate_plan(plan):
    schema = json.loads(Path(__file__).with_name("plan.schema.json").read_text())
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(plan)
    ids = [c["case_id"] for c in plan["cases"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate planned case IDs")
    if timestamp(plan["generated_at"]) - timestamp(plan["cooldown_cutoff"]) != dt.timedelta(hours=72):
        raise ValueError("Plan cutoff is not 72 hours")
    for case in plan["cases"]:
        if case["runtime_identity"]["version"].split(".")[:2] != case["runtime"].split("."):
            raise ValueError("Runtime label differs from actual frozen patch identity")
        if case["non_blocking"] != (case["selector"] in ("prerelease", "historical")):
            raise ValueError("Only explicit exploratory cells are non-blocking")
    return plan


def verify_metadata(registry, root):
    """Fail declaration drift instead of silently using an obsolete second inventory."""
    for target in registry["integrations"]:
        if not target["canary"]["enabled"]:
            continue
        if target["language"] != "python":
            raise ValueError("Native floating execution is not yet qualified")
        for dep in target["upstream"]:
            source = dep["range_source"]
            if source["kind"] not in ("metadata", "optional"):
                raise ValueError("Pilot dependency needs a metadata declaration")
            value = tomllib.loads(safe_path(root, source["path"]).read_text())
            for key in source["key"].split("."):
                value = value[key]
            value = value["version"] if isinstance(value, dict) else value
            if value != dep["declared_range"]:
                raise ValueError(f"Registry declaration drift: {target['id']} {dep['name']}")


def generate(registry, discovery, source_sha, workflow_sha, now, mode="stable", self_test=False, allowlist=None, runtime_identities=None):
    if not re.fullmatch("[0-9a-f]{40}", source_sha) or not re.fullmatch("[0-9a-f]{40}", workflow_sha):
        raise ValueError("Immutable source/workflow SHAs required")
    cutoff = now - dt.timedelta(hours=72)
    enabled_ids = {t["id"] for t in registry["integrations"] if t["canary"]["enabled"]}
    if allowlist and (mode != "weekly" or not isinstance(allowlist, dict) or set(allowlist) - enabled_ids):
        raise ValueError("Exploration requires weekly mode and explicitly enabled integration IDs")
    packages = {canonicalize_name(p["name"]): p for p in discovery["packages"] if p["ecosystem"] == "pypi"}
    core = next(t for t in registry["integrations"] if t["language"] == "python" and t["role"] == "core")
    cases = []
    for target in sorted(registry["integrations"], key=lambda t: t["id"]):
        if not target["canary"]["enabled"]:
            continue
        if target["language"] != "python":
            raise ValueError("npm/Go discovery is implemented; floating execution is deliberately disabled")
        if target["qualification"]["status"] not in ("qualified", "pilot_pending"):
            raise ValueError("Unqualified target cannot float")
        if not target["upstream"]:
            raise ValueError("Enabled target has no upstream")
        runtimes = target["runtimes"]["representative"][:1]
        if mode == "weekly":
            runtimes = sorted(set(runtimes + target["runtimes"]["boundaries"]))
        selectors = ["latest_eligible"] + (["floor"] if mode == "weekly" else [])
        explorations = (allowlist or {}).get(target["id"], {}) if mode == "weekly" else {}
        if explorations:
            # Explicit per-upstream specifiers form the allowlist. No automatic major rewrite.
            if set(explorations) != {d["name"] for d in target["upstream"]}:
                raise ValueError("Exploratory allowlist must name the complete upstream tuple")
            selectors.append("prerelease")
        for runtime in runtimes:
            identity = (runtime_identities or {}).get(runtime)
            if not identity or identity["version"].split(".")[:2] != runtime.split("."):
                raise ValueError("Actual runtime patch/platform identity required for " + runtime)
            if not SpecifierSet(target["runtimes"]["declared"]).contains(identity["version"]):
                continue
            for selector in selectors:
                modes = ["source"]
                if mode == "weekly" and selector == "latest_eligible" and runtime == target["runtimes"]["representative"][0]:
                    modes += ["published"]
                for package_mode in modes:
                    if package_mode not in target["canary"]["modes"]:
                        continue
                    selected, records, problems = [], [], []
                    for dep in target["upstream"]:
                        package = packages[canonicalize_name(dep["name"])]
                        summary = summarize(package, cutoff, identity, dep["declared_range"])
                        version = summary["latest_eligible"]
                        if selector == "floor":
                            version = dep["exact_floor"]
                        elif selector == "prerelease":
                            choices = [r for r in package["releases"] if eligible(r, cutoff, identity)
                                       and SpecifierSet(explorations[dep["name"]]).contains(r["version"], prereleases=True)]
                            version = choices[-1]["version"] if choices else None
                        release = next((r for r in package["releases"] if r["version"] == version), None)
                        if not release or not eligible(release, cutoff, identity):
                            problems.append(f"{dep['name']}: exact {selector} selection unavailable/ineligible")
                        if dep["declared_range"] is None:
                            problems.append(f"{dep['name']}: undeclared support range")
                        selected.append({"name": dep["name"], "role": "upstream" if dep["role"] == "primary" else "companion",
                                         "requirement": dep["name"] + "==" + (version or "unavailable"), "selected_version": version})
                        records.append({"name": dep["name"], "absolute_latest": summary["absolute_latest"],
                                        "latest_eligible": summary["latest_eligible"], "requested_version": version,
                                        "resolved_version": None, "published_at": release["published_at"] if release else None,
                                        "first_seen_at": release["first_seen_at"] if release else None,
                                        "yanked": release["yanked"] if release else False})
                    for role, entry in (("instrumentation", target), ("core", core)):
                        if package_mode == "source":
                            version = None  # Filled from built wheel METADATA, not an inferred version.
                        else:
                            p = packages[canonicalize_name(entry["distribution"])]
                            version = summarize(p, cutoff, identity)["latest_eligible"]
                            if version is None:
                                problems.append(f"No eligible published {entry['distribution']}")
                        selected.append({"name": entry["distribution"], "role": role,
                                         "requirement": entry["distribution"] + ("==" + version if version else "@source:" + source_sha),
                                         "selected_version": version})
                    actual_selector = selector
                    if selector == "prerelease" and not any(r["selected_version"] and Version(r["selected_version"]).is_prerelease for r in selected[:len(target["upstream"])]):
                        actual_selector = "historical"  # Explicit stable next-major/other exploratory tuple, not a prerelease claim.
                    case_id = f"{target['id']}-{package_mode}-{actual_selector}-py{runtime.replace('.', '')}"
                    cases.append({"case_id": case_id, "integration_id": target["id"], "mode": package_mode,
                                  "selector": actual_selector, "runtime": runtime, "runtime_identity": identity,
                                  "non_blocking": selector == "prerelease",
                                  "requested": selected, "discovery": records, "problems": problems,
                                  "resolution": None})
    if not cases or len(cases) > MAX_CASES:
        raise ValueError("Empty or unbounded canary matrix")
    return {"schema_version": 1, "source_sha": source_sha, "workflow_sha": workflow_sha,
            "generated_at": iso(now), "cooldown_hours": 72, "cooldown_cutoff": iso(cutoff),
            "cadence": mode, "self_test": self_test, "cases": cases}


def build_sources(root, output, registry):
    """Build development core and enabled instrumentation once, never editable."""
    import zipfile
    from email.parser import BytesParser
    wheels = output / "built"
    wheels.mkdir()
    entries = [t for t in registry["integrations"] if (t["language"] == "python" and t["role"] == "core") or t["canary"]["enabled"]]
    for target in entries:
        run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheels),
             str(safe_path(root, target["package_dir"]))], output, output / (target["id"] + "-build.log"))
    result = {}
    for path in wheels.glob("*.whl"):
        with zipfile.ZipFile(path) as archive:
            name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
            metadata = BytesParser().parsebytes(archive.read(name))
            result[canonicalize_name(metadata["Name"])] = (metadata["Version"], path)
    return result


def resolve(case, root, output, built, python):
    """One resolver invocation captures the COMPLETE closure, including test tools.

    Fetch only the report's exact binary URLs and hashes. Install jobs run offline
    from this wheelhouse using --require-hashes; no second registry resolution.
    """
    target = next(t for t in json.loads((root / ".github/compat/registry.json").read_text())["integrations"]
                  if t["id"] == case["integration_id"])
    contract = target["contract_test"]
    if not contract or any(not safe_path(root, path).is_file() for path in contract["paths"]):
        case["problems"].append("Missing real-SDK contract; requires PR1 qualification contract")
    if case["problems"]:
        return
    folder = output / "wheels" / case["case_id"]
    folder.mkdir(parents=True)
    report = output / "locks" / (case["case_id"] + ".report.json")
    report.parent.mkdir(exist_ok=True)
    requirements = []
    for req in case["requested"]:
        if case["mode"] == "source" and req["role"] in ("core", "instrumentation"):
            version, path = built[canonicalize_name(req["name"])]
            req["selected_version"] = version
            req["requirement"] = req["name"] + "==" + version
            requirements.append(str(path))
        else:
            requirements.append(req["requirement"])
    # Resolve LangChain core/graph together as the exact selected tuple, including
    # exact historical floors. An impossible pair remains blocked; never bump it.
    command = [python, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--only-binary=:all:",
               "--report", str(report), *requirements, *TOOLS]
    run(command, output, output / (case["case_id"] + "-resolve.log"))
    data = json.loads(report.read_text())
    snapshot = json.loads((output / "discovery.json").read_text())
    packages = {canonicalize_name(p["name"]): p for p in snapshot["packages"]}
    cutoff = timestamp(snapshot["discovered_at"]) - dt.timedelta(hours=72)  # same frozen planning instant
    names, lock, resolved = set(), [], []
    for item in data["install"]:
        name, version = canonicalize_name(item["metadata"]["name"]), item["metadata"]["version"]
        if name in names:
            raise ValueError("Duplicate distribution in resolver report")
        names.add(name)
        url = item["download_info"]["url"]
        digest = item["download_info"]["archive_info"]["hashes"]["sha256"]
        parsed = urllib.parse.urlparse(url)
        filename = Path(urllib.parse.unquote(parsed.path)).name
        if not filename.endswith(".whl") or not re.fullmatch("[0-9a-f]{64}", digest):
            raise ValueError("Non-wheel or unhashed resolution")
        destination = folder / filename
        wanted = next((r for r in case["requested"] if canonicalize_name(r["name"]) == name), None)
        if wanted and not (case["mode"] == "source" and wanted["role"] in ("core", "instrumentation")):
            release = next((r for r in packages[name]["releases"] if Version(r["version"]) == Version(version)), None)
            artifact = next((f for f in release["artifacts"] if f["filename"] == filename and f["sha256"] == digest), None) if release else None
            if not artifact or not usable_artifact(artifact, cutoff, case["runtime_identity"]):
                raise ValueError("Resolved upstream artifact is unusable/yanked/unknown or inside the 72-hour cooldown")
        if parsed.scheme == "file":
            source = Path(urllib.parse.unquote(parsed.path)).resolve()
            if not source.is_relative_to((output / "built").resolve()):
                raise ValueError("Unexpected local dependency URL")
            shutil.copyfile(source, destination)
        elif parsed.scheme == "https" and parsed.hostname == "files.pythonhosted.org":
            with urllib.request.urlopen(url, timeout=60) as response, destination.open("wb") as out:
                if urllib.parse.urlparse(response.url).hostname != "files.pythonhosted.org":
                    raise ValueError("Untrusted package redirect")
                shutil.copyfileobj(response, out)
        else:
            raise ValueError("Only built wheels and the public PyPI file host are allowed")
        if sha256(destination.read_bytes()) != digest:
            raise ValueError("Wheel digest mismatch")
        lock.append(f"{name}=={version} --hash=sha256:{digest}")
        for wanted in case["requested"]:
            if canonicalize_name(wanted["name"]) == name:
                if Version(wanted["selected_version"]) != Version(version):
                    raise ValueError("requested_resolved_mismatch")
                resolved.append({"name": wanted["name"], "role": wanted["role"], "version": version, "artifact_sha256": digest})
    if len(resolved) != len(case["requested"]):
        raise ValueError("requested_resolved_mismatch: missing direct distribution")
    lock_path = output / "locks" / (case["case_id"] + ".txt")
    lock_path.write_text("\n".join(sorted(lock)) + "\n")
    case["resolution"] = {"resolved": resolved, "report": report.relative_to(output).as_posix(),
                          "requirements": lock_path.relative_to(output).as_posix(),
                          "requirements_sha256": sha256(lock_path.read_bytes()),
                          "wheelhouse": folder.relative_to(output).as_posix(),
                          "report_sha256": sha256(report.read_bytes())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--discovery", required=True, type=Path)
    parser.add_argument("--mode", choices=["stable", "weekly"], default="stable")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--now")
    parser.add_argument("--resolve", action="store_true")
    parser.add_argument("--allowlist", type=Path, help="Explicit experimental upstream specifiers; default none")
    parser.add_argument("--python", action="append", default=[], metavar="VERSION=EXECUTABLE")
    args = parser.parse_args()
    args.root, args.out = args.root.resolve(), args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    if (args.out / "compat-plan.json").exists():
        raise ValueError("Refusing to re-resolve/overwrite an immutable plan; reuse the original artifact")
    raw = (args.root / ".github/compat/registry.json").read_bytes()
    registry = json.loads(raw)
    verify_metadata(registry, args.root)
    discovery = json.loads(args.discovery.read_text())
    now = timestamp(discovery["discovered_at"])
    if args.now and timestamp(args.now) != now:
        raise ValueError("Discovery and planning must use one frozen eligibility cutoff")
    pythons = dict(p.split("=", 1) for p in args.python)
    identities = {version: runtime_identity(path) for version, path in pythons.items()}
    if not identities:
        identity = runtime_identity(sys.executable)
        identities[".".join(identity["version"].split(".")[:2])] = identity
    source = subprocess.check_output(["git", "-C", str(args.root), "rev-parse", "HEAD"], text=True).strip()
    workflow = os.environ.get("GITHUB_WORKFLOW_SHA", source)
    plan = generate(registry, discovery, source, workflow, now, args.mode, args.self_test,
                    json.loads(args.allowlist.read_text()) if args.allowlist else None, identities)
    plan["registry_sha256"] = sha256(raw)
    (args.out / "registry.json").write_bytes(raw)
    shutil.copyfile(args.discovery, args.out / "discovery.json")
    if args.resolve:
        try:
            built = build_sources(args.root, args.out, registry)
        except (ValueError, subprocess.SubprocessError, OSError) as error:
            built = {}
            for case in plan["cases"]:
                case["problems"].append("Source artifact build failed: " + str(error))
        pythons = dict(p.split("=", 1) for p in args.python)
        for case in plan["cases"]:
            try:
                interpreter = pythons.get(case["runtime"], sys.executable)
                if runtime_identity(interpreter) != case["runtime_identity"]:
                    raise ValueError("Resolver runtime does not match frozen patch/platform identity")
                resolve(case, args.root, args.out, built, interpreter)
            except (ValueError, subprocess.SubprocessError, OSError) as error:
                case["problems"].append(str(error))
    validate_plan(plan)
    write_json(args.out / "compat-plan.json", plan)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write("matrix=" + json.dumps({"include": [{"case_id": c["case_id"], "python": c["runtime_identity"]["version"],
                                                              "non_blocking": c["non_blocking"]} for c in plan["cases"]]}) + "\n")
            stream.write("source_sha=" + source + "\n")


if __name__ == "__main__":
    main()
