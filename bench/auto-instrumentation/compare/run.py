#!/usr/bin/env python3.12
"""Sequential offline SDK comparison. Quick smoke only: never a 10x benchmark claim."""
from __future__ import annotations

import argparse
import datetime
import difflib
import hashlib
import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PARENT = HERE.parent
ROOT = PARENT.parent.parent
SCRATCH = Path("/Volumes/nikm4/futureagi/tmp/bench-compare")
# -I removes cwd/user imports; add only these explicitly trusted harness directories.
sys.path[:0] = [str(HERE), str(PARENT)]
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True
from measure import paired, run_process, stats
from safety import HostSafety, clean_environment
from mocks import BT_KEY, FI_KEY, FI_SECRET, MEDIA_B64, SECRET, STREAM_TEXT_DELAY_MS, CompareMocks
from scoring import MANUAL, SCENARIOS, STREAMS, coverage, privacy, safe_ratio, spec

TOOLS = ("baseline", "traceai", "braintrust")
SCHEMA_VERSION = 2
# All six permutations: each tool occupies each position twice, each pair's
# precedence balances 3:3, and adjacent blocks reverse one another.
ORDER_CYCLE = (TOOLS, tuple(reversed(TOOLS)),
               ("traceai", "braintrust", "baseline"), ("baseline", "braintrust", "traceai"),
               ("braintrust", "baseline", "traceai"), ("traceai", "baseline", "braintrust"))


def execution_orders(cycles=1):
    return [list(order) for _ in range(cycles) for order in ORDER_CYCLE]


def counterbalanced(orders):
    if not orders or len(orders) % 6:
        return False
    if any(sorted(order) != sorted(TOOLS) for order in orders):
        return False
    target = len(orders) // len(TOOLS)
    return (all(sum(order[position] == tool for order in orders) == target
                for tool in TOOLS for position in range(len(TOOLS)))
            and all(sum(order.index(a) < order.index(b) for order in orders) == len(orders) // 2
                    for a in TOOLS for b in TOOLS if a != b))
DOCS = [
    "https://www.braintrust.dev/docs/sdks/python/install-and-instrument.md",
    "https://www.braintrust.dev/docs/instrument/trace-llm-calls",
    "https://www.braintrust.dev/docs/sdks/typescript/versions/3.38.0",
    "https://github.com/braintrustdata/braintrust-sdk-python",
]
PY_SETUP = 'import braintrust\ncompare_logger = braintrust.init_logger(project="offline-compare")\nbraintrust.auto_instrument()'
JS_SETUP = 'import {initLogger, wrapTraced} from "braintrust";\nconst compareLogger = initLogger({projectName:"offline-compare"});'


def under(path, root):
    return path.resolve().is_relative_to(root.resolve())


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def harness_hashes(compare=HERE, parent=PARENT):
    paths = [p for p in compare.iterdir() if p.suffix in (".py", ".mjs")]
    paths += [compare / name for name in ("requirements-python.txt", "package.json", "package-lock.json")]
    paths += [parent / name for name in ("measure.py", "mock_servers.py", "safety.py")]
    return {str(p.relative_to(parent)): file_hash(p) for p in sorted(paths)}


def validate_append(previous, config, host, hashes):
    if (previous.get("schema_version") != SCHEMA_VERSION or previous.get("config") != config
            or previous.get("host") != host or previous.get("source_hashes") != hashes):
        raise ValueError("Cannot append different schema/config/host/harness (including shared helpers); use a fresh output")


def tree_hash(path):
    h = hashlib.sha256()
    for file in sorted(path.rglob("*")):
        if file.is_file() and "__pycache__" not in file.parts:
            h.update(str(file.relative_to(path)).encode()); h.update(file.read_bytes())
    return h.hexdigest()


def variant(language, tool, scenario):
    original = (HERE / ("app.py" if language == "python" else "app.mjs")).read_text()
    result = original
    if tool == "braintrust":
        config = spec(scenario)
        if language == "python":
            result = result.replace("# INSTRUMENTATION_SETUP", PY_SETUP)
            result = result.replace("# FLUSH_SETUP", "compare_logger.flush()")
            if scenario in MANUAL:
                wrapper = ('invoke = braintrust.traced(name="manual-' + scenario + '", metadata=' + repr({
                    "model": config["model"], "provider": config["provider"]}) + ')(invoke)')
                result = result.replace("# MANUAL_WRAPPER", wrapper)
        else:
            result = result.replace("// INSTRUMENTATION_SETUP", JS_SETUP)
            result = result.replace("// FLUSH_SETUP", "await compareLogger.flush();")
            if scenario in MANUAL:
                wrapper = ('invoke = wrapTraced(invoke, {name:"manual-' + scenario + '", event:{metadata:' + json.dumps({
                    "model": config["model"], "provider": config["provider"]}) + '}});')
                result = result.replace("// MANUAL_WRAPPER", wrapper)
    diff = list(difflib.unified_diff(original.splitlines(), result.splitlines(), fromfile="baseline", tofile=tool, lineterm=""))
    additions = sum(line.startswith('+') and not line.startswith('+++') and bool(line[1:].strip())
                    and not line[1:].lstrip().startswith(('#', '//')) for line in diff)
    deletions = sum(line.startswith('-') and not line.startswith('---') and bool(line[1:].strip())
                    and not line[1:].lstrip().startswith(('#', '//')) for line in diff)
    setup = {"application_lines_added": additions, "application_lines_removed": deletions,
             "application_lines_changed": additions + deletions,
             "commands_before_first_trace": None if tool == "baseline" else 2,
             "command_count_definition": "one instrumentation installation + one launch; provider installation and environment assignment excluded; no cloud account/UI steps counted",
             "mode": "none" if tool == "baseline" else ("generic manual span + auto" if tool == "braintrust" and scenario in MANUAL else "auto"),
             "setup_code": "\n".join(line[1:] for line in diff if line.startswith('+') and not line.startswith('+++')),
             "unified_diff": "\n".join(diff),
             "justification": "Recommended auto instrumentation (init logger before provider imports)." if tool == "braintrust" and scenario not in MANUAL else
                 ("No native HTTP/Together wrapper in inspected supported matrix; documented generic traced/wrapTraced bridge logs actual input/output and explicit known model/provider, not invented usage." if tool == "braintrust" else
                  "Zero application tracing code; installed wheel startup flag / relocatable Node auto-register bundle.")}
    return result, setup


