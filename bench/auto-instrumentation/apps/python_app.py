"""Ordinary HTTPX app: deliberately no tracing imports or flush calls."""
print('{"kind":"ready"}', flush=True)
import os
if os.environ.get("BENCH_STARTUP_ONLY") == "1":
    raise SystemExit(0)
import base64
import json
import time
import httpx

base = os.environ["BENCH_PROVIDER"]
n, warmup = int(os.environ["BENCH_N"]), int(os.environ["BENCH_WARMUP"])
with httpx.Client(trust_env=False, timeout=20) as client:
    for stream in (False, True):
        for i in range(n + warmup):
            call_id = f'{os.environ["BENCH_PAIR"]}-{int(stream)}-{i}'
            request = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": call_id}],
                       "stream": stream, "stream_options": {"include_usage": True}}
            chunks, first = [], None
            start = time.perf_counter_ns()
            with client.stream("POST", base + "/v1/chat/completions", json=request) as response:
                response.raise_for_status()
                for chunk in response.iter_raw():
                    if chunk and first is None: first = time.perf_counter_ns()
                    chunks.append(chunk)
            end = time.perf_counter_ns()
            print(json.dumps({"kind": "call", "id": call_id, "stream": stream, "warmup": i < warmup,
                              "latency_us": (end - start) / 1000, "ttfc_us": (first - start) / 1000,
                              "body_b64": base64.b64encode(b"".join(chunks)).decode()}), flush=True)
    client.get(base + "/health").raise_for_status()
    client.post(base + "/v1/chat/completions", json={"not_ai": True}).raise_for_status()
print('{"kind":"done"}', flush=True)
# Uniform idle grace, outside measured call/startup latencies. No telemetry APIs.
time.sleep(float(os.environ["BENCH_SETTLE_SECONDS"]))
