#!/usr/bin/python3
"""One bounded hosted observation; NEVER an admission or enforcement controller.

Only the fixed identity/manual/PF read argv below can run. Named reads require a
complete clean inventory with explicit full paths. There is no command option,
policy operation, socket, account operation, SDK setup, or local test entrypoint.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone

SESSION_SECONDS = 300
COMMAND_SECONDS = 15
CLEANUP_SECONDS = 1
STDOUT_BYTES = 65536
STDERR_BYTES = 16384
MANUAL_BYTES = 262144
SOURCE_BYTES = 65536
MAX_ANCHORS = 64
MAX_DEPTH = 8
# Deliberately restrictive capture limits, NOT an assertion of Apple's ABI size.
MAX_COMPONENT_BYTES = 31
MAX_PATH_BYTES = 127
ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_ALL": "C"}
ALTQ = b"No ALTQ support in kernel\nALTQ related functions disabled\n"
ROOT_FILTER = b'anchor "com.apple/*" all\n'
ROOT_DEFAULT = b'scrub-anchor "com.apple/*" all fragment reassemble\n' + ROOT_FILTER
IDENTITY = (
    ("sw-vers", ("/usr/bin/sw_vers",)),
    ("uname", ("/usr/bin/uname", "-a")),
    ("kernel-version", ("/usr/sbin/sysctl", "-n", "kern.version")),
    ("pfctl-sha256", ("/usr/bin/shasum", "-a", "256", "/sbin/pfctl")),
    ("man-pfctl-path", ("/usr/bin/man", "-w", "8", "pfctl")),
    ("man-pf-conf-path", ("/usr/bin/man", "-w", "5", "pf.conf")),
)
ROOT_VIEWS = (
    ("info", ("/sbin/pfctl", "-s", "info")),
    ("root-rules", ("/sbin/pfctl", "-sr")),
    ("root-inventory", ("/sbin/pfctl", "-s", "Anchors", "-v")),
)
PARENT_INVENTORY = ("/sbin/pfctl", "-a", "com.apple", "-s", "Anchors")
MANUAL_PATHS = {
    "man-pfctl-path": {"/usr/share/man/man8/pfctl.8", "/usr/share/man/man8/pfctl.8.gz"},
    "man-pf-conf-path": {"/usr/share/man/man5/pf.conf.5", "/usr/share/man/man5/pf.conf.5.gz"},
}


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def clean(row):
    return row["clean"]


def path_check(path):
    raw = path.encode("ascii", "strict")
    parts = path.split("/")
    if not raw or len(raw) > MAX_PATH_BYTES or len(parts) > MAX_DEPTH:
        raise ValueError("anchor path exceeds conservative byte/depth bound")
    for part in parts:
        if (part in ("", ".", "..") or len(part) > MAX_COMPONENT_BYTES
                or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part)):
            raise ValueError("invalid, ambiguous, or over-bound anchor component")
    return parts


def inventory(raw, recursive, known=()):
    """Map only literal full paths; indentation is layout, never parentage."""
    if raw and not raw.endswith(b"\n"):
        raise ValueError("inventory lacks a complete final newline")
    rows = []
    offset = 0
    seen = set()
    for line_no, line in enumerate(raw.splitlines(keepends=True), 1):
        match = re.fullmatch(rb"( {0,16})([A-Za-z0-9_][A-Za-z0-9_./-]*)\n", line)
        if not match:
            raise ValueError("unknown inventory grammar at raw line " + str(line_no))
        path = match.group(2).decode("ascii", "strict")
        parts = path_check(path)
        if path in seen:
            raise ValueError("duplicate full path at raw line " + str(line_no))
        seen.add(path)
        if len(seen) > MAX_ANCHORS:
            raise ValueError("inventory exceeds 64 distinct anchors; no entries dropped")
        rows.append({"path": path, "depth": len(parts), "raw_line": line_no,
                     "raw_start": offset, "raw_end": offset + len(line),
                     "indent_spaces": len(match.group(1))})
        offset += len(line)
    if recursive:
        for path in seen:
            parts = path.split("/")
            for length in range(1, len(parts)):
                if "/".join(parts[:length]) not in seen:
                    raise ValueError("parentage unknown: literal ancestor missing from inventory")
    else:
        # Nonverbose data corroborates only; it is never the namespace authority.
        expected = {p for p in known if p.startswith("com.apple/") and p.count("/") == 1}
        if any(not p.startswith("com.apple/") or p.count("/") != 1 for p in seen):
            raise ValueError("direct list did not supply exact full child paths; no basename inference")
        if seen != expected:
            raise ValueError("direct child list differs from visible recursive inventory")
    return rows


class Recorder:
    def __init__(self, output):
        self.output = output
        self.start = time.monotonic()
        self.deadline = self.start + SESSION_SECONDS
        self.allowed = {argv for _, argv in IDENTITY + ROOT_VIEWS}
        self.rows = []
        self.bytes = {}
        self.halt = False

    def run(self, label, argv):
        argv = tuple(argv)
        if argv not in self.allowed:
            raise ValueError("argv outside explicit read-only allowlist")
        row = {"label": label, "argv": list(argv), "started_utc": utc(),
               "executed": False, "returncode": None, "signal": None,
               "timeout": False, "truncated": False, "complete": False,
               "clean": False, "error": None, "cleanup_complete": True,
               "stdout_bytes_seen": 0, "stderr_bytes_seen": 0,
               "stdout_limit": STDOUT_BYTES, "stderr_limit": STDERR_BYTES,
               "command_budget_seconds": COMMAND_SECONDS,
               "environment": ENV, "cwd": "/", "shell": False}
        data = {"stdout": bytearray(), "stderr": bytearray()}
        started = time.monotonic()
        hard_end = min(started + COMMAND_SECONDS, self.deadline)
        execute_end = hard_end - CLEANUP_SECONDS
        proc = None
        selector = selectors.DefaultSelector()
        killed = False
        ended_normally = False
        try:
            if self.halt or execute_end <= started:
                row["error"] = "owned-process cleanup incomplete" if self.halt else "session budget exhausted"
            else:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        env=ENV, cwd="/", close_fds=True,
                                        start_new_session=True, shell=False)
                row["executed"] = True
                for name, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, name)
                while time.monotonic() < hard_end:
                    if not selector.get_map() and proc.poll() is not None:
                        ended_normally = True
                        break
                    if time.monotonic() >= execute_end and not killed:
                        row["timeout"] = True
                        killed = True
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    for key, _ in selector.select(timeout=min(0.05, max(0, hard_end - time.monotonic()))):
                        try:
                            chunk = os.read(key.fileobj.fileno(), 8192)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                            continue
                        name = key.data
                        limit = STDOUT_BYTES if name == "stdout" else STDERR_BYTES
                        row[name + "_bytes_seen"] += len(chunk)
                        room = max(0, limit - len(data[name]))
                        data[name].extend(chunk[:room])
                        if len(chunk) > room:
                            row["truncated"] = True
                            if not killed:
                                killed = True
                                try:
                                    os.killpg(proc.pid, signal.SIGKILL)
                                except ProcessLookupError:
                                    pass
                row["returncode"] = proc.poll()
                row["cleanup_complete"] = row["returncode"] is not None and not selector.get_map()
                if not row["cleanup_complete"]:
                    row["error"] = "owned process or output pipes did not finish inside command budget"
                    if not killed:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    self.halt = True
        except Exception as exc:
            row["error"] = type(exc).__name__ + ": " + str(exc)
            if proc is not None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=max(0, min(CLEANUP_SECONDS, hard_end - time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
                row["returncode"] = proc.poll()
                row["cleanup_complete"] = row["returncode"] is not None
                self.halt = not row["cleanup_complete"]
        finally:
            for key in list(selector.get_map().values()):
                key.fileobj.close()
            selector.close()
        row["ended_utc"] = utc()
        row["elapsed_seconds"] = time.monotonic() - started
        if row["returncode"] is not None and row["returncode"] < 0:
            row["signal"] = -row["returncode"]
        streams = {name: bytes(value) for name, value in data.items()}
        row["decoding_errors"] = []
        for name, raw in streams.items():
            path = self.output / (label + "." + name)
            path.write_bytes(raw)
            row[name + "_file"] = path.name
            row[name + "_sha256"] = digest(raw)
            row[name + "_bytes_saved"] = len(raw)
            try:
                raw.decode("utf-8", "strict")
            except UnicodeDecodeError:
                row["decoding_errors"].append(name)
        row["complete"] = bool(row["executed"] and ended_normally
                               and not row["timeout"] and not row["truncated"]
                               and not row["error"] and row["cleanup_complete"])
        is_pf = argv[0] == "/sbin/pfctl"
        row["stderr_gate"] = (streams["stderr"] in (b"", ALTQ) if is_pf
                              else streams["stderr"] == b"")
        row["retained_altq_warning"] = is_pf and streams["stderr"] == ALTQ
        row["clean"] = bool(row["complete"] and row["returncode"] == 0
                            and row["signal"] is None and not row["decoding_errors"]
                            and row["stderr_gate"])
        write_json(self.output / (label + ".json"), row)
        self.rows.append(row)
        self.bytes[label] = streams
        return row

    def manual(self, row, permitted):
        result = {"discovery_label": row["label"], "complete": False, "error": None}
        try:
            if not clean(row):
                raise ValueError("manual path discovery was not clean")
            if time.monotonic() >= self.deadline:
                raise ValueError("session budget exhausted before manual read")
            raw = self.bytes[row["label"]]["stdout"]
            if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
                raise ValueError("manual path output is not one complete path")
            path = raw[:-1].decode("ascii", "strict")
            if path not in permitted or str(Path(path).resolve()) not in permitted:
                raise ValueError("manual path outside explicit installed-manual allowlist")
            result["path"] = path
            with open(path, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("installed manual is not a regular file")
                blob = handle.read(MANUAL_BYTES + 1)
            result.update({"size": info.st_size, "mtime_ns": info.st_mtime_ns,
                           "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid,
                           "gid": info.st_gid, "limit_bytes": MANUAL_BYTES,
                           "truncated": len(blob) > MANUAL_BYTES})
            saved = blob[:MANUAL_BYTES]
            target = row["label"] + ".installed-raw"
            (self.output / target).write_bytes(saved)
            result.update({"captured_file": target, "captured_bytes": len(saved),
                           "captured_sha256": digest(saved)})
            if result["truncated"] or len(blob) != info.st_size:
                raise ValueError("manual capture incomplete or changed during read")
            result["complete"] = True
        except Exception as exc:
            result["error"] = type(exc).__name__ + ": " + str(exc)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    for field in ("image-os", "image-version", "runner-os", "runner-arch", "commit", "run-id", "run-attempt"):
        parser.add_argument("--" + field, required=True)
    args = parser.parse_args()
    metadata = {key: value for key, value in vars(args).items() if key != "output"}
    for value in metadata.values():
        if not re.fullmatch(r"[A-Za-z0-9_.-]{0,128}", value):
            raise ValueError("non-public or over-bound workflow metadata")
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        raise ValueError("commit must be an exact SHA")
    output = Path(args.output)
    if not output.is_absolute():
        raise ValueError("output must be a new absolute artifact directory")
    output.mkdir(exist_ok=False)
    recorder = Recorder(output)
    result = {"schema": 1, "classification": "NOT_ADMISSIBLE", "started_utc": utc(),
              "public_workflow_metadata": metadata,
              "reader_identity": {"uid": os.getuid(), "euid": os.geteuid(),
                                  "gid": os.getgid(), "egid": os.getegid(),
                                  "groups": os.getgroups(), "python": sys.executable,
                                  "python_version": sys.version, "isolated": sys.flags.isolated},
              "bounds": {"session_seconds": SESSION_SECONDS, "command_seconds": COMMAND_SECONDS,
                         "stdout_bytes": STDOUT_BYTES, "stderr_bytes": STDERR_BYTES,
                         "manual_bytes": MANUAL_BYTES, "anchors": MAX_ANCHORS,
                         "depth": MAX_DEPTH, "component_bytes": MAX_COMPONENT_BYTES,
                         "full_path_bytes": MAX_PATH_BYTES,
                         "path_bound_is_capture_restriction_not_abi_proof": True},
              "stop_reasons": [], "manuals": [], "inventory_mapping": [],
              "named_views_used": [], "drift": [], "source_applicability": "UNRESOLVED",
              "namespace_completeness": "UNRESOLVED_RESERVED_NAMES",
              "ruleclass_completeness": "UNRESOLVED",
              "mutable_host_resources_created": False,
              "evidence_files_created": True, "sdk_tests_builds_provider_calls": 0,
              "future_mutation_authorized": False,
              "limits": ["CLI visibility does not prove absence of reserved namespaces.",
                         "Filter/scrub reads do not cover all rule classes.",
                         "No exact shipped userspace source applicability is established.",
                         "Matching end samples are not atomic and cannot exclude ABA drift.",
                         "Read failures are not enforcement failures or unsupported-platform evidence."]}
    used = []
    initial = {}
    try:
        source_dir = output / "executed-source"
        source_dir.mkdir()
        result["executed_source"] = []
        own = Path(__file__).resolve()
        for path, name in ((own, "collect-pf-readonly.py"),
                           (own.parent.parent / "workflows" / "google-genai-sdk-compat.yml", "google-genai-sdk-compat.yml")):
            with path.open("rb") as handle:
                raw = handle.read(SOURCE_BYTES + 1)
            if len(raw) > SOURCE_BYTES:
                raise ValueError("executed source exceeds fixed preservation bound")
            (source_dir / name).write_bytes(raw)
            result["executed_source"].append({"file": "executed-source/" + name,
                                              "bytes": len(raw), "sha256": digest(raw)})
        for label, argv in IDENTITY:
            row = recorder.run(label, argv)
            if not clean(row):
                result["stop_reasons"].append(label + ": identity/manual command failed strict gate")
            if label in MANUAL_PATHS:
                manual = recorder.manual(row, MANUAL_PATHS[label])
                result["manuals"].append(manual)
                if not manual["complete"]:
                    result["stop_reasons"].append(label + ": manual provenance capture incomplete")
        for label, argv in ROOT_VIEWS:
            initial[label] = recorder.run("initial-" + label, argv)
            if not clean(initial[label]):
                result["stop_reasons"].append("initial-" + label + ": failed strict PF read gate")
        names = []
        if not result["stop_reasons"]:
            root = recorder.bytes["initial-root-rules"]["stdout"]
            if root not in (ROOT_DEFAULT, ROOT_FILTER):
                result["stop_reasons"].append("unknown root rules/hooks: narrow default conclusion blocked")
            else:
                try:
                    mapping = inventory(recorder.bytes["initial-root-inventory"]["stdout"], True)
                    result["inventory_mapping"] = mapping
                    names = [item["path"] for item in mapping]
                    if "com.apple" not in names:
                        raise ValueError("clean inventory lacks concrete com.apple parent; STOP named reads")
                except ValueError as exc:
                    result["stop_reasons"].append(str(exc))
        if not result["stop_reasons"]:
            recorder.allowed.add(PARENT_INVENTORY)
            for name in names:
                path_check(name)
                recorder.allowed.add(("/sbin/pfctl", "-a", name, "-sr"))
            parent_argv = ("/sbin/pfctl", "-a", "com.apple", "-sr")
            for label, argv in (("parent-inventory", PARENT_INVENTORY), ("parent-rules", parent_argv)):
                row = recorder.run("initial-" + label, argv)
                if row["executed"]:
                    used.append((label, argv, row))
                if not clean(row):
                    result["stop_reasons"].append("initial-" + label + ": failed strict PF read gate")
            if not result["stop_reasons"]:
                try:
                    result["direct_inventory_mapping"] = inventory(
                        recorder.bytes["initial-parent-inventory"]["stdout"], False, names)
                except ValueError as exc:
                    result["stop_reasons"].append(str(exc))
                if recorder.bytes["initial-parent-rules"]["stdout"] != b"":
                    result["stop_reasons"].append("unknown parent rules/hooks; raw nested display retained, never flattened")
            if not result["stop_reasons"]:
                for index, name in enumerate(names):
                    if name == "com.apple":
                        continue  # Already captured above, not omitted.
                    label = "named-" + str(index).zfill(2)
                    argv = ("/sbin/pfctl", "-a", name, "-sr")
                    row = recorder.run("initial-" + label, argv)
                    if row["executed"]:
                        used.append((label, argv, row))
                    if not clean(row):
                        result["stop_reasons"].append(label + ": failed strict PF read gate; stop remaining named reads")
                        break
                    if recorder.bytes[row["label"]]["stdout"] != b"":
                        result["stop_reasons"].append(label + ": unknown rules/hooks block narrow default conclusion")
                        break
    except Exception as exc:
        result["stop_reasons"].append(type(exc).__name__ + ": " + str(exc))
    finally:
        # One predeclared end sample, even after failure. Never authorizes children.
        for label, argv in ROOT_VIEWS:
            row = recorder.run("end-" + label, argv)
            before = initial.get(label)
            same = bool(before and clean(before) and clean(row)
                        and recorder.bytes[before["label"]] == recorder.bytes[row["label"]])
            result["drift"].append({"view": label, "both_clean_and_identical": same,
                                    "comparison": "sample-only-not-atomic"})
            if not same:
                result["stop_reasons"].append(label + ": end view differs, failed, or initial view missing")
        for label, argv, before in used:
            row = recorder.run("end-" + label, argv)
            same = bool(clean(before) and clean(row)
                        and recorder.bytes[before["label"]] == recorder.bytes[row["label"]])
            result["drift"].append({"view": label, "both_clean_and_identical": same,
                                    "comparison": "sample-only-not-atomic"})
            if not same:
                result["stop_reasons"].append(label + ": end view differs or failed; no retry")
        result["named_views_used"] = [{"label": label, "argv": list(argv)} for label, argv, _ in used]
        result["observed_visible_default_only"] = not result["stop_reasons"]
        result["observation_disposition"] = "BLOCKED" if result["stop_reasons"] else "VISIBLE_SAMPLE_ONLY"
        result["command_envelopes"] = recorder.rows
        result["ended_utc"] = utc()
        result["elapsed_seconds"] = time.monotonic() - recorder.start
        result["session_budget_exceeded"] = result["elapsed_seconds"] > SESSION_SECONDS
        if result["session_budget_exceeded"]:
            result["observed_visible_default_only"] = False
            result["observation_disposition"] = "BLOCKED"
            result["stop_reasons"].append("session budget exceeded")
        write_json(output / "result.json", result)
    # A successful recorder exit is transport completion, NEVER admission/SDK pass.
    return 0


if __name__ == "__main__":
    sys.exit(main())