def environment(work, mocks, scenario, args):
    cleaned = clean_environment(work)
    # Keep the helper's generated settings but discard unrelated inherited credentials,
    # config, language hooks and arbitrary provider-specific URL/telemetry variables.
    allowed = {"PATH", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME", "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE",
               "npm_config_cache", "PIP_CACHE_DIR", "UV_CACHE_DIR", "NODE_COMPILE_CACHE", "NO_PROXY", "no_proxy"}
    env = {k: v for k, v in cleaned.items() if k in allowed or k.startswith(("FI_", "OTEL_"))}
    home = work / "home"; home.mkdir(exist_ok=True)
    env.update({"HOME": str(home), "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9",
                "http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9", "all_proxy": "http://127.0.0.1:9",
                "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
                "FI_API_KEY": FI_KEY, "FI_SECRET_KEY": FI_SECRET, "FI_BASE_URL": mocks.collector_url,
                "FI_GRPC_URL": mocks.collector_url, "FI_AUTO_INSTRUMENT": "false",
                "OTEL_EXPORTER_OTLP_ENDPOINT": mocks.collector_url,
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": mocks.collector_url + "/v1/traces",
                "BRAINTRUST_API_KEY": BT_KEY, "BRAINTRUST_API_URL": mocks.collector_url,
                "BRAINTRUST_APP_URL": mocks.collector_url, "BRAINTRUST_APP_PUBLIC_URL": mocks.collector_url,
                "BRAINTRUST_PROXY_URL": mocks.collector_url, "BRAINTRUST_ORG_NAME": "offline",
                "OPENAI_API_KEY": SECRET, "OPENAI_BASE_URL": mocks.provider_url + "/v1",
                "ANTHROPIC_API_KEY": SECRET, "ANTHROPIC_BASE_URL": mocks.provider_url,
                "GOOGLE_API_KEY": SECRET, "GEMINI_API_KEY": SECRET,
                "GOOGLE_GEMINI_BASE_URL": mocks.provider_url, "GOOGLE_VERTEX_BASE_URL": mocks.provider_url,
                "TOGETHER_API_KEY": SECRET, "TOGETHER_BASE_URL": mocks.provider_url + "/v1",
                "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false", "LANGSMITH_ENDPOINT": mocks.collector_url,
                "BENCH_SCENARIO": scenario, "BENCH_PROVIDER": mocks.provider_url, "BENCH_MEDIA": MEDIA_B64,
                "BENCH_N": str(args.n), "BENCH_WARMUP": str(args.warmup), "BENCH_SETTLE": str(args.settle_seconds)})
    return env


def probe(language, args, work, guard):
    if language == "python":
        code = ('import importlib.metadata as m,json,sys;print(json.dumps({"kind":"versions","runtime":sys.version,'
                '"packages":{d.metadata["Name"]:d.version for d in m.distributions()}}))')
        cmd = [args.python, "-I", "-c", code]
    else:
        code = ('const fs=require("node:fs");const p=JSON.parse(fs.readFileSync(' + json.dumps(str(SCRATCH / "package-lock.json")) +
                ',"utf8")); console.log(JSON.stringify({kind:"versions",runtime:process.version,packages:p.packages}));')
        cmd = [args.node, "-e", code]
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(work), "FI_AUTO_INSTRUMENT": "false",
           "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(work), "HTTP_PROXY": "http://127.0.0.1:9",
           "HTTPS_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1,localhost"}
    with guard.step():
        run = run_process(cmd, env, work, work / (language + "-versions.log"), 20)
    found = next((r for r in run["records"] if r.get("kind") == "versions"), None)
    if run["exit_code"] or found is None:
        raise RuntimeError("Version probe failed: " + run["stderr_log"])
    return found


