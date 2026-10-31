#!/usr/bin/env python3.12
"""Offline, sequential auto-agent correctness and overhead release gate (stdlib only)."""
from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import shutil
import sys
import traceback
from pathlib import Path

from measure import json_safe, summarize, workload
from runtimes import Unavailable, prepare
from safety import HostSafety, clean_environment

HERE = Path(__file__).resolve().parent
LANGUAGES = ("python", "node", "go", "dotnet", "java", "ruby", "launcher", "launcher-node")


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    modes = p.add_mutually_exclusive_group(required=True)
    modes.add_argument("--quick", action="store_true", help="laptop smoke, NOT a benchmark")
    modes.add_argument("--full", action="store_true", help="large-N cloud VM lane")
    p.add_argument("--languages", help="comma-separated adapters; default quick=python,node; full=all")
    p.add_argument("--require", default="", help="comma-separated adapters that may not be skipped")
    p.add_argument("--require-all", action="store_true", help="fail on any unmeasured adapter")
    p.add_argument("--n", type=int, help="measured calls per mode per pair (20 quick / 1000 full)")
    p.add_argument("--warmup", type=int, help="untimed warmups per mode (3 / 20); spans still checked")
    p.add_argument("--pairs", type=int, help="AB/BA workload pairs (2 / 5)")
    p.add_argument("--startup-pairs", type=int, help="dedicated zero-call startup pairs (3 / 15)")
    p.add_argument("--chunk-delay-ms", type=float, default=2.0)
    p.add_argument("--settle-seconds", type=float, default=1.5, help="identical idle grace after workload, outside call timers")
    p.add_argument("--output", type=Path, default=HERE / "results.json")
    p.add_argument("--append", action="store_true", help="merge a sequential language run into same-config results")
    p.add_argument("--keep-work", action="store_true", help="keep tiny fixture builds/caches for diagnostics")
    p.add_argument("--self-test", action="store_true", help="run only the stdlib harness-oracle tests under the shared lock")
    p.add_argument("--braintrust-results", type=Path, help="same-schema measured competitor evidence from same host/config")
    p.add_argument("--require-10x", action="store_true", help="also fail without a valid matched 10x latency-overhead comparison")
    p.add_argument("--python", default="python3.12")
    p.add_argument("--node", default="node")
    p.add_argument("--go", default="go")
    p.add_argument("--java", default="java")
    p.add_argument("--javac", default="javac")
    p.add_argument("--dotnet", default="dotnet")
    p.add_argument("--ruby", default="ruby")
    p.add_argument("--ruby-gem-home", help="pre-provisioned external-drive gem directory; used as GEM_HOME and GEM_PATH")
    for option in ("node-agent", "java-agent", "dotnet-agent", "ruby-lib", "launcher"):
        p.add_argument("--" + option)
    return p


def comparison(results, path):
    if path is None: return {"status": "not_measured", "reason": "No matched Braintrust measurements. No 10x claim is supported."}
    if results["mode"] != "full":
        return {"status": "inconclusive", "reason": "Laptop smoke cannot substantiate the 10x performance bar"}
    try:
        other = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return {"status": "incomparable", "reason": "Cannot read competitor evidence: " + str(exc)}
    if other.get("mode") != "full":
        return {"status": "incomparable", "reason": "Competitor evidence must also be a full cloud-lane run"}
    if other.get("product") != "braintrust" or other.get("schema_version") != results["schema_version"]:
        return {"status": "incomparable", "reason": "Expected same-schema product=braintrust evidence"}
    if other.get("config") != results["config"] or other.get("host") != results["host"]:
        return {"status": "incomparable", "reason": "Host or workload configuration differs"}
    ratios, reasons = {}, []
    for name, row in results["languages"].items():
        if row.get("status") != "passed": continue
        baseline = other.get("languages", {}).get(name, {})
        if baseline.get("status") != "passed":
            reasons.append(name + ": Braintrust correctness evidence absent/failed"); continue
        ratios[name] = {}
        for mode in ("non_streaming", "streaming"):
            a = row["summary"]["calls"][mode]["total_us"]["overhead"].get("p95")
            b = baseline["summary"]["calls"][mode]["total_us"]["overhead"].get("p95")
            if a is None or b is None or a <= 0 or b <= 0:
                reasons.append(name + "/" + mode + ": nonpositive/noisy added overhead, no ratio"); continue
            ratios[name][mode] = b/a
    if reasons or not ratios:
        return {"status": "inconclusive", "ratios": ratios, "reasons": reasons}
    passed = all(len(v) == 2 and min(v.values()) >= 10 for v in ratios.values())
    return {"status": "passed" if passed else "failed", "ratios": ratios,
            "definition": "Braintrust p95 paired added-call overhead / traceAI p95 paired added-call overhead >=10 for JSON and SSE in each measured adapter; correctness parity required",
            "caveat": "External evidence is consumed as supplied, not independently collected by this harness; this is not a universal 10x performance claim."}


