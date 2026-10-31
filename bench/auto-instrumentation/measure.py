"""Child lifecycle measurement, exact-response checks, and signed paired overhead."""
from __future__ import annotations

import base64
import collections
import hashlib
import json
import math
import os
import signal
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

from mock_servers import MODEL, RESPONSE_MODEL, LocalMocks, output_for, payload_for


def stats(values):
    values = sorted(values)
    if not values: return {"n": 0}
    def percentile(p):
        pos = (len(values) - 1) * p
        lo = int(pos); hi = min(lo + 1, len(values) - 1)
        return values[lo] + (values[hi] - values[lo]) * (pos - lo)
    return {"n": len(values), "p50": percentile(.5), "p95": percentile(.95), "p99": percentile(.99),
            "min": values[0], "max": values[-1], "mean": statistics.fmean(values)}


def paired(off, on):
    if len(off) != len(on): raise ValueError("unpaired observations")
    return {"off": stats(off), "on": stats(on), "overhead": stats([b-a for a,b in zip(off,on)]),
            "relative_percent": stats([(b-a)/a*100 for a,b in zip(off,on) if a > 0])}


def run_process(command, env, work, log_path, timeout):
    records, noise, reader_errors = [], [], []
    ready_ns = None
    timeout_hit = threading.Event()
    start_ns = time.monotonic_ns()
    with log_path.open("w") as stderr:
        process = subprocess.Popen(command, env=env, cwd=work, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=stderr, start_new_session=True)
        def read():
            nonlocal ready_ns
            try:
                for line in process.stdout:
                    received_ns = time.monotonic_ns()
                    try:
                        value = json.loads(line)
                        if not isinstance(value, dict): raise ValueError("non-object protocol record")
                    except (ValueError, UnicodeError):
                        noise.append(line.decode(errors="replace").rstrip()); continue
                    if value.get("kind") == "ready" and ready_ns is None: ready_ns = received_ns
                    records.append(value)
            except Exception as exc:
                reader_errors.append(str(exc))
        def kill():
            timeout_hit.set()
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        reader = threading.Thread(target=read, daemon=True); reader.start()
        timer = threading.Timer(timeout, kill); timer.daemon = True; timer.start()
        try:
            _, status, usage = os.wait4(process.pid, 0)
            process.returncode = os.waitstatus_to_exitcode(status)
        except BaseException:
            kill()
            _, status, _ = os.wait4(process.pid, 0)
            process.returncode = os.waitstatus_to_exitcode(status)
            raise
        finally:
            timer.cancel()
        reader.join(timeout=5)
        if reader.is_alive():
            kill(); reader_errors.append("stdout stayed open after child exit")
        process.stdout.close()
    end_ns = time.monotonic_ns()
    return {"command": command, "exit_code": process.returncode, "timed_out": timeout_hit.is_set(),
            "startup_us": None if ready_ns is None else (ready_ns-start_ns)/1000,
            "wall_us": (end_ns-start_ns)/1000,
            "peak_rss_bytes": int(usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024)),
            "records": records, "stdout_noise": noise, "protocol_errors": reader_errors,
            "stderr_log": str(log_path), "_start_ns": start_ns}


def valid_timing(call):
    return all(type(call.get(key)) in (int, float) and math.isfinite(call[key]) and call[key] > 0
               for key in ("latency_us", "ttfc_us")) and call["latency_us"] >= call["ttfc_us"]