def snapshot_node(work):
    source = ROOT / "typescript/packages/traceai_auto/dist/agent"
    if not (source / "preload.cjs").is_file():
        raise RuntimeError("Built relocatable Node dist/agent missing; no build is performed")
    package = work / "node_modules/@traceai/auto"
    shutil.copytree(source, package / "agent")
    (package / "package.json").write_text(json.dumps({"name": "@traceai/auto", "version": "0.1.0",
        "exports": {".": "./agent/preload.cjs"}, "private": True}))
    return {"version": "0.1.0", "source": str(source), "sha256": tree_hash(package / "agent"),
            "entry": "node --import @traceai/auto",
            "entry_note": "Scratch package maps @traceai/auto to unchanged relocatable dist/agent/preload.cjs (register/init). Current workspace root export only re-exports init and does not auto-start. No agent code edited."}


def timing_profiles(language, args, work):
    """Source-reviewed trigger contracts, not a guess based on metric spelling.

    Only the inspected Python Anthropic implementation is text-aware for these
    streaming methods. Pin its exact source hash before permitting semantic credit;
    unknown future versions must be reviewed rather than automatically rewarded.
    Body/SDK-item triggers remain nonsemantic even if buffering coincides with text.
    """
    site = Path(args.python).absolute().parent.parent / "lib/python3.12/site-packages"
    def contract(path, trigger, basis, directory=False):
        return {"trigger": trigger, "source": str(path),
                "sha256": tree_hash(path) if directory else file_hash(path), "review_basis": basis}
    if language == "python":
        body = contract(site / "fi_instrumentation/auto/wire/_capture.py", "first_chunk_or_item",
                        "Capture.on_chunk timestamps the first nonempty body bytes, without checking semantic content.")
        openai = contract(site / "braintrust/integrations/openai/tracing.py", "first_chunk_or_item",
                          "ChatCompletionWrapper.create.gen timestamps the first raw_response item, including empty role events.")
        anthropic = contract(site / "braintrust/integrations/anthropic/tracing.py", "unverified",
                            "__process_message checks nonempty delta text/thinking/input or nonempty content blocks before recording time; plain text fixture additionally requires application alignment.")
        if anthropic["sha256"] == "b85e8fd81f6db6f52148ee2eb26da05e466e3fb3336984e5dbb9726b3b5ef55f":
            anthropic["trigger"] = "semantic_text"
        else:
            anthropic["review_basis"] = "Unknown implementation hash: semantic trigger unverified; no TTFT credit."
    else:
        body = contract(work / "node_modules/@traceai/auto/agent", "first_chunk_or_item",
                        "Wire fetch/body reader records first nonempty bytes, not a semantic-text predicate.", True)
        native = contract(SCRATCH / "node_modules/braintrust", "first_chunk_or_item",
                          "Published traceStreamingCall sets firstChunkTime on its first yielded item; OpenAI/Anthropic messages.create use this generic path, not the separate text-aware sessions API.", True)
        openai = anthropic = native
    return {"baseline": {},
            "traceai": {name: {"gen_ai.response.time_to_first_chunk": body} for name in STREAMS},
            "braintrust": {"openai_stream": {"metrics.time_to_first_token": openai},
                           "anthropic_stream": {"metrics.time_to_first_token": anthropic}}}


def execute(language, tool, scenario, args, work, guard, repetition=0, timing_contracts=None):
    source, setup = variant(language, tool, scenario)
    label = f"{language}-{scenario}-r{repetition}-{tool}"
    app = work / (label + (".py" if language == "python" else ".mjs"))
    app.write_text(source)
    with CompareMocks() as mocks:
        env = environment(work, mocks, scenario, args)
        if language == "python":
            if tool == "traceai":
                env["FI_AUTO_INSTRUMENT"] = "true"
            command = [args.python, "-I", str(app)]
        else:
            command = [args.node, "--import", str(HERE / "loopback.mjs")]
            if tool == "traceai":
                command += ["--import", "@traceai/auto"]
            elif tool == "braintrust":
                command += ["--import", "braintrust/hook.mjs"]
            command.append(str(app))
        with guard.step():
            run = run_process(command, env, work, work / (label + ".stderr.log"), args.timeout)
    calls = [r for r in run["records"] if r.get("kind") == "call"]
    errors = []
    if run["exit_code"] != 0 or run["timed_out"]:
        errors.append("application failed or timed out; see " + run["stderr_log"])
    if len(calls) != args.n + args.warmup:
        errors.append("missing application calls")
    if len(mocks.requests) != args.n + args.warmup:
        errors.append("provider request count differs from expected calls")
    if run["startup_us"] is None or not any(r.get("kind") == "done" for r in run["records"]):
        errors.append("missing ready/done lifecycle records")
    errors += mocks.collector_errors + run["protocol_errors"]
    measured = [r for r in calls if not r["warmup"]]
    cov = coverage(tool, scenario, calls, mocks.spans, mocks.bt_rows, mocks.exports, timing_contracts)
    if tool == "baseline" and cov["spans"]:
        errors.append("uninstrumented baseline unexpectedly exported spans")
    first = min((e["received_ns"] for e in mocks.exports if e.get("received_ns") is not None), default=None)
    run.update({"repetition": repetition, "setup": setup, "calls": calls, "coverage": cov, "spans": mocks.spans,
                "timing_contracts": timing_contracts or {},
                "braintrust_rows": mocks.bt_rows, "exports": mocks.exports, "collector_errors": mocks.collector_errors,
                "provider_requests": mocks.requests, "errors": errors, "status": "failed" if errors else "measured",
                "latency_us": stats([r["latency_us"] for r in measured]),
                "ttft_us": stats([r["ttft_us"] for r in measured if r["ttft_us"] is not None]),
                "ttfc_us": stats([r["ttfc_us"] for r in measured if r["ttfc_us"] is not None]),
                "first_export_us": None if first is None else (first - run["_start_ns"]) / 1000,
                "routing": {k: env[k] for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "OPENAI_BASE_URL",
                    "ANTHROPIC_BASE_URL", "BRAINTRUST_API_URL", "BRAINTRUST_APP_URL", "FI_BASE_URL")}})
    run.pop("_start_ns", None)
    run.pop("records", None)  # calls and lifecycle summaries are retained above, without duplicating responses.
    return run


