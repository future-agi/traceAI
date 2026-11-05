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
import runpy
import signal
import shutil
import subprocess
import sys

admission = runpy.run_path(str(Path(__file__).with_name("pf-admission.py")))

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
account_created = group_created = anchor_touched = root_created = False
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
        (evidence / (label + ".stdout")).write_text(result.stdout)
        (evidence / (label + ".stderr")).write_text(result.stderr)
    if check and result.returncode:
        raise Blocked(f"{label or Path(str(args[0])).name}: exit {result.returncode}")
    return result

def pf(*args, **kwargs):
    return run(["/sbin/pfctl", *args], **kwargs)

def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()

def as_uid(target, args, **kwargs):
    env = dict(ENV, HOME=str(root / ("home" if target == uid else "runner-home")),
               TMPDIR=str(root / ("home" if target == uid else "runner-home")), PF_ANCHOR=anchor,
               TEST_UID=str(uid), TEST_GID=str(uid))
    # sudo itself gets no inherited env; the command gets only this explicit map.
    return run(["/usr/bin/sudo", "-n", "-u", f"#{target}", "/usr/bin/env", "-i",
                *[f"{key}={value}" for key, value in env.items()], *args], **kwargs)

def packets(label):
    result = pf("-a", anchor, "-vvsr", label=label)
    return admission["packet_counts"](result.stdout)


def interrupted(signum, _frame):
    raise Blocked(f"Controller interrupted by signal {signum}; cleanup required")


signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)
# Bound controller runtime below the hosted job deadline, leaving cleanup time.
signal.signal(signal.SIGALRM, interrupted)
signal.alarm(360)

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
    report["root_admission"] = admission["admit_root"](root_rules)
    recursive = pf("-a", "*", "-sr", label="recursive-rules-before")
    report["recursive_admission"] = admission["admit_recursive"](recursive.stdout)
    translation = pf("-a", "*", "-sn", label="translation-rules-before")
    admission["admit_translation"](translation.stdout)
    states = pf("-ss", label="states-before")
    if states.stdout.strip():
        raise Blocked("Existing state entries could bypass rule evaluation; refusing admission")
    report["pf_initial_status"] = re.search(r"^Status:\s+(Enabled|Disabled)\b", info.stdout).group(1)
    references = pf("-s", "References", label="references-before")
    pf("-s", "Interfaces", "-v", label="interfaces-before")
    existing = pf("-a", anchor, "-sr")
    if existing.stdout.strip():
        raise Blocked("Unique candidate anchor already has rules; refusing ownership")
    if root.exists():
        raise Blocked("Unique temporary directory already exists")
    root.mkdir(mode=0o755)
    root_created = True
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
    existing_processes = run(["/bin/ps", "-U", str(uid), "-o", "pid="], check=False)
    if existing_processes.returncode not in [0, 1] or existing_processes.stdout.strip():
        raise Blocked("Unused account ID still has processes; refusing ownership")
    report["test_uid"] = uid
    report["stage"] = "dedicated-uid-setup"
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
    as_uid(uid, ["/usr/bin/id"], label="actual-test-identity")
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
    if pf("-sr").stdout != root_rules or pf("-a", "*", "-sr").stdout != recursive.stdout or pf("-a", "*", "-sn").stdout != translation.stdout or pf("-ss").stdout.strip():
        raise Blocked("Global rules/state changed during setup; no policy loaded")
    anchor_touched = True
    pf("-a", anchor, "-f", rules_path, label="load-anchor")
    enabled = pf("-E", label="enable-reference", check=False)
    match = re.search(r"Token\s*:\s*(\d+)", enabled.stdout + enabled.stderr)
    if match:
        token = match.group(1)
    if enabled.returncode or token is None:
        raise Blocked("PF did not provide a releasable enable reference; no test process launched")
    active = pf("-s", "info", label="pf-info-active")
    if not re.search(r"^Status:\s+Enabled\b", active.stdout):
        raise Blocked("PF enable reference did not enable filtering")
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
            before = packets(name + "-counters-before")
            result = as_uid(uid, [str(node), str(root / "diagnose-pf-isolation.cjs"), mode, operation, str(a.runner_uid)], label=name, check=False, timeout=12, cwd=str(root / "home"))
            after = packets(name + "-counters-after")
            unchanged = pf("-a", anchor, "-sr").stdout == installed
            negative = operation not in ["positive", "privileges"]
            key = "traceai-" + ("inet6" if operation.endswith("6") else "inet") + "-" + ("tcp" if "tcp" in operation else "udp")
            delta = {label: after[label] - before[label] for label in before}
            # Timeout/no route is never evidence of denial: this exact family's
            # protocol rule must account for packets in this isolated invocation.
            attributed = delta.get(key, 0) > 0 and all(value == 0 for label, value in delta.items() if label != key)
            passed = result.returncode == 0 and unchanged and (not negative or attributed)
            record = {"name": name, "exit": result.returncode, "block_packets": sum(delta.values()), "per_rule_delta": delta, "expected_rule": key if negative else None, "policy_unchanged": unchanged, "passed": passed}
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
except (Blocked, OSError, ValueError, AttributeError, subprocess.TimeoutExpired) as error:
    report["reason"] = str(error)