def correctness(run, enabled, expected_ids, startup=False, warmup=0):
    errors = []
    def demand(condition, detail):
        if not condition: errors.append(detail)
    demand(run["exit_code"] == 0, f'process exit {run["exit_code"]}; see {run["stderr_log"]}')
    demand(not run["timed_out"], "process timed out")
    demand(run["startup_us"] is not None, "missing app ready line")
    demand(sum(r.get("kind") == "ready" for r in run["records"]) == 1, "missing/duplicate app ready line")
    demand(not run["protocol_errors"], "stdout protocol reader error")
    demand(run["peak_rss_bytes"] > 0, "missing/invalid process peak RSS")
    calls = [r for r in run["records"] if r.get("kind") == "call"]
    ids = [c.get("id") for c in calls]
    demand(collections.Counter(ids) == collections.Counter(expected_ids), "missing/duplicate/unexpected app call records")
    if not startup:
        demand(sum(r.get("kind") == "done" for r in run["records"]) == 1, "missing/duplicate app done line")
    for call in calls:
        try:
            _, expected_stream, index = call["id"].rsplit("-", 2)
            demand(type(call.get("stream")) is bool and call["stream"] == (expected_stream == "1"),
                   f'{call["id"]}: incorrect streaming mode')
            demand(type(call.get("warmup")) is bool and call["warmup"] == (int(index) < warmup),
                   f'{call["id"]}: incorrect warmup flag')
            body = base64.b64decode(call.pop("body_b64"), validate=True)
            call["_body"] = body
            call["body_sha256"] = hashlib.sha256(body).hexdigest()
            call["body_bytes"] = len(body)
            demand(body == b"".join(payload_for(call["id"], call["stream"])), f'{call["id"]}: bytes differ from mock provider')
            demand(valid_timing(call), f'{call["id"]}: invalid/nonfinite timing')
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            errors.append(f"invalid app response record: {exc}")
    actual_ai = [r.get("call_id") for r in run["provider_requests"] if r.get("ai")]
    demand(collections.Counter(actual_ai) == collections.Counter(expected_ids), "provider AI request count/IDs do not match the workload")
    for request in run["provider_requests"]:
        if request.get("ai"):
            demand(request.get("path") == "/v1/chat/completions" and request.get("model") == MODEL,
                   "unexpected AI request endpoint/model")
            demand(type(request.get("stream")) is bool and request["stream"] == (request["call_id"].rsplit("-", 2)[1] == "1"),
                   "unexpected AI request streaming mode")
    non_ai = [r for r in run["provider_requests"] if not r.get("ai")]
    demand(collections.Counter((r.get("method"), r.get("path")) for r in non_ai) ==
           collections.Counter([] if startup else [("GET", "/health"), ("POST", "/v1/chat/completions")]),
           "expected health GET + non-AI POST (or zero startup requests)")
    spans = run["spans"]
    demand(not run["collector_errors"], "collector decode errors: " + repr(run["collector_errors"]))
    if not enabled or startup:
        demand(len(spans) == 0, f"expected zero spans, received {len(spans)}")
    else:
        demand(len(spans) == len(expected_ids), f"expected exactly {len(expected_ids)} spans, received {len(spans)}")
        span_keys = [(s["trace_id"], s["span_id"]) for s in spans]
        for trace_id, span_id in span_keys:
            for value, length in ((trace_id, 32), (span_id, 16)):
                demand(isinstance(value, str) and len(value) == length and
                       all(c in "0123456789abcdefABCDEF" for c in value) and any(c != "0" for c in value),
                       "missing/malformed/zero trace or span ID")
        demand(len(set(span_keys)) == len(span_keys), "duplicate exported trace/span IDs")
        by_response = collections.defaultdict(list)
        for span in spans: by_response[span["attributes"].get("gen_ai.response.id")].append(span)
        demand(set(by_response) == {"chatcmpl-" + i for i in expected_ids}, "span response IDs mismatch (non-AI/unmapped/missing spans)")
        for call_id in expected_ids:
            matches = by_response.get("chatcmpl-" + call_id, [])
            demand(len(matches) == 1, f"{call_id}: expected one span, got {len(matches)}")
            for span in matches:
                attrs = span["attributes"]
                checks = {"gen_ai.request.model": MODEL, "gen_ai.response.model": RESPONSE_MODEL,
                          "gen_ai.usage.input_tokens": 11, "gen_ai.usage.output_tokens": 7,
                          "gen_ai.usage.total_tokens": 18, "output.value": output_for(call_id),
                          "gen_ai.output.messages.0.message.content": output_for(call_id)}
                demand(span["name"] == "chat " + MODEL, f'{call_id}: incorrect span name {span["name"]!r}')
                for key, expected in checks.items():
                    demand(attrs.get(key) == expected, f'{call_id}: {key} expected {expected!r}, got {attrs.get(key)!r}')
                demand(span["status"].get("code") in (1, "STATUS_CODE_OK", "OK"), f"{call_id}: span status is not OK")
        demand(run["first_trace_us"] is not None, "no first trace observed")
    return {"passed": not errors, "error_count": len(errors), "errors": errors[:100],
            "app_calls": len(calls), "spans_received": len(spans), "non_ai_requests": len(non_ai),
            "scope": "raw HTTP OpenAI-compatible JSON + SSE, including warmups; no SDK/library-layer assertion"}