def finalize_scenario(row):
    baseline = row["baseline"]
    for tool in TOOLS:
        item = row[tool]
        item["parity"] = not item["errors"] and not baseline["errors"] and len(item["calls"]) == len(baseline["calls"]) and all(
            a["response"] == b["response"] for a, b in zip(baseline["calls"], item["calls"]))
        if not item["parity"]:
            item["status"] = "failed"
        off = [r["latency_us"] for r in baseline["calls"] if not r["warmup"]]
        on = [r["latency_us"] for r in item["calls"] if not r["warmup"]]
        item["overhead"] = {
            "latency_us": paired(off, on) if len(off) == len(on) else None,
            "startup_us": None if baseline["startup_us"] is None or item["startup_us"] is None else item["startup_us"] - baseline["startup_us"],
            "peak_rss_bytes": item["peak_rss_bytes"] - baseline["peak_rss_bytes"]}


def set_ratios(row):
    a, b = row["traceai"], row["braintrust"]
    ratios = {"definition": "Braintrust / traceAI; >1 favors traceAI for costs, <1 favors traceAI for coverage. Not a universal 10x claim.",
              "application_lines": safe_ratio(b["setup"]["application_lines_changed"], a["setup"]["application_lines_changed"]),
              "commands": safe_ratio(b["setup"]["commands_before_first_trace"], a["setup"]["commands_before_first_trace"]),
              "semantic_score": safe_ratio(b["coverage"]["score"], a["coverage"]["score"]),
              "spans": safe_ratio(b["coverage"]["spans"], a["coverage"]["spans"]),
              "latency_p50": safe_ratio(b["latency_us"].get("p50"), a["latency_us"].get("p50")),
              "latency_p99": safe_ratio(b["latency_us"].get("p99"), a["latency_us"].get("p99")),
              "startup": safe_ratio(b["startup_us"], a["startup_us"]),
              "peak_rss": safe_ratio(b["peak_rss_bytes"], a["peak_rss_bytes"]),
              "parity": "both" if a["parity"] and b["parity"] else "failed",
              "privacy": "both unobserved" if not a["coverage"]["privacy"]["observed_exports"] and not b["coverage"]["privacy"]["observed_exports"] else "see per-tool canary booleans"}
    for quantile in ("p50", "p99"):
        def added(item):
            distribution = item["overhead"]["latency_us"]
            return None if distribution is None else distribution["overhead"].get(quantile)
        aa, bb = added(a), added(b)
        ratios["positive_added_latency_" + quantile] = bb / aa if aa is not None and bb is not None and aa > 0 and bb > 0 and a["parity"] and b["parity"] else None
    if not a["parity"] or not b["parity"]:
        # Preserve raw evidence, but do not present a speed/quality ratio for broken apps.
        for key in ("semantic_score", "spans", "latency_p50", "latency_p99", "startup", "peak_rss"):
            ratios[key] = None
    if not row.get("performance_eligible", False):
        for key in ("latency_p50", "latency_p99", "startup", "peak_rss",
                    "positive_added_latency_p50", "positive_added_latency_p99"):
            ratios[key] = None
    row["ratios"] = ratios


