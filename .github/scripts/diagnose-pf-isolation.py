"""One-shot synthetic macOS PF/UID admission experiment, never an SDK waiver.

Run only on a disposable GitHub macos-14 runner via root env -i / python -I.
Reads the installed manual and effective root rules BEFORE making any change.
Only an already-referenced, unique direct com.apple child anchor may be changed.
Never loads /etc/pf.conf, flushes global rules/states, or disables a firewall.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_ALL": "C"}
p = argparse.ArgumentParser()
p.add_argument("--node", required=True)
p.add_argument("--source", required=True)
p.add_argument("--evidence", required=True)
p.add_argument("--nonce", required=True)
p.add_argument("--runner-uid", type=int, required=True)
a = p.parse_args()
if os.geteuid() != 0 or sys.platform != "darwin" or not re.fullmatch(r"[0-9]+-node(20|24)", a.nonce):
    raise SystemExit("root disposable macOS runner and validated nonce required")
source = Path(a.source).resolve()
evidence = Path(a.evidence).resolve()
evidence.mkdir(parents=True, exist_ok=False)
root = Path("/private/tmp") / ("traceai-pf-" + a.nonce)
anchor = "com.apple/000.traceai-" + a.nonce
user = "_traceaipf" + a.nonce.replace("-node", "n")
uid = None
token = None
account_created = group_created = anchor_touched = False
report = {"status": "BLOCKED", "stage": "read-only-admission", "anchor": anchor,
          "node_requested": a.nonce.split("node")[1], "tests": [], "cleanup": {},
          "policy_loaded": False, "sdk_jobs_started": 0, "failures_retained": True}

class Blocked(Exception):
    pass

def run(args, *, label=None, check=True, env=None, timeout=15, cwd="/private/tmp"):
    result = subprocess.run([str(v) for v in args], env=env or ENV, cwd=cwd,
                            capture_output=True, text=True, timeout=timeout, close_fds=True)
    if label:
        (evidence / (label + ".log")).write_text(result.stdout + result.stderr)
    if check and result.returncode:
        raise Blocked(f"{label or Path(str(args[0])).name}: exit {result.returncode}")
    return result

def pf(*args, **kwargs):
    return run(["/sbin/pfctl", *args], **kwargs)

def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()

def as_uid(target, args, **kwargs):
    env = dict(ENV, HOME=str(root / ("home" if target == uid else "runner-home")),
               TMPDIR=str(root / ("home" if target == uid else "runner-home")), PF_ANCHOR=anchor)
    # sudo itself gets no inherited env; the command gets only this explicit map.
    return run(["/usr/bin/sudo", "-n", "-u", f"#{target}", "/usr/bin/env", "-i",
                *[f"{key}={value}" for key, value in env.items()], *args], **kwargs)

def packets():
    result = pf("-a", anchor, "-vvsr")
    counts = re.findall(r"Packets:\s*(\d+)", result.stdout)
    if len(counts) != 4:
        raise Blocked("Cannot read one packet counter per four UID deny rules")
    return sum(map(int, counts))

def runner_network(label):
    result = as_uid(a.runner_uid, ["/usr/bin/curl", "--disable", "--noproxy", "*",
                    "--fail", "--silent", "--show-error", "--max-time", "10",
                    "--output", "/dev/null", "--write-out", "%{http_code}\n",
                    "https://github.com/robots.txt"], label=label, check=False)
    report[label] = {"uid": a.runner_uid, "exit": result.returncode, "http": result.stdout.strip()}
    if result.returncode or result.stdout.strip() != "200":
        raise Blocked(label + " did not demonstrate unaffected public GitHub connectivity")

try:
    run(["/usr/bin/sw_vers"], label="os")
    run([a.node, "--version"], label="node-version")
    for section, name in [(5, "pf.conf"), (8, "pfctl")]:
        run(["/usr/bin/man", str(section), name], env=dict(ENV, MANPAGER="cat", PAGER="cat", MANWIDTH="100"), label="installed-man-" + name, check=False)
    # This OS-owned public configuration is not applied or edited.
    (evidence / "default-pf.conf").write_text(Path("/etc/pf.conf").read_text())
    effective = pf("-sr", label="root-rules-before", check=False)
    info = pf("-s", "info", label="pf-info-before", check=False)
    if effective.returncode or info.returncode:
        report["blocker_class"] = "PF_DEVICE_OR_KERNEL_UNAVAILABLE"
        raise Blocked("Hosted PF status/rules cannot be read; no policy was loaded")
    root_rules = effective.stdout
    report["root_rules_sha256_before"] = digest(root_rules)
    root_lines = [line.strip() for line in root_rules.splitlines() if line.strip()]
    if root_lines != ['anchor "com.apple/*" all']:
        report["blocker_class"] = "NO_SAFE_ACTIVE_ANCHOR_HOOK"
        report["effective_root_rules"] = root_lines
        raise Blocked("Effective root rules are not the sole default com.apple/* filter hook; loading an orphan anchor cannot prove enforcement, and loading/replacing global rules is forbidden")
    recursive = pf("-a", "*", "-sr", label="recursive-rules-before")
    # Reject any existing pass/block rule: its quick/state semantics could exempt
    # this UID, and enabling a previously-disabled existing policy could affect runner traffic.
    if re.search(r"^\s*(pass|block)\b", recursive.stdout, re.MULTILINE):
        report["blocker_class"] = "EXISTING_FILTER_RULES_REQUIRE_GLOBAL_ANALYSIS"
        raise Blocked("Existing effective filtering prevents a bounded UID-only admission")
    pf("-s", "Interfaces", "-v", label="interfaces-before")
    existing = pf("-a", anchor, "-sr")
    if existing.stdout.strip():
        raise Blocked("Unique candidate anchor already has rules; refusing ownership")
    if root.exists():
        raise Blocked("Unique temporary directory already exists")
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)
    for name in ["home", "runner-home", "bin"]:
        (root / name).mkdir(mode=0o755 if name == "bin" else 0o700)
    os.chown(root / "runner-home", a.runner_uid, -1)
    runner_network("runner-connectivity-before")
    # Choose a wholly unused numerical UID/GID; no existing account is modified.
    for candidate in range(61000, 61010):
        found_user = run(["/usr/bin/dscl", ".", "-search", "/Users", "UniqueID", str(candidate)], check=False)
        found_group = run(["/usr/bin/dscl", ".", "-search", "/Groups", "PrimaryGroupID", str(candidate)], check=False)
        if found_user.returncode == 0 and found_group.returncode == 0 and not found_user.stdout.strip() and not found_group.stdout.strip():
            uid = candidate
            break
    if uid is None or uid == a.runner_uid:
        raise Blocked("No verified unused test UID/GID")
    report["test_uid"] = uid
    if run(["/usr/bin/dscl", ".", "-read", "/Users/" + user], check=False).returncode == 0 or run(["/usr/bin/dscl", ".", "-read", "/Groups/" + user], check=False).returncode == 0:
        raise Blocked("Unique account name already exists")
    run(["/usr/bin/dscl", ".", "-create", "/Groups/" + user])
    group_created = True
    run(["/usr/bin/dscl", ".", "-create", "/Groups/" + user, "PrimaryGroupID", str(uid)])
    run(["/usr/bin/dscl", ".", "-create", "/Users/" + user])
    account_created = True
    for key, value in [("UniqueID", str(uid)), ("PrimaryGroupID", str(uid)),
                       ("UserShell", "/usr/bin/false"), ("NFSHomeDirectory", str(root / "home")),
                       ("IsHidden", "1"), ("AuthenticationAuthority", ";DisabledUser;")]:
        run(["/usr/bin/dscl", ".", "-create", "/Users/" + user, key, value])
    os.chown(root / "home", uid, uid)
    # No host credentials, inherited descriptors or writable trusted launcher.
    node = root / "bin/node"
    shutil.copyfile(a.node, node)
    os.chmod(node, 0o755)
    for filename in ["diagnose-loopback.cjs", "diagnose-pf-isolation.cjs"]:
        shutil.copyfile(source / filename, root / filename)
        os.chmod(root / filename, 0o644)
    rules = "\n".join(f'block return out quick {family} proto {protocol} from any to ! {loopback} user {uid} label "traceai-{family}-{protocol}"'
                      for family, loopback in [("inet", "127.0.0.0/8"), ("inet6", "::1")]
                      for protocol in ["tcp", "udp"]) + "\n"
    rules_path = root / "rules.pf"
    rules_path.write_text(rules)
    (evidence / "candidate-rules.pf").write_text(rules)
    dry = pf("-n", "-a", anchor, "-f", rules_path, label="parse-rules", check=False)
    if dry.returncode:
        report["blocker_class"] = "UID_RULE_SYNTAX_UNSUPPORTED"
        raise Blocked("Hosted PF cannot parse narrowly UID-scoped IPv4/IPv6 TCP/UDP rules")
    anchor_touched = True
    pf("-a", anchor, "-f", rules_path, label="load-anchor")
    enabled = pf("-E", label="enable-reference", check=False)
    match = re.search(r"Token\s*:\s*(\d+)", enabled.stdout + enabled.stderr)
    if match:
        token = match.group(1)
    if enabled.returncode or token is None:
        raise Blocked("PF did not provide a releasable enable reference; no test process launched")
    report["policy_loaded"] = True
    report["effective_boundary"] = rules
    installed = pf("-a", anchor, "-sr", label="installed-rules").stdout
    report["installed_rules_sha256"] = digest(installed)
    if pf("-sr").stdout != root_rules:
        raise Blocked("Root rules changed concurrently; no test process launched")
    report["stage"] = "cold-builtin-census"
    cold = as_uid(uid, [str(node), "--require", str(root / "diagnose-pf-isolation.cjs"), str(root / "diagnose-loopback.cjs"), "pf-test-uid"], label="cold-census", check=False, timeout=80, cwd=str(root / "home"))
    report["cold_census_exit"] = cold.returncode
    if cold.returncode:
        raise Blocked("Cold unchanged synthetic loopback census failed; no retries")
    report["stage"] = "positive-and-negative-controls"
    failures = []
    for mode in ["direct", "child"]:
        for operation in ["positive", "privileges", "external-tcp4", "external-udp4", "external-dns4", "external-tcp6", "external-udp6", "external-dns6", "local-tcp4", "local-udp4", "local-tcp6", "local-udp6"]:
            name = mode + "-" + operation
            before = packets()
            result = as_uid(uid, [str(node), str(root / "diagnose-pf-isolation.cjs"), mode, operation, str(a.runner_uid)], label=name, check=False, timeout=12, cwd=str(root / "home"))
            after = packets()
            unchanged = pf("-a", anchor, "-sr").stdout == installed
            negative = operation not in ["positive", "privileges"]
            passed = result.returncode == 0 and unchanged and (not negative or after > before)
            record = {"name": name, "exit": result.returncode, "block_packets": after - before, "policy_unchanged": unchanged, "passed": passed}
            report["tests"].append(record)
            if not passed:
                failures.append(name)
    pf("-a", anchor, "-vvsr", label="rule-counters-final")
    runner_network("runner-connectivity-during")
    if failures:
        report["blocker_class"] = "CONTROL_ENFORCEMENT_NOT_DEMONSTRATED"
        raise Blocked("Failed controls retained: " + ", ".join(failures))
    # Prototype admission is not independent qualification or an SDK matrix pass.
    report["status"] = "SAFE_CANDIDATE"
    report["stage"] = "prototype-complete"
except (Blocked, OSError, subprocess.TimeoutExpired) as error:
    report["reason"] = str(error)
finally:
    errors = []
    if account_created:
        try:
            stopped = run(["/usr/bin/pkill", "-KILL", "-u", str(uid)], check=False)
            if stopped.returncode not in [0, 1]:
                errors.append("test-process-termination")
            remaining = run(["/bin/ps", "-U", str(uid), "-o", "pid="], check=False)
            report["cleanup"]["test_uid_processes_remaining"] = bool(remaining.stdout.strip())
            if remaining.stdout.strip():
                errors.append("test-processes-remain")
        except Exception as error:
            errors.append("process-cleanup:" + str(error))
    if anchor_touched:
        try:
            pf("-a", anchor, "-F", "rules", label="cleanup-owned-anchor")
            report["cleanup"]["owned_anchor_empty"] = not pf("-a", anchor, "-sr").stdout.strip()
        except Exception as error:
            errors.append("anchor-cleanup:" + str(error))
    if token:
        try:
            pf("-X", token, label="release-owned-reference")
            report["cleanup"]["enable_reference_released"] = True
        except Exception as error:
            errors.append("token-cleanup:" + str(error))
    for created, kind in [(account_created, "Users"), (group_created, "Groups")]:
        if created:
            try:
                run(["/usr/bin/dscl", ".", "-delete", f"/{kind}/{user}"])
                report["cleanup"][kind.lower() + "_deleted"] = True
            except Exception as error:
                errors.append("account-cleanup:" + str(error))
    try:
        after_rules = pf("-sr", label="root-rules-after", check=False)
        if "root_rules" in globals() and not after_rules.returncode:
            report["cleanup"]["root_rules_unchanged"] = after_rules.stdout == root_rules
            if after_rules.stdout != root_rules:
                errors.append("root-rules-changed")
        pf("-s", "info", label="pf-info-after", check=False)
        if root.exists():
            runner_network("runner-connectivity-after")
            shutil.rmtree(root)
            report["cleanup"]["owned_directory_removed"] = True
    except Exception as error:
        errors.append("final-cleanup:" + str(error))
    report["cleanup"]["errors"] = errors
    report["cleanup"]["global_rules_loaded_flushed_or_firewall_disabled"] = False
    report["cleanup"]["account_created"] = account_created
    report["cleanup"]["anchor_touched"] = anchor_touched
    if errors:
        report["status"] = "BLOCKED"
    (evidence / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
sys.exit(0 if report["status"] == "SAFE_CANDIDATE" else 1)