def table(results):
    lines = ["LAPTOP SMOKE — NOT A BENCHMARK" if results["mode"] == "quick" else "CLOUD LANE — offline mock-provider measurements",
             "", "| Adapter | Correctness | JSON added us p50/p95/p99 | SSE added us p50/p95/p99 | p95 relative JSON/SSE | SSE TTFC added us p95 | Startup added ms p50 | RSS added MiB p50 | First trace ms p50 |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    def fmt(value, factor=1): return "n/a" if value is None else f"{value/factor:.1f}"
    for name, row in results["languages"].items():
        if "summary" not in row:
            reason = row.get("reason", "").replace("\n", " ").replace("|", "/")
            lines.append(f"| {name} | {row['status']}: {reason[:200]} | — | — | — | — | — | — | — |")
            continue
        s = row["summary"]; a = s["calls"]["non_streaming"]; b = s["calls"]["streaming"]
        def quantiles(item): return "/".join(fmt(item["overhead"].get(q)) for q in ("p50", "p95", "p99"))
        relative = "/".join(fmt(m["total_us"]["relative_percent"].get("p95")) + "%" for m in (a,b))
        lines.append(f"| {name} | {row['status']} | {quantiles(a['total_us'])} | {quantiles(b['total_us'])} | {relative} | {fmt(b['ttfc_us']['overhead'].get('p95'))} | {fmt(s['startup_us']['overhead'].get('p50'),1000)} | {fmt(s['peak_rss_bytes']['overhead'].get('p50'),1048576)} | {fmt(s['first_trace_us'].get('p50'),1000)} |")
    lines += ["", "Signed paired on-minus-off deltas; negative values are retained, not interpreted as speedups.",
              "TTFC = first nonempty body bytes (not first semantic token). RSS = per-child wait4 high-water mark.",
              "Absolute off/on p50/p95/p99, raw timings, span attributes, byte hashes and failures are in results.json.",
              "Braintrust comparison: " + results["braintrust_comparison"]["status"],
              "Gate: " + ("PASS" if results["gate_passed"] else "FAIL"),
              "Not measured: " + ", ".join(results["not_measured"])]
    return "\n".join(lines) + "\n"


def save(results, args):
    results["not_measured"] = [name for name in LANGUAGES if results["languages"].get(name, {}).get("status") not in ("passed", "failed")]
    measured = [row for row in results["languages"].values() if row.get("status") in ("passed", "failed")]
    required = set(LANGUAGES) if args.require_all else set(filter(None, args.require.split(',')))
    required_missing = sorted(name for name in required if results["languages"].get(name, {}).get("status") != "passed")
    results["braintrust_comparison"] = comparison(results, args.braintrust_results)
    results["gate_passed"] = bool(measured) and all(row["status"] in ("passed", "skipped") for row in results["languages"].values()) and not required_missing
    if args.require_10x: results["gate_passed"] &= results["braintrust_comparison"]["status"] == "passed"
    results["gate_policy"] = {"required_adapters": sorted(required), "required_missing_or_failed": required_missing,
                              "require_10x": args.require_10x, "skips_are_passes": False,
                              "note": "Optional skips do not fail the exit code, but are not correctness passes. No measurements fails."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(json_safe(results), indent=2, allow_nan=False) + "\n")
    temporary.replace(args.output)


def main():
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Run the standard-library orchestrator with Python 3.12")
    args = parser().parse_args()
    if args.self_test:
        import unittest
        work = HERE / ".work" / f"self-test-{os.getpid()}"
        work.mkdir(parents=True, exist_ok=True)
        guard = HostSafety(work)
        try:
            with guard.step(True):
                suite = unittest.defaultTestLoader.loadTestsFromName("test_harness")
                outcome = unittest.TextTestRunner(verbosity=2).run(suite)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return 0 if outcome.wasSuccessful() else 1
    defaults = {"n": (20,1000), "warmup": (3,20), "pairs": (2,5), "startup_pairs": (3,15)}
    for key, choices in defaults.items():
        if getattr(args,key) is None: setattr(args,key,choices[int(args.full)])
    for key in ("n", "pairs", "startup_pairs"):
        if getattr(args,key) < 1: raise SystemExit(key + " must be positive")
    if args.warmup < 0 or args.chunk_delay_ms < 0 or args.settle_seconds < 0:
        raise SystemExit("warmup/delay/settle cannot be negative")
    selected = list(dict.fromkeys((args.languages or (','.join(LANGUAGES) if args.full else 'python,node')).split(',')))
    if set(selected) - set(LANGUAGES) or set(filter(None,args.require.split(','))) - set(LANGUAGES):
        raise SystemExit("Unknown adapter; choose " + ','.join(LANGUAGES))
    args.output = args.output.resolve()
    config = {key: getattr(args,key) for key in ("n", "warmup", "pairs", "startup_pairs", "chunk_delay_ms", "settle_seconds")}
    config.update({"order": "alternating off/on, on/off per pair", "batch_env_delay_ms": 200,
                   "quantiles": "linear interpolation, p=(n-1)*q; overhead quantiles are per-call paired signed deltas",
                   "transport": "raw HTTP clients, JSON + SSE; 11 input / 7 output tokens; 2 non-AI controls per workload"})
    results = {"schema_version": 1, "product": "traceai", "mode": "full" if args.full else "quick", "config": config,
               "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "host": {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
                        "cpu_count": os.cpu_count(), "hostname": platform.node()},
               "languages": {}, "host_observations": [], "scope_limits": [
                   "Quick results are laptop smoke only; no 10x or cloud performance claim.",
                   "Raw HTTP wire capture only; SDK/library integrations, dedup with native spans, errors/cancellation, other providers and realistic WAN delays are not measured.",
                   "Streaming compares full entity bytes, not packet/HTTP chunk boundaries; Unicode is split across writes.",
                   "No SDK trace flushing inside app. Identical post-workload idle grace precedes natural exit. Go blank import has no atexit.",
                   "Process RSS, not process tree or allocator attribution. Startup is spawn invocation to parent receipt of first app ready line, with reader scheduling noise.",
                   "Startup repeats have warm OS/import/Node compilation caches; not a cold boot/disk test.",
                   "Loopback server, collector and app share one host; exporter CPU is included but server load/noise can confound the deltas."]}
    if args.append and args.output.is_file():
        prior = json.loads(args.output.read_text())
        if prior.get("config") != config or prior.get("host") != results["host"] or prior.get("mode") != results["mode"]:
            raise SystemExit("Cannot append: host/mode/config differs; use another output path")
        results = prior
    work = HERE / ".work" / (datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}")
    work.mkdir(parents=True, exist_ok=True)
    (work / "logs").mkdir(exist_ok=True)
    guard = HostSafety(work)
    env = clean_environment(work)
    env.update({"FI_BASE_URL": "http://127.0.0.1:9", "FI_GRPC_URL": "http://127.0.0.1:9",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9"})
    root = HERE.parent.parent
    try:
        for name in selected:
            print(f"Measuring {name} sequentially", file=sys.stderr, flush=True)
            try:
                runtime = prepare(name, args, root, work, env, guard)
                row = {"status": "running", "metadata": runtime.metadata, "workloads": [], "startups": []}
                results["languages"][name] = row
                for pair_index in range(args.pairs):
                    pair_runs = {}
                    for enabled in ((False,True) if pair_index%2 == 0 else (True,False)):
                        pair_runs["on" if enabled else "off"] = workload(runtime,env,work,guard,args,pair_index,enabled)
                    row["workloads"].append(pair_runs)
                    print(f"{name}: workload pair {pair_index + 1}/{args.pairs} complete", file=sys.stderr, flush=True)
                for startup_index in range(args.startup_pairs):
                    pair_runs = {}
                    for enabled in ((False,True) if startup_index%2 == 0 else (True,False)):
                        pair_runs["on" if enabled else "off"] = workload(runtime,env,work,guard,args,0,enabled,startup_index)
                    row["startups"].append(pair_runs)
                    print(f"{name}: startup pair {startup_index + 1}/{args.startup_pairs} complete", file=sys.stderr, flush=True)
                print(f"{name}: summarizing", file=sys.stderr, flush=True)
                row["summary"] = summarize(row["workloads"],row["startups"])
                row["status"] = "passed" if row["summary"]["correctness_passed"] else "failed"
            except Unavailable as exc:
                results["languages"][name] = {"status": "skipped", "reason": str(exc)}
            except Exception as exc:
                old = results["languages"].get(name, {})
                results["languages"][name] = {**old, "status": "error", "reason": f"{type(exc).__name__}: {exc}",
                                              "traceback": traceback.format_exc()}
            results["host_observations"].extend(guard.observations); guard.observations.clear()
            save(results,args)
    except KeyboardInterrupt:
        results["languages"][name] = {"status": "error", "reason": "interrupted"}
    finally:
        results["host_observations"].extend(guard.observations)
        save(results,args)
        if not args.keep_work:
            for directory in (work/"tmp",work/"build",work/"cache",work/"snapshots"):
                shutil.rmtree(directory, ignore_errors=True)
    print(table(results), end="")
    print("Evidence: " + str(args.output))
    return 0 if results["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