def summarize_repetitions(repetitions):
    """Pool equal-N calls only after pairing each repeat with its own baseline.

    Raw samples/exports remain once, under repetitions; summaries do not re-map
    spans across processes with overlapping call indices. Startup/RSS summarize
    one matched process sample per repeat (not a separate cold-start experiment).
    """
    row = {"repetitions": repetitions, "counterbalanced": counterbalanced([r["order"] for r in repetitions])}
    for tool in TOOLS:
        runs = [r[tool] for r in repetitions]
        off, on = [], []
        for repeat in repetitions:
            off.extend(c["latency_us"] for c in repeat["baseline"]["calls"] if not c["warmup"])
            on.extend(c["latency_us"] for c in repeat[tool]["calls"] if not c["warmup"])
        coverages = [r["coverage"] for r in runs]
        all_exports = [e for r in runs for e in r["exports"]]
        cov = {"spans": sum(c["spans"] for c in coverages), "score": None, "privacy": privacy(all_exports)}
        if tool != "baseline":
            required = coverages[0]["required_fields"]
            per_call = [{**c, "repetition": i} for i, coverage_row in enumerate(coverages) for c in coverage_row["per_call"]]
            fields = {key: sum(c["field_hits"][key] for c in coverages) for key in required}
            cov.update({"required_fields": required, "field_hits": fields, "per_call": per_call,
                        "score": sum(fields.values()) / (len(per_call) * len(required)) if per_call else None,
                        "calls_with_spans": sum(c["calls_with_spans"] for c in coverages),
                        "unmapped_spans": sum(c["unmapped_spans"] for c in coverages)})
            timing = [c["stream_timing"] for c in coverages if c.get("stream_timing") is not None]
            cov["stream_timing"] = ({key: sum(t[key] for t in timing) for key in (
                "first_chunk_calls", "semantic_ttft_calls", "unresolved_calls")} if timing else None)
        startup = stats([r["startup_us"] for r in runs if r["startup_us"] is not None])
        rss = stats([r["peak_rss_bytes"] for r in runs])
        row[tool] = {"setup": runs[0]["setup"], "coverage": cov, "latency_us": stats(on),
                     "ttft_us": stats([c["ttft_us"] for r in runs for c in r["calls"] if not c["warmup"] and c["ttft_us"] is not None]),
                     "ttfc_us": stats([c["ttfc_us"] for r in runs for c in r["calls"] if not c["warmup"] and c["ttfc_us"] is not None]),
                     "startup_us": startup.get("p50"), "startup_distribution_us": startup,
                     "peak_rss_bytes": rss.get("p50"), "peak_rss_distribution_bytes": rss,
                     "parity": all(r["parity"] for r in runs),
                     "status": "measured" if all(r["status"] == "measured" for r in runs) else "failed",
                     "errors": [f"repeat {i}: {error}" for i, r in enumerate(runs) for error in r["errors"]],
                     "overhead": {"latency_us": paired(off, on) if len(off) == len(on) else None,
                                  "startup_us": stats([r["overhead"]["startup_us"] for r in runs if r["overhead"]["startup_us"] is not None]),
                                  "peak_rss_bytes": stats([r["overhead"]["peak_rss_bytes"] for r in runs])}}
    row["performance_eligible"] = row["counterbalanced"] and all(row[t]["parity"] and row[t]["status"] == "measured" for t in TOOLS)
    set_ratios(row)
    return row