def workload(runtime, env, work: Path, guard, args, pair_index, enabled, startup_index=None):
    startup = startup_index is not None
    label = f'{runtime.name}-' + (f'startup-{startup_index}' if startup else f'round-{pair_index}') + ('-on' if enabled else '-off')
    pair_id = f"round{pair_index}"
    expected = [] if startup else [f"{pair_id}-{s}-{i}" for s in (0,1) for i in range(args.n + args.warmup)]
    with LocalMocks(args.chunk_delay_ms) as mocks:
        local_env = {**env, **(runtime.on_env if enabled else runtime.off_env),
                     "FI_BASE_URL": mocks.collector_url, "OTEL_EXPORTER_OTLP_ENDPOINT": mocks.collector_url,
                     "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": mocks.collector_url + "/v1/traces",
                     "FI_GRPC_URL": mocks.collector_url,
                     "BENCH_PROVIDER": mocks.provider_url, "BENCH_N": str(args.n), "BENCH_WARMUP": str(args.warmup),
                     "BENCH_PAIR": pair_id, "BENCH_STARTUP_ONLY": "1" if startup else "0",
                     "BENCH_SETTLE_SECONDS": str(args.settle_seconds)}
        with guard.step(runtime.heavy):
            run = run_process(runtime.on if enabled else runtime.off, local_env, work,
                              work / "logs" / (label + ".stderr.log"), max(60, args.n*.1 + args.settle_seconds + 30))
    # Stop accepting requests and join handlers before snapshotting evidence. Otherwise
    # a late export already in flight could escape the duplicate/zero-span checks.
    with mocks.lock:
        run["spans"] = mocks.spans.copy()
        run["provider_requests"] = mocks.requests.copy()
        run["collector_requests"] = mocks.collector_requests.copy()
        run["collector_errors"] = mocks.collector_errors.copy()
        run["first_trace_us"] = None if mocks.first_span_ns is None else (mocks.first_span_ns-run["_start_ns"])/1000
    run.pop("_start_ns")
    run.update({"enabled": enabled, "pair": pair_index, "startup_only": startup,
                "correctness": correctness(run, enabled, expected, startup, args.warmup)})
    return run


def summarize(workloads, startups):
    errors, samples = [], {}
    for stream, label in ((False, "non_streaming"), (True, "streaming")):
        off, on, off_first, on_first = [], [], [], []
        equality, equal_count = True, 0
        for pair_runs in workloads:
            left = {r["id"]: r for r in pair_runs["off"]["records"] if r.get("kind") == "call" and r.get("stream") == stream}
            right = {r["id"]: r for r in pair_runs["on"]["records"] if r.get("kind") == "call" and r.get("stream") == stream}
            equality &= left.keys() == right.keys()
            for key in left:
                if key not in right: continue
                a, b = left[key], right[key]
                same = a.get("_body") == b.get("_body") and a.get("_body") is not None
                equality &= same; equal_count += int(same)
                if a.get("warmup"): continue
                if not valid_timing(a) or not valid_timing(b):
                    errors.append(key + ": invalid paired timings"); continue
                off.append(a["latency_us"]); on.append(b["latency_us"])
                off_first.append(a["ttfc_us"]); on_first.append(b["ttfc_us"])
        if not equality: errors.append(label + ": on/off bytes are not identical")
        samples[label] = {"total_us": paired(off, on), "ttfc_us": paired(off_first, on_first),
                          "byte_equal": equality, "byte_equal_calls_including_warmup": equal_count}
    def lifecycle(pairs, key):
        off = [p["off"][key] for p in pairs if p["off"][key] is not None and p["on"][key] is not None]
        on = [p["on"][key] for p in pairs if p["off"][key] is not None and p["on"][key] is not None]
        return paired(off, on)
    all_runs = [r for pair_runs in workloads + startups for r in pair_runs.values()]
    failures = [r["correctness"] for r in all_runs if not r["correctness"]["passed"]]
    checks = {"all_process_checks": not failures,
              "non_streaming_byte_equal": samples["non_streaming"]["byte_equal"],
              "streaming_byte_equal": samples["streaming"]["byte_equal"],
              "non_streaming_samples_present": samples["non_streaming"]["total_us"]["off"]["n"] > 0,
              "streaming_samples_present": samples["streaming"]["total_us"]["off"]["n"] > 0,
              "startup_samples_present": bool(startups),
              "first_trace_observed_in_every_on_workload": all(p["on"]["first_trace_us"] is not None for p in workloads)}
    return {"correctness_passed": all(checks.values()) and not errors, "checks": checks,
            "run_failures": len(failures), "comparison_errors": errors,
            "calls": samples, "startup_us": lifecycle(startups, "startup_us"),
            "peak_rss_bytes": lifecycle(workloads, "peak_rss_bytes"),
            "first_trace_us": stats([p["on"]["first_trace_us"] for p in workloads if p["on"]["first_trace_us"] is not None])}


def json_safe(value):
    if isinstance(value, dict): return {k: json_safe(v) for k,v in value.items() if not k.startswith("_")}
    if isinstance(value, list): return [json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value): return None
    return value