finally:
    signal.alarm(0)
    # Do not abandon cleanup upon a second termination request. Job VM expiry
    # is the ultimate bound; each cleanup subprocess remains individually timed.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
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
            if not report["cleanup"]["owned_anchor_empty"]:
                errors.append("owned-anchor-not-empty")
        except Exception as error:
            errors.append("anchor-cleanup:" + str(error))
    if token:
        try:
            pf("-X", token, label="release-owned-reference")
            remaining_refs = pf("-s", "References", label="references-after-release")
            report["cleanup"]["enable_reference_released"] = token not in remaining_refs.stdout
            if not report["cleanup"]["enable_reference_released"]:
                errors.append("owned-enable-token-remains")
        except Exception as error:
            errors.append("token-cleanup:" + str(error))
    for created, kind in [(account_created, "Users"), (group_created, "Groups")]:
        if created:
            try:
                run(["/usr/bin/dscl", ".", "-delete", f"/{kind}/{user}"])
                absent = run(["/usr/bin/dscl", ".", "-read", f"/{kind}/{user}"], check=False)
                report["cleanup"][kind.lower() + "_deleted"] = absent.returncode != 0 and "eDSRecordNotFound" in absent.stdout + absent.stderr
                if not report["cleanup"][kind.lower() + "_deleted"]:
                    errors.append(kind + "-deletion-not-verified")
            except Exception as error:
                errors.append("account-cleanup:" + str(error))
    try:
        after_rules = pf("-sr", label="root-rules-after", check=False)
        if "root_rules" in globals() and not after_rules.returncode:
            report["root_rules_sha256_after"] = digest(after_rules.stdout)
            report["cleanup"]["root_rules_unchanged"] = after_rules.stdout == root_rules
            if after_rules.stdout != root_rules:
                errors.append("root-rules-changed")
        elif "root_rules" in globals():
            errors.append("root-rules-after-unreadable")
        final_info = pf("-s", "info", label="pf-info-after")
        report["pf_final_status"] = re.search(r"^Status:\s+(Enabled|Disabled)\b", final_info.stdout).group(1)
        if "pf_initial_status" in report and report["pf_final_status"] != report["pf_initial_status"]:
            errors.append("pf-status-not-restored")
        if "recursive" in globals():
            final_recursive = pf("-a", "*", "-sr", label="recursive-rules-after")
            report["cleanup"]["recursive_rules_unchanged"] = final_recursive.stdout == recursive.stdout
            if not report["cleanup"]["recursive_rules_unchanged"]:
                errors.append("recursive-rules-changed")
        if "translation" in globals():
            final_translation = pf("-a", "*", "-sn", label="translation-rules-after")
            report["cleanup"]["translation_rules_unchanged"] = final_translation.stdout == translation.stdout
            if not report["cleanup"]["translation_rules_unchanged"]:
                errors.append("translation-rules-changed")
    except Exception as error:
        errors.append("final-policy-check:" + str(error))
    if root_created:
        try:
            runner_network("runner-connectivity-after")
        except Exception as error:
            errors.append("runner-after:" + str(error))
        # Directory removal must still run when final connectivity fails.
        try:
            shutil.rmtree(root)
            report["cleanup"]["owned_directory_removed"] = not root.exists()
        except Exception as error:
            errors.append("directory-cleanup:" + str(error))
    report["cleanup"]["errors"] = errors
    report["cleanup"]["global_rules_loaded_flushed_or_firewall_disabled"] = False
    report["cleanup"]["account_created"] = account_created
    report["cleanup"]["anchor_touched"] = anchor_touched
    if errors:
        report["status"] = "BLOCKED"
    (evidence / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
sys.exit(0 if report["status"] == "SAFE_CANDIDATE" else 1)