def report(data):
    def num(v, scale=1):
        return "n/a" if v is None else f"{v/scale:.2f}"
    lines = ["# Offline SDK comparison", "", "**LAPTOP QUICK SMOKE — NOT A BENCHMARK. No 10x performance claim is supported.**", "",
        f"Measured calls/tool/scenario/repetition: {data['config']['n']}; warmups: {data['config']['warmup']}; repetitions: {6 * data['config']['cycles']}. Providers and collectors are loopback mocks. Adapters and tools run sequentially.", "",
        "All six tool-order permutations are run per cycle in reverse-paired blocks. Each tool occupies each position twice and pairwise precedence is 3:3. Differences pair calls by index within the same repetition, then pool equal-N samples. Performance ratios are withheld unless the complete counterbalanced cycle and parity pass. Startup/RSS show medians across repetition processes.", "",
        "| Runtime/scenario | Spans T/B | Semantic % T/B | Parity T/B | Added p50 us T/B | Added p99 us T/B | BT/T absolute p50 |", "|---|---:|---:|---|---:|---:|---:|"]
    not_ahead = ["No 10x claim is established: every timing below is a shared-laptop, tiny-N smoke observation.",
                 "Native provider attribution is a stricter rubric than protocol identity: wire exports labelled openai_compatible/gemini_compatible do not get OpenAI/Google identity credit for SDK scenarios. The raw gateway only requires openai_compatible.",
                 "Both adapters require provisioning and a launch. Zero application LOC is not zero deployment effort; Node uses a staged register bundle because the workspace root export does not auto-start."]
    for language, language_row in data["languages"].items():
        if "scenarios" not in language_row:
            lines.append(f"| {language} | unavailable | n/a | failed | n/a | n/a | n/a |")
            continue
        for name, row in language_row["scenarios"].items():
            a, b = row["traceai"], row["braintrust"]
            def overhead(item, q):
                value = item["overhead"]["latency_us"]
                return None if value is None or not row["performance_eligible"] else value["overhead"].get(q)
            lines.append(f"| {language}/{name} | {a['coverage']['spans']}/{b['coverage']['spans']} | " +
                f"{num(a['coverage']['score'],.01)}/{num(b['coverage']['score'],.01)} | {a['parity']}/{b['parity']} | " +
                f"{num(overhead(a,'p50'))}/{num(overhead(b,'p50'))} | {num(overhead(a,'p99'))}/{num(overhead(b,'p99'))} | {num(row['ratios']['latency_p50'])} |")
            if a["coverage"]["score"] is not None and b["coverage"]["score"] is not None and b["coverage"]["score"] >= a["coverage"]["score"]:
                not_ahead.append(f"{language}/{name}: Braintrust semantic score is equal or higher ({num(b['coverage']['score'],.01)}% vs {num(a['coverage']['score'],.01)}%).")
            if not a["parity"]:
                not_ahead.append(f"{language}/{name}: traceAI application parity/setup failed; cannot claim an advantage.")
            for metric in ("latency_p50", "latency_p99", "startup", "peak_rss"):
                ratio = row["ratios"].get(metric)
                if ratio is not None and ratio <= 1:
                    not_ahead.append(f"{language}/{name}: Braintrust observed {metric} cost is no higher (BT/T={ratio:.2f}); tiny-N noise, not a speed conclusion.")
    lines += ["", "## Streaming timing validation", "",
        "Exported timing names OR numerical coincidence establish no semantic credit. Reviewed implementation trigger contracts and source hashes are recorded per language/run: body/first-SDK-item timers remain TTFC even if buffering coincides with text. Only a source-verified text-aware trigger may earn TTFT, and its span-relative seconds must also align to the independently observed first text within 3 ms (+1 ms for Node rounding), with first item/text separated by >=10 ms. A 30 ms mock pause exposes role/header events. Unknown source/units and ambiguous/out-of-window text timings abstain. The inspected Python Braintrust Anthropic trigger is text-aware and hash-pinned; traceAI body timers and Braintrust OpenAI/Node generic first-item timers are not. This validates this fixture, not all production streams.", "",
        "| Runtime/scenario | Calls T/B (warmups included) | First chunk/item timing T/B | Semantic TTFT T/B | Unresolved T/B |",
        "|---|---:|---:|---:|---:|"]
    for language, lr in data["languages"].items():
        for name, row in lr.get("scenarios", {}).items():
            if name not in STREAMS:
                continue
            a, b = row["traceai"]["coverage"], row["braintrust"]["coverage"]
            values = [f"{a['stream_timing'][key]}/{b['stream_timing'][key]}" for key in (
                "first_chunk_calls", "semantic_ttft_calls", "unresolved_calls")]
            lines.append(f"| {language}/{name} | {len(a['per_call'])}/{len(b['per_call'])} | " + " | ".join(values) + " |")
    lines += ["", "## Setup, startup, memory and privacy", "", "T/B = traceAI / Braintrust. Costs include setup/shutdown policy below. Credential/media = exported payload canaries, not authorization headers.", "",
        "| Runtime/scenario | App LOC T/B | Commands T/B | Startup ms T/B | Peak MiB T/B | Credentials T/B | Raw media T/B |", "|---|---:|---:|---:|---:|---|---|"]
    for language, lr in data["languages"].items():
        for name, row in lr.get("scenarios", {}).items():
            a, b = row["traceai"], row["braintrust"]
            ap, bp = a["coverage"]["privacy"], b["coverage"]["privacy"]
            def observed(p, key):
                return str(p[key]) if p["observed_exports"] else "unobserved"
            lines.append(f"| {language}/{name} | {a['setup']['application_lines_changed']}/{b['setup']['application_lines_changed']} | " +
                f"{a['setup']['commands_before_first_trace']}/{b['setup']['commands_before_first_trace']} | " +
                f"{num(a['startup_us'],1000)}/{num(b['startup_us'],1000)} | {num(a['peak_rss_bytes'],1048576)}/{num(b['peak_rss_bytes'],1048576)} | " +
                f"{observed(ap,'credentials_in_payload')}/{observed(bp,'credentials_in_payload')} | {observed(ap,'raw_media_in_payload')}/{observed(bp,'raw_media_in_payload')} |")
    lines += ["", "## Per-metric ratios (Braintrust / traceAI)", "",
        "Cost ratios >1 favor traceAI; coverage/span ratios are descriptive (more spans can mean duplicates). LOC has a zero denominator and is n/a, not infinity. Parity/privacy are categorical, not meaningful multiplicative ratios. Failed parity suppresses performance/coverage ratios.", "",
        "| Runtime/scenario | Semantic | Spans | App LOC | Commands | p50 call | p99 call | Startup | RSS | Positive added p50 / p99 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for language, lr in data["languages"].items():
        for name, row in lr.get("scenarios", {}).items():
            r = row["ratios"]
            values = [num(r[k]) for k in ("semantic_score", "spans", "application_lines", "commands", "latency_p50", "latency_p99", "startup", "peak_rss")]
            lines.append(f"| {language}/{name} | " + " | ".join(values) + " | " +
                         num(r["positive_added_latency_p50"]) + " / " + num(r["positive_added_latency_p99"]) + " |")
    lines += ["", "## Where traceAI is NOT ahead", ""] + ["- " + t for t in not_ahead]
    lines += ["", "## Protocol and limitations", ""] + ["- " + t for t in data["limitations"]]
    lines += ["", "## Braintrust configuration", "", "Python uses `init_logger` + `auto_instrument()` before SDK imports (including LangChain); Node uses `initLogger` + `node --import braintrust/hook.mjs` (including Vercel). OpenAI audio/embedding/tool calls use the same native auto configuration, not a weaker obsolete chat-only wrapper.",
        "", "Raw HTTP and Together have no native entry in the inspected matrices; they additionally use a documented generic `traced`/`wrapTraced` operation with model/provider metadata and automatic I/O. These are labelled manual, not native provider coverage. Usage/TTFT/audio metadata are not fabricated by the harness. Exact inserted setup/diffs, actual commands and all installed versions are in results.json.",
        "", "Documentation was read via indexed public excerpts (no direct Braintrust-domain request). Read-only source matrices were inspected, never executed. Published PyPI/npm packages are the implementations run. Sources:"]
    lines += ["- " + url for url in DOCS]
    lines += ["", "## Failures", ""]
    for language, lr in data["languages"].items():
        if lr.get("error"):
            lines.append(f"- {language}: {lr['error']}")
        for name, row in lr.get("scenarios", {}).items():
            for repeat in row["repetitions"]:
                for tool in TOOLS:
                    if repeat[tool]["errors"] or not repeat[tool]["parity"]:
                        lines.append(f"- {language}/{name}/repeat-{repeat['index']}/{tool}: {repeat[tool]['errors'] or ['response differs from baseline']}; {repeat[tool]['stderr_log']}")
    return "\n".join(lines) + "\n"


def save(data, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")
    temp.replace(output)
    output.with_suffix(".md").write_text(report(data))


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--quick", action="store_true", required=True, help="explicit acknowledgement: not a benchmark")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--languages", default="python,node")
    p.add_argument("--scenarios", help="comma-separated; defaults to all applicable scenarios")
    p.add_argument("--n", type=int, default=3)
    p.add_argument("--warmup", type=int, default=1)
    p.add_argument("--cycles", type=int, default=1, help="complete six-permutation counterbalanced cycles (1..2); no unbalanced timing mode")
    p.add_argument("--settle-seconds", type=float, default=1)
    p.add_argument("--timeout", type=float, default=40)
    p.add_argument("--python", default=str(SCRATCH / "python/bin/python"))
    p.add_argument("--node", default=shutil.which("node") or "node")
    p.add_argument("--output", type=Path, default=HERE / "results.json")
    p.add_argument("--append", action="store_true")
    return p


def main():
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Use Python 3.12 for the stdlib-only orchestrator")
    args = parser().parse_args()
    if not 1 <= args.n <= 20 or not 0 <= args.warmup <= 3 or not 1 <= args.cycles <= 2 or not 0 <= args.settle_seconds <= 3 or not 1 <= args.timeout <= 120:
        raise SystemExit("Quick-only safety bounds: N 1..20, warmup 0..3, cycles 1..2, settle 0..3 seconds, timeout 1..120")
    selected = list(dict.fromkeys(args.languages.split(',')))
    if set(selected) - {"python", "node"}:
        raise SystemExit("Only python,node adapters are supported")
    scenarios = list(dict.fromkeys(args.scenarios.split(','))) if args.scenarios else None
    if scenarios is not None and set(scenarios) - set(SCENARIOS):
        raise SystemExit("Unknown scenario")
    args.output = args.output.resolve()
    if not under(args.output, HERE) and not under(args.output, SCRATCH):
        raise SystemExit("Outputs must be within compare/ or the designated scratch root")
    work = SCRATCH / "runs" / (datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}")
    work.mkdir(parents=True)
    guard = HostSafety(work)
    if args.self_test:
        import unittest
        with guard.step():
            outcome = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName("self_test"))
        return int(not outcome.wasSuccessful())
    config = {k: getattr(args, k) for k in ("n", "warmup", "cycles", "settle_seconds", "timeout")}
    config.update({"requested_scenarios": scenarios, "execution_orders": execution_orders(args.cycles),
                   "stream_text_delay_ms": STREAM_TEXT_DELAY_MS,
                   "pairing": "within repetition by call index; equal-N samples pooled across complete balanced cycles"})
    host = {"system": platform.system(), "machine": platform.machine(), "release": platform.release()}
    data = {"schema_version": SCHEMA_VERSION, "mode": "quick-not-a-benchmark", "config": config, "host": host,
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "languages": {}, "host_observations": [],
        "source_hashes": harness_hashes(),
        "limitations": [
            "Tiny N per process, warm caches, shared laptop. Each complete cycle runs all six tool-order permutations in reverse pairs (each tool in each position twice; pairwise precedence 3:3). Equal-N paired deltas pool within-repetition baseline comparisons, never unrelated processes. Drift and nonlinear carryover remain possible; p99 interpolation is not tail confidence. No 10x claim; nonpositive added overhead ratios are null.",
            "Call latency includes full stream consumption/SDK processing and a fixed 30 ms mock pause before first text. App TTFC is the first SDK item; app TTFT is first nonempty semantic text. Semantic TTFT requires a reviewed text-aware source trigger AND candidate alignment to separated app events within 3 ms (+1 ms Node rounding). First-body/item triggers never earn semantic credit even if buffering numerically coincides with text. Trigger contracts/source hashes are recorded; only the inspected hash-pinned Python Braintrust Anthropic trigger is semantic for these methods. Unknown implementations, missing/ambiguous/out-of-window references abstain. This is a fixture-specific check, not a universal proof. Ratios require complete counterbalance and parity.",
            "Startup is spawn to ready after relevant SDK imports/client construction, before first call; medians/distributions cover one workload process per repetition, not separate zero-call cold starts. RSS is individual wait4 high-water mark, not process-tree memory.",
            "Semantic score is equal-weight required fields per actual call (warmups included), value-checked from actual exports. Model/provider must be explicit. No inferred provider credit. Unmapped spans are reported. Span counts alone are not quality.",
            "Semantic evidence is unioned within each call's epoch interval, including framework/provider children; duplicates can increase span count but not score. Node exports have millisecond clocks: matching uses +/-1 ms and a 4 ms inter-call gap outside timers. Zero-resolution completed durations are valid. Successful native spans count as non-error by native conventions; a deliberate 400 separately checks errors.",
            "Full SDK JSON responses/chunks are compared within each language, except LangChain's generated random message id is omitted and Vercel exposes text/finishReason/usage/toolCalls. This is not HTTP wire-byte or cross-language parity.",
            "Mocks use OpenAI JSON/SSE, Anthropic SSE, Gemini JSON, multipart transcription and a real 0.1-second synthetic PCM WAV. TTS duration is not invented if the export does not provide it. No real provider behavior, cancellations, async/concurrency or production performance tested.",
            "Privacy scans exported entity bodies/attachment uploads for dummy credential and raw-media canaries; normal collector auth headers are excluded. No exports means unobserved, never privacy success. Transcript/content defaults are not disabled.",
            "Loopback base URLs, cleared inherited credentials, fake HOME and closed proxies are mandatory. Python socket / Node socket-DNS-fetch guards add defense in depth, not a kernel-enforced sandbox.",
            "Braintrust defaults to background export; a documented flush is outside timed calls after identical idle grace. Flush LOC is counted. traceAI uses normal process shutdown. Tool-specific batching/flush policy is not normalized into a misleading first-trace speed claim.",
            "Python FI_AUTO_INSTRUMENT tests the isolated locally built wheel; no optional native traceAI provider packages are added. Node tests an unchanged snapshot of the relocatable dist/agent register bundle using a scratch package export, not the currently inert workspace root import.",
            "Setup LOC counts inserted executable lines versus identical fixtures, including initialization/flush/manual wrapper; install and launch are each one command. Zero traceAI LOC yields an undefined ratio, not infinity/10x. Environment/config/account effort is not condensed into LOC.",
            "Source can change under other workers. Agent artifacts, fixtures, commands, versions and hashes are recorded per invocation. Harness fingerprints include compare Python/MJS, dependency locks and reused measure.py/mock_servers.py/safety.py; append rejects differing fingerprints and checks them at each block and before final save. Installations are explicit separate host-gated steps, never automatic in the orchestrator.",
        ], "documentation": DOCS}
    if args.append and args.output.exists():
        previous = json.loads(args.output.read_text())
        try:
            validate_append(previous, config, host, data["source_hashes"])
        except ValueError as exc:
            raise SystemExit(str(exc))
        data = previous
    for language in selected:
        print(f"Measuring {language} sequentially", file=sys.stderr, flush=True)
        try:
            versions = probe(language, args, work, guard)
            metadata = {"versions": versions, "work": str(work)}
            if language == "node":
                metadata["agent"] = snapshot_node(work)
            else:
                agent = Path(args.python).absolute().parent.parent / "lib/python3.12/site-packages/fi_instrumentation"
                metadata["agent"] = {"path": str(agent), "sha256": tree_hash(agent)}
            metadata["stream_timing_contracts"] = timing_profiles(language, args, work)
            data["languages"][language] = {"metadata": metadata, "scenarios": {}}
            for scenario in (scenarios or SCENARIOS):
                if (language == "python" and scenario == "vercel") or (language == "node" and scenario == "langchain"):
                    continue
                repetitions = []
                for index, order in enumerate(config["execution_orders"]):
                    if harness_hashes() != data["source_hashes"]:
                        raise RuntimeError("Harness/shared helper source changed during invocation; refusing mixed evidence")
                    repeat = {"index": index, "order": order}
                    for tool in order:
                        print(f"  {scenario}/repeat-{index}/{tool}", file=sys.stderr, flush=True)
                        contracts = metadata["stream_timing_contracts"][tool].get(scenario, {})
                        repeat[tool] = execute(language, tool, scenario, args, work, guard, index, contracts)
                    finalize_scenario(repeat)
                    repetitions.append(repeat)
                data["languages"][language]["scenarios"][scenario] = summarize_repetitions(repetitions)
                data["host_observations"].extend(guard.observations); guard.observations.clear()
                save(data, args.output)
        except Exception as exc:
            data["languages"].setdefault(language, {})["error"] = f"{type(exc).__name__}: {exc}"
            save(data, args.output)
    data["host_observations"].extend(guard.observations)
    data["source_hashes_unchanged"] = harness_hashes() == data["source_hashes"]
    data["gate_passed"] = data["source_hashes_unchanged"] and bool(data["languages"]) and all(
        not lr.get("error") and lr.get("scenarios") and all(row["performance_eligible"] for row in lr["scenarios"].values())
        for lr in data["languages"].values())
    save(data, args.output)
    print(report(data))
    return 0 if data["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
