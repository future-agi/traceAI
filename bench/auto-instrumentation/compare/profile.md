# Offline cost profile

**QUICK DIAGNOSTIC — NOT A BENCHMARK.**

Generated 2026-10-31T03:35:30.290885+00:00; N=20, warmup=5, plain repetitions=2. All runs sequential.

Interpretation recomputed 2026-10-31T04:05:27.640814+00:00 from unchanged saved profiles; the measurement date above is original.

## Scope and interpretation

- QUICK DIAGNOSTIC, not a benchmark or a release pass. 20-call caps and shared-host p99 are unstable.
- Plain, CPU-profile and internal-timer processes are separate; never subtract a profiled run from an unprofiled baseline.
- Python RAW profile figures use cProfile exclusive process CPU with C callees apportioned by observed call counts. Its per-event Python process-clock callback has a dominant, instrumentation-dependent observer effect. Raw microseconds and percentages describe the PERTURBED profile only; no unprofiled stage partition is validated. Background exporter CPU can contribute; internal boundary timers independently restrict themselves to the foreground thread. Thread-local clocks are invalid for cross-thread cProfile events.
- Node figures are V8 100us active-wall sampling estimates (idle/program excluded), not exact CPU or caller latency. Samples include 4ms inter-call drains and background export callbacks.
- Percentages divide each RAW stage by all attributed instrumentation stages IN THE PROFILED PROCESS; they are not unprofiled cost shares. Unattributed includes SDK, runtime and harness work. Node inheritance stops at provider-SDK/HTTP-client and application/harness boundaries, except explicitly named codec/copy shims. Only reviewed chunk-aggregation functions, not generic streaming wrappers, count as Braintrust SSE reassembly.
- Normalized stage estimates are WITHHELD for both languages. A positive/noise-screened plain CPU delta does not calibrate stage-dependent observation overhead, background CPU or sample composition. Retain raw profiles, observer-effect audit and independently timed boundaries; do not rescale them to plain CPU or p50.
- S1=credential discovery; S5/S6=privacy/safe snapshot boundary. They are grouped because the same recursive scrub functions serve both. No privacy checks are disabled.
- Internal timers are mutually exclusive nested boundaries (Python thread CPU, Node synchronous wall); JSON/body-copy *_all rows include provider SDK work and are NOT traceAI-only costs.
- Simple/batch is a harness-only processor swap retaining the exact guarded exporter, privacy overlay and coverage callback. Python native=batch; Node native=simple. No production agent sources are modified.
- Python is the already-installed comparison wheel, Node is a hash-pinned snapshot of the current prebuilt bundle. These may differ from concurrently edited checkout sources and from results.json.
- Node hot-function file:line:column refers to the measured minified OUR bundle; no source map exists. Do not invent original TypeScript line numbers.
- No cloud calls: existing clean environment, dead-loopback proxies, loopback mocks and Python/Node socket/DNS/fetch guards. These are defense-in-depth, not an OS firewall.

## What the measurements indicate

- Python native batch: foreground privacy boundaries consume 570.8 us/call under internal timers, with 3.0 export_span snapshots and 10.0 mask_attributes invocations per call before any unobserved background work. Repeated privacy/materialization is the primary target, not switching Python to SimpleSpanProcessor.
- Node native simple: 197.1 whole-application JSON parse/stringify invocations per call versus baseline 3.4; guarded privacy/export synchronous dispatch measures 455.8 us/call under separate internal timers (codec child time excluded). Do not add overlapping sampled and timed figures.
- python added p50 simple / batch (us): openai_chat 1737 / 893; openai_stream 2355 / 1255; embeddings 1456 / 758. Tiny shared-host observations, not causal effect sizes or stable tail claims.
- node added p50 simple / batch (us): openai_chat 1495 / 470; openai_stream 1123 / 156; embeddings 2243 / 414. Tiny shared-host observations, not causal effect sizes or stable tail claims.
- Byte-copy/SSE have few observed samples on these tiny fixtures; that does not establish their unprofiled costs. Transport includes observation/wrapper bookkeeping, not just copying. The fetch hook observes caller-created clones/tees rather than proactively teeing every response. Large payloads, slow consumers and concurrency are not measured.
- Braintrust has no directly equivalent S1/S5/S6 boundary in this attribution model: zero in those cells is not proof of identical privacy protection or zero sanitization cost. Sampled GC is process-wide, not proof of incremental agent GC.

## Unprofiled stage partition: WITHHELD

No normalized microsecond estimates are published for either language. Python process-clock callbacks perturb high-event-count instrumentation disproportionately; multiplying raw stage shares by a plain CPU delta cannot remove that bias. Node active-wall sample composition is also uncalibrated against background/worker CPU. Raw percentages below describe ONLY the observed profiled execution, not how unprofiled per-call time is divided. Independently timed boundaries support local prioritization, not all other stage shares.

Before any unprofiled stage estimates: collect low-overhead statistical Python samples at multiple intervals in counterbalanced plain/sampled processes, quantify per-tool perturbation and sampling uncertainty, compare stage shares across intervals, and validate selected boundaries with timer/no-op controls. Independently validate Node sampling against event-loop/background work and process-CPU mismatch. A positive aggregate CPU anchor or a single privacy timer does not validate every stage proportion. This experiment has NOT been performed.

## Observer-effect audit

Separate processes, shared host; ratios are descriptive, not causal correction factors. Plain p50 pools plain repetitions. Raw attributed / plain added CPU is especially not a scale factor: its numerator includes profiler effects and may include background work. Baseline and nonpositive denominators abstain.

| Language/scenario/tool/processor | Plain p50 us | CPU-profile p50 us | Ratio | Internal p50 us | Raw attributed us/call | Plain added mean CPU us | Raw / added |
|---|---:|---:|---:|---:|---:|---:|---:|
| node/embeddings/baseline/native | 1936.7 | 1496.2 | 0.77 | 1889.0 | 0.0 | unresolved | unresolved |
| node/embeddings/braintrust/native | 2205.3 | 1564.9 | 0.71 | 1925.0 | 313.7 | 508.1 | 0.62 |
| node/embeddings/traceai/batch | 2351.0 | 1754.3 | 0.75 | 2353.5 | 345.4 | 792.8 | 0.44 |
| node/embeddings/traceai/simple | 3961.7 | 2619.6 | 0.66 | 3100.1 | 1242.4 | 2597.7 | 0.48 |
| node/openai_chat/baseline/native | 1945.8 | 1453.7 | 0.75 | 1662.5 | 0.0 | unresolved | unresolved |
| node/openai_chat/braintrust/native | 2086.0 | 1526.4 | 0.73 | 1829.8 | 295.7 | 259.7 | 1.14 |
| node/openai_chat/traceai/batch | 2405.3 | 1805.2 | 0.75 | 2160.2 | 491.7 | 669.9 | 0.73 |
| node/openai_chat/traceai/simple | 3402.9 | 2750.0 | 0.81 | 3350.8 | 1400.2 | 1890.4 | 0.74 |
| node/openai_stream/baseline/native | 45264.9 | 42464.2 | 0.94 | 44863.2 | 19.4 | unresolved | unresolved |
| node/openai_stream/braintrust/native | 44478.8 | 43045.1 | 0.97 | 43880.8 | 500.8 | 799.9 | 0.63 |
| node/openai_stream/traceai/batch | 45227.1 | 43632.8 | 0.96 | 45376.0 | 1875.9 | 3070.9 | 0.61 |
| node/openai_stream/traceai/simple | 45591.8 | 43052.7 | 0.94 | 46510.1 | 2037.3 | 3008.3 | 0.68 |
| python/embeddings/baseline/native | 535.6 | 2884.7 | 5.39 | 736.5 | 0.0 | unresolved | unresolved |
| python/embeddings/braintrust/native | 575.3 | 3285.0 | 5.71 | 624.3 | 415.2 | 65.2 | 6.37 |
| python/embeddings/traceai/batch | 1283.7 | 12631.4 | 9.84 | 1461.3 | 12870.5 | 759.3 | 16.95 |
| python/embeddings/traceai/simple | 1973.2 | 16631.5 | 8.43 | 2344.1 | 14569.4 | 1262.3 | 11.54 |
| python/openai_chat/baseline/native | 661.5 | 4125.5 | 6.24 | 690.7 | 0.0 | unresolved | unresolved |
| python/openai_chat/braintrust/native | 759.2 | 4940.6 | 6.51 | 769.3 | 584.0 | 62.9 | 9.28 |
| python/openai_chat/traceai/batch | 1573.4 | 16466.5 | 10.47 | 1776.5 | 15162.4 | 891.5 | 17.01 |
| python/openai_chat/traceai/simple | 2431.0 | 21829.2 | 8.98 | 2584.3 | 18093.8 | 1537.7 | 11.77 |
| python/openai_stream/baseline/native | 43488.7 | 47087.8 | 1.08 | 42908.8 | 0.0 | unresolved | unresolved |
| python/openai_stream/braintrust/native | 43263.1 | 47313.5 | 1.09 | 43333.1 | 2227.4 | -93.6 | unresolved |
| python/openai_stream/traceai/batch | 44601.3 | 60136.8 | 1.35 | 43811.4 | 18476.7 | 1186.3 | 15.57 |
| python/openai_stream/traceai/simple | 46296.5 | 65230.7 | 1.41 | 44315.0 | 19738.1 | 1683.7 | 11.72 |

## Raw native-policy stage profile

Equal scenario weight; **perturbed profile us/call (percent of attributed instrumentation CPU/samples)**. These are NOT measured unprofiled costs or shares. Python and Node methods differ; do not compare their totals as a speed ratio. Unattributed is separately shown. Node SDK/harness samples stop before an instrumentation ancestor, and Braintrust SSE means reviewed chunk aggregation, not traceStreamingCall.

| Stage | Python traceAI | Python Braintrust | Node traceAI | Node Braintrust |
|---|---:|---:|---:|---:|
| transport_buffer | 1376.5 (8.9%) | 0.0 (0.0%) | 68.0 (4.4%) | 0.0 (0.0%) |
| body_copy | 105.0 (0.7%) | 5.2 (0.5%) | 13.3 (0.9%) | 0.0 (0.0%) |
| json_codec | 211.0 (1.4%) | 0.8 (0.1%) | 623.4 (40.0%) | 68.2 (18.4%) |
| sse_reassembly | 13.0 (0.1%) | 6.8 (0.6%) | 2.5 (0.2%) | 6.6 (1.8%) |
| privacy_S1 | 347.2 (2.2%) | 0.0 (0.0%) | 107.1 (6.9%) | 0.0 (0.0%) |
| privacy_S5_S6 | 11902.3 (76.8%) | 0.0 (0.0%) | 358.9 (23.0%) | 0.0 (0.0%) |
| attributes | 643.2 (4.1%) | 200.7 (18.7%) | 73.4 (4.7%) | 28.3 (7.7%) |
| span | 419.0 (2.7%) | 14.9 (1.4%) | 49.6 (3.2%) | 22.3 (6.0%) |
| processor_export | 267.3 (1.7%) | 8.7 (0.8%) | 112.4 (7.2%) | 53.9 (14.6%) |
| context | 148.5 (1.0%) | 0.0 (0.0%) | 28.9 (1.9%) | 0.0 (0.0%) |
| wrapper | 70.1 (0.5%) | 838.5 (78.0%) | 0.0 (0.0%) | 160.4 (43.3%) |
| gc | 0.0 (0.0%) | 0.0 (0.0%) | 122.5 (7.9%) | 30.2 (8.2%) |
| unattributed | 3938.6 (excluded) | 4951.9 (excluded) | 2463.7 (excluded) | 2788.0 (excluded) |

GC zero means no observed Python explicit gc.collect/profile leaf or Node GC sample, NOT no allocation/GC cost. Node body-copy/JSON builtins can be charged to their parent stage; zero does not prove free.

## Unprofiled paired added latency

Each row pools within-repetition index-paired differences. Adjacent repetitions reverse the four-condition order; this is NOT the original six-permutation fair-ratio gate. No ratios or release conclusions.

| Language/scenario/tool/processor | N | Added p50 us | Added p99 us | Parity |
|---|---:|---:|---:|---|
| node/embeddings/braintrust/native | 40 | 183.7 | 8731.9 | True |
| node/embeddings/traceai/batch | 40 | 413.8 | 1732.5 | True |
| node/embeddings/traceai/simple | 40 | 2243.2 | 4556.3 | True |
| node/openai_chat/braintrust/native | 40 | 145.7 | 10084.4 | True |
| node/openai_chat/traceai/batch | 40 | 469.8 | 835.8 | True |
| node/openai_chat/traceai/simple | 40 | 1495.5 | 1887.3 | True |
| node/openai_stream/braintrust/native | 40 | 113.3 | 4388.1 | True |
| node/openai_stream/traceai/batch | 40 | 156.0 | 6527.0 | True |
| node/openai_stream/traceai/simple | 40 | 1122.7 | 5062.1 | True |
| python/embeddings/braintrust/native | 40 | 53.9 | 266.1 | True |
| python/embeddings/traceai/batch | 40 | 758.1 | 1109.0 | True |
| python/embeddings/traceai/simple | 40 | 1455.6 | 1653.5 | True |
| python/openai_chat/braintrust/native | 40 | 52.8 | 331.5 | True |
| python/openai_chat/traceai/batch | 40 | 892.6 | 1147.3 | True |
| python/openai_chat/traceai/simple | 40 | 1736.8 | 2101.7 | True |
| python/openai_stream/braintrust/native | 40 | -334.1 | 4432.8 | True |
| python/openai_stream/traceai/batch | 40 | 1254.7 | 6457.2 | True |
| python/openai_stream/traceai/simple | 40 | 2354.6 | 6881.8 | True |

## Fine-grained internal timing (separate perturbed processes)

Python exclusive foreground thread CPU; Node exclusive synchronous wall. Whole-application codec/copy rows are explicitly *_all. Values cannot be subtracted from plain added latency. Private Node stages remain CPU-sample estimates above.

| Language/tool/processor | Boundary | us/call | Calls/call |
|---|---|---:|---:|
| node/baseline/native | body_copy_all | 5.1 | 2.33 |
| node/baseline/native | json_codec_all | 18.1 | 3.38 |
| node/braintrust/native | body_copy_all | 6.4 | 3.87 |
| node/braintrust/native | json_codec_all | 69.9 | 58.62 |
| node/traceai/batch | body_copy_all | 11.9 | 9.22 |
| node/traceai/batch | json_codec_all | 374.4 | 111.03 |
| node/traceai/batch | privacy_and_export_dispatch | 235.7 | 0.08 |
| node/traceai/batch | processor_export | 3.6 | 1.00 |
| node/traceai/batch | span | 43.5 | 1.00 |
| node/traceai/simple | body_copy_all | 15.5 | 12.10 |
| node/traceai/simple | json_codec_all | 650.7 | 197.10 |
| node/traceai/simple | privacy_and_export_dispatch | 455.8 | 1.00 |
| node/traceai/simple | processor_export | 12.0 | 1.00 |
| node/traceai/simple | span | 43.1 | 1.00 |
| python/baseline/native | gc | 0.8 | 0.00 |
| python/baseline/native | json_codec_all | 22.1 | 3.38 |
| python/braintrust/native | gc | 8.4 | 0.00 |
| python/braintrust/native | json_codec_all | 19.9 | 3.38 |
| python/braintrust/native | span | 56.5 | 3.00 |
| python/braintrust/native | wrapper | 26.1 | 1.33 |
| python/traceai/batch | attributes | 148.6 | 4.00 |
| python/traceai/batch | context | 27.4 | 67.00 |
| python/traceai/batch | gc | 24.4 | 0.00 |
| python/traceai/batch | json_codec_all | 52.1 | 23.72 |
| python/traceai/batch | privacy_S1 | 43.4 | 13.33 |
| python/traceai/batch | privacy_S5_S6 | 570.8 | 15.00 |
| python/traceai/batch | processor_export | 24.1 | 1.00 |
| python/traceai/batch | span | 123.4 | 3.00 |
| python/traceai/batch | sse_reassembly | 6.0 | 0.33 |
| python/traceai/batch | transport_buffer | 20.9 | 6.67 |
| python/traceai/batch | wrapper | 36.7 | 1.00 |
| python/traceai/simple | attributes | 146.6 | 4.00 |
| python/traceai/simple | context | 41.3 | 88.00 |
| python/traceai/simple | gc | 23.3 | 0.00 |
| python/traceai/simple | json_codec_all | 56.1 | 27.72 |
| python/traceai/simple | privacy_S1 | 52.7 | 15.33 |
| python/traceai/simple | privacy_S5_S6 | 715.7 | 19.00 |
| python/traceai/simple | processor_export | 498.9 | 1.00 |
| python/traceai/simple | span | 125.6 | 3.00 |
| python/traceai/simple | sse_reassembly | 5.9 | 0.33 |
| python/traceai/simple | transport_buffer | 21.1 | 6.67 |
| python/traceai/simple | wrapper | 38.4 | 1.00 |

## Top 10 self-time hot functions: node/traceai

| Function | Measured file:line:column | us/call |
|---|---|---:|
| scrubValue | `file:///Volumes/nikm4/futureagi/tmp/bench-compare/profiles/20261031T033310-31101/node_modules/@traceai/auto/agent/runtime.cjs:61:102377` | 69.92 |
| <anonymous> | `file:///Volumes/nikm4/futureagi/tmp/bench-compare/profiles/20261031T033310-31101/node_modules/@traceai/auto/agent/runtime.cjs:61:104262` | 61.25 |
| <anonymous> | `file:///Volumes/nikm4/futureagi/tmp/bench-compare/profiles/20261031T033310-31101/node_modules/@traceai/auto/agent/runtime.cjs:76:3561` | 19.62 |
| <anonymous> | `file:///Volumes/nikm4/futureagi/tmp/bench-compare/profiles/20261031T033310-31101/node_modules/@traceai/auto/agent/runtime.cjs:76:3280` | 14.93 |
| checkRequest | `file:///Volumes/nikm4/futureagi/tmp/bench-compare/profiles/20261031T033310-31101/node_modules/@traceai/auto/agent/runtime.cjs:72:1008` | 12.98 |
| observeHeaders | `file:///Volumes/nikm4/futureagi/tmp/bench-compare/profiles/20261031T033310-31101/node_modules/@traceai/auto/agent/runtime.cjs:75:19752` | 11.25 |
| span | `file:///Volumes/nikm4/futureagi/tmp/bench-compare/profiles/20261031T033310-31101/node_modules/@traceai/auto/agent/runtime.cjs:76:6423` | 8.70 |
| build | `file:///Volumes/nikm4/futureagi/tmp/bench-compare/profiles/20261031T033310-31101/node_modules/@traceai/auto/agent/runtime.cjs:75:5370` | 8.60 |
| <anonymous> | `file:///Volumes/nikm4/futureagi/tmp/bench-compare/profiles/20261031T033310-31101/node_modules/@traceai/auto/agent/runtime.cjs:76:11637` | 6.55 |
| e | `file:///Volumes/nikm4/futureagi/tmp/bench-compare/profiles/20261031T033310-31101/node_modules/@traceai/auto/agent/runtime.cjs:61:103950` | 6.55 |

## Top 10 self-time hot functions: node/braintrust

| Function | Measured file:line:column | us/call |
|---|---|---:|
| walk | `file:///Volumes/nikm4/futureagi/tmp/bench-compare/node_modules/braintrust/dist/chunk-YEIVWW5E.mjs:10610:16` | 15.12 |
| _SpanImpl | `file:///Volumes/nikm4/futureagi/tmp/bench-compare/node_modules/braintrust/dist/chunk-YEIVWW5E.mjs:11276:14` | 13.32 |
| flushWrappedItemsChunk | `file:///Volumes/nikm4/futureagi/tmp/bench-compare/node_modules/braintrust/dist/chunk-YEIVWW5E.mjs:8256:31` | 11.07 |
| logInternal | `file:///Volumes/nikm4/futureagi/tmp/bench-compare/node_modules/braintrust/dist/chunk-YEIVWW5E.mjs:11370:14` | 8.77 |
| mergeDictsWithPathsHelper | `file:///Volumes/nikm4/futureagi/tmp/bench-compare/node_modules/braintrust/dist/chunk-YEIVWW5E.mjs:1030:35` | 8.67 |
| <anonymous> | `file:///Volumes/nikm4/futureagi/tmp/bench-compare/node_modules/braintrust/dist/chunk-YEIVWW5E.mjs:9364:5` | 8.50 |
| aggregateChatCompletionChunks | `file:///Volumes/nikm4/futureagi/tmp/bench-compare/node_modules/braintrust/dist/chunk-YEIVWW5E.mjs:23544:39` | 6.65 |
| processNode | `file:///Volumes/nikm4/futureagi/tmp/bench-compare/node_modules/braintrust/dist/chunk-YEIVWW5E.mjs:4318:23` | 6.60 |
| splitLoggingData | `file:///Volumes/nikm4/futureagi/tmp/bench-compare/node_modules/braintrust/dist/chunk-YEIVWW5E.mjs:11641:26` | 6.60 |
| <anonymous> | `file:///Volumes/nikm4/futureagi/tmp/bench-compare/node_modules/braintrust/dist/chunk-YEIVWW5E.mjs:1036:37` | 6.50 |

## Top 10 self-time hot functions: python/traceai

| Function | Measured file:line:column | us/call |
|---|---|---:|
| scrub | `/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/auto/wire/_privacy.py:91` | 4316.30 |
| <genexpr> | `/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/instrumentation/config.py:277` | 674.97 |
| mask_attributes | `/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/auto/wire/_privacy.py:167` | 663.88 |
| mask | `/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/instrumentation/config.py:291` | 270.33 |
| is_base64_url | `/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/instrumentation/config.py:427` | 157.72 |
| hide_error_details | `/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/instrumentation/config.py:267` | 124.23 |
| observe_request | `/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/auto/wire/_privacy.py:35` | 121.63 |
| export_span | `/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/auto/wire/_privacy.py:228` | 91.83 |
| resolve_provider | `/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/auto/wire/_core.py:137` | 79.53 |
| _header | `/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/auto/wire/_core.py:250` | 41.07 |

## Top 10 self-time hot functions: python/braintrust

| Function | Measured file:line:column | us/call |
|---|---|---:|
| _deep_copy_object | `/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/braintrust/bt_json.py:172` | 77.95 |
| merge_dicts_with_paths | `/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/braintrust/util.py:61` | 33.90 |
| _strip_nones | `/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/braintrust/logger.py:5494` | 25.75 |
| log_internal | `/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/braintrust/logger.py:5150` | 25.43 |
| _validate_and_sanitize_experiment_log_partial_args | `/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/braintrust/logger.py:3543` | 16.02 |
| _get_attr_or_item | `/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/braintrust/integrations/openai/tracing.py:598` | 14.08 |
| __init__ | `/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/braintrust/logger.py:4998` | 13.45 |
| split_logging_data | `/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/braintrust/logger.py:5500` | 12.65 |
| get_caller_location | `/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/braintrust/util.py:141` | 10.28 |
| _postprocess_streaming_results | `/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/braintrust/integrations/openai/tracing.py:746` | 9.58 |

## ROUND6 S9 design recommendations

1. S9 P0: keep one authoritative fail-closed final snapshot, but replace repeated whole-span scrubs with policy/secret-registry-versioned immutable safe views. Preserve per-export policy refresh, all resource/event/link/context surfaces, caps, hostile-object handling and multi-exporter isolation. Never reuse a stale safe mark across changed policy or secrets.
2. S9 P0: queue bounded detached telemetry work after body completion. Python can build/serialize/export on a worker, but must copy bounded owned buffers, request-time context and timing before return; redact before handing telemetry to untrusted exporters. Node BSP only defers exporter dispatch to the event loop, not another CPU thread: use a bounded worker for parse/sanitize/serialization if tail isolation is required.
3. S9 P1: lazy content materialization; skip request-parameter JSON, duplicate raw.input/raw.output and per-message aliases until a consumer/export convention requires them. Keep model, usage, response identity, error and privacy semantics. Do not parse nested JSON-looking strings repeatedly in each masking pass.
4. S9 P1: reuse already parsed request and response summaries; parse SSE incrementally once with bounded decoder state, reusing usage/text deltas rather than concatenating and reparsing all frames. Keep consumer identity, cancellation, byte ordering and backpressure tests.
5. S9 P2: bounded zero-copy views where ownership is guaranteed; one final owned copy only when asynchronous work outlives transport buffers. Cache catalog/URL match results with bounded keys. Keep context capture once per call; avoid per-chunk async-context work and avoid allocation-heavy Proxy/closure construction where safe.
6. Drop redundant representation/validation work, NOT credential discovery, fail-closed masking, default content capture, sampling correctness or required semantic fields. No safety-off comparison is a proposed optimization.

## Planning hypotheses, NOT achievable estimates or accepted gates

UNVERIFIED PLANNING HYPOTHESES ONLY. Achievable p50/p99 and accepted release budgets are unresolved: no optimized prototype, calibrated stage partition or measured residual-cost model establishes them. Especially no p99 achievability claim.

| Language | Unverified planning p50 / p99 range us | Candidate absolute ceiling p50 / p99 us | Achievable p50 / p99 | Accepted gate |
|---|---:|---:|---|---|
| python | [200, 450] / [600, 1200] | 500 / 1500 | unresolved | none |
| node | [250, 600] / [800, 1800] | 650 / 2000 | unresolved | none |

Absolute candidate ceilings alone do NOT establish competitive parity. Measure a stable Braintrust floor on the same dedicated runner and separately approve a numeric privacy-guarantee surcharge (currently unresolved). Require BOTH an absolute limit and traceAI added overhead <= Braintrust added overhead + that approved surcharge, per scenario and percentile. Nonpositive/noisy Braintrust overhead abstains. The implied gaps below are disclosures, not approved surcharges or evidence that the gaps are acceptable.

| Language/scenario | Observed Braintrust added p50 us | Candidate ceiling us | Ceiling / Braintrust | Implied UNAPPROVED privacy surcharge us |
|---|---:|---:|---:|---:|
| node/embeddings | 183.7 | 650.0 | 3.5 | 466.3 |
| node/openai_chat | 145.7 | 650.0 | 4.5 | 504.3 |
| node/openai_stream | 113.3 | 650.0 | 5.7 | 536.7 |
| python/embeddings | 53.9 | 500.0 | 9.3 | 446.1 |
| python/openai_chat | 52.8 | 500.0 | 9.5 | 447.2 |
| python/openai_stream | -334.1 | 500.0 | unresolved | unresolved |

Positive smoke overheads are NOT a stable attainable floor. In particular, a 500 us Python or 650 us Node absolute ceiling can permit a large Braintrust-relative gap; different privacy guarantees require an explicit justified surcharge, not an unexplained multiplier. No surcharge or attainable tail budget is established here. Numeric optimization savings are unresolved; the boundary times are not additive removable costs.

Proposed validation protocol, NOT an active release pass: dedicated runner, pinned payload/SDK/runtime, chat/stream/embeddings each, content ON, exact parity/privacy/one-span checks; >=10000 calls per mode across >=6 counterbalanced processes, warmup >=200; use upper bootstrap 95% CI of paired p50/p99 differences. Other candidate budgets: incremental SSE work <=50us p50 / <=200us p99 per chunk; <=4MiB/call retained capture and a queue bounded by BOTH 2048 entries and 32MiB total with visible drops; flush <=3s under healthy loopback collector.

## Provenance and validation

Scratch: `/Volumes/nikm4/futureagi/tmp/bench-compare/profiles/20261031T033310-31101`. Raw profiles, logs, per-call data, timer call counts, exact commands, sampling counts and hashes are in profile.json.

Failures: 0

```json
{
  "harness_sha256": {
    "profile_compare.py": "2f25919a8e0f7328a73f5eb41226a6b4a1cd7a2b6947fe1d8b3faf250cce5871",
    "profile_python.py": "b1c5c99abadd929de7e9d218b6350ce52a5cf956a8eecdce6cc99f7ee24b2705",
    "profile_node.mjs": "79c6fedc4a319690f89fab735956973571d9554067ff303d0707d97a090213ad",
    "profile_ranges.cjs": "76e9fd81a857d05bc0ba0d30f10175f4f7d420df55522a9546e2354f5f8a4dcb",
    "app.py": "d64034fd89c6fe3e4e6d7c7522b108ee5c6dee8afe4e1189fa56dbf38a673841",
    "app.mjs": "5e5e149379f6ca1960688f55f597d9a6aefc70fd267704a27230b8bf9c56a65b",
    "mocks.py": "f77cb9bc5c022f23892f14219e1c85216a2f60f8a58d6b9a31466e55d17bc06c",
    "loopback.mjs": "6b26d9fa54899a0248a4410866fa9a65bdd03607b1e51b086224470f2e6c5dbc"
  },
  "runtime": {
    "python": {
      "kind": "versions",
      "runtime": "3.12.11 (main, Oct  7 2025, 15:53:48) [Clang 20.1.4 ]",
      "packages": {
        "xxhash": "4.0.2",
        "opentelemetry-exporter-otlp-common": "0.66b2",
        "openai": "3.42.0",
        "rich": "14.3.4",
        "jsonpointer": "3.6",
        "opentelemetry-instrumentation": "0.66b2",
        "text-unidecode": "1.3",
        "pydantic": "2.14.2",
        "opentelemetry-exporter-otlp-proto-common": "1.45.2",
        "exceptiongroup": "1.3.1",
        "googleapis-common-protos": "1.76.0",
        "packaging": "26.3",
        "protobuf": "7.36.2",
        "together": "3.0.0",
        "websockets": "17.4",
        "Pygments": "2.21.0",
        "jsonschema-specifications": "2025.9.1",
        "requests": "2.34.2",
        "google-auth": "2.62.0",
        "rich-rst": "2.2.0",
        "certifi": "2026.10.23",
        "jsonschema": "4.26.0",
        "orjson": "3.13.0",
        "types-PyYAML": "6.0.12.20261030",
        "filelock": "3.32.7",
        "python-slugify": "9.1.3",
        "jsonpatch": "1.38",
        "pydantic_core": "2.50.2",
        "docstring_parser": "0.18.0",
        "zstandard": "0.25.0",
        "opentelemetry-exporter-http-transport": "0.66b2",
        "tenacity": "9.1.4",
        "jiter": "0.18.0",
        "httpcore": "1.0.9",
        "opentelemetry-exporter-otlp-proto-http": "1.45.2",
        "cryptography": "50.0.2",
        "cyclopts": "4.25.3",
        "pycparser": "3.12",
        "opentelemetry-semantic-conventions": "0.66b2",
        "langsmith": "0.14.16",
        "opentelemetry-api": "1.45.2",
        "detect_agent": "0.6.0",
        "tqdm": "4.70.1",
        "wrapt": "1.17.3",
        "langchain-openai": "1.7.3",
        "opentelemetry-proto": "1.45.2",
        "urllib3": "2.8.0",
        "h11": "0.16.0",
        "sniffio": "1.3.1",
        "mdurl": "0.1.2",
        "google-genai": "2.34.0",
        "PyYAML": "6.0.3",
        "httpcore2": "2.16.0",
        "anthropic": "2.3.0",
        "truststore": "0.10.5",
        "cffi": "2.1.1",
        "httpx2": "2.16.0",
        "annotated-types": "0.8.0",
        "referencing": "0.37.0",
        "regex": "2026.9.29",
        "markdown-it-py": "4.2.0",
        "typing-inspection": "0.4.4",
        "langchain-core": "1.7.3",
        "distro": "1.9.0",
        "rpds-py": "2026.9.1",
        "attrs": "26.1.0",
        "tiktoken": "0.14.0",
        "uuid_utils": "0.19.0",
        "requests-toolbelt": "1.0.0",
        "opentelemetry-sdk": "1.45.2",
        "httpx": "0.28.1",
        "typing_extensions": "4.16.0",
        "pyasn1": "0.6.5",
        "fi-instrumentation-otel": "1.2.1",
        "pyasn1_modules": "0.4.2",
        "braintrust": "0.49.1",
        "charset-normalizer": "3.5.2",
        "sseclient-py": "1.9.0",
        "langchain-protocol": "0.0.20",
        "anyio": "4.15.1",
        "chevron": "0.14.0",
        "idna": "3.20"
      },
      "source_sha256": {
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/__init__.py": "3a8495164c21f5d854803ffab042fb76db59d219ff6c6291602d12605a21e7d6",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/_secrets.py": "404eb2c4dc057c87fd8bfc7653a274c946eeae5d86f004aa1a8c823814ce38c3",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/fi_types.py": "fcfeaaeb467015793ec0c6d995d5ba28ecb25d0876b3ec6d69deb27d563b6005",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/settings.py": "c424b1e58ae32f2f9381fc874c6ce013f6ce356cd8a52148f9fb2573d987cc54",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/otel.py": "65cbaeb54b186c3ffbcf777e189285cfc60dc2e35f16e79d5ae06b7f29e2fddc",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/auto/_otel_entry.py": "ad5493c9ff1f794509c76a3caff1252d55e44d626603ded9ef22abcd3f1244fd",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/auto/_bootstrap.py": "1b7ad32d015e19fe0f674fc445d7d4b0086d298aaab780c799ff7467139415f9",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/auto/_cli.py": "9c79e5069d4e0be63613b6093d0fa8175ebce749093976903493ce389b6366dd",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/auto/__init__.py": "2255af2df7f50eafa0565f422529aca5e2904e963c90556d4aa484bfd31b11d9",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/auto/_registry.py": "b608a243705a316c60bff7d71e8ef79021636a531af51798e81741d885da35ba",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/auto/_sampling.py": "472439a77e67d0d333cc1c37bdfe088c1866edfca704a4faab166afe18b2f49a",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/auto/_config.py": "ae2d889ef27adbe99d9b18941868dd6b7fba9a390abcd5e3d32887a38ca2a4d0",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/auto/__main__.py": "8ef9b0afead2affc7500f6312bbaa0c682df9c674cf1cc74ae9fe1da31ce76f4",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/auto/_manager.py": "451d7a66c81ef579c08935dc4786f8d5f8090fce855f2991cd0ce251a23954b9",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/instrumentation/_spans.py": "737e057ef1ded0feb3339d589a209b5e4479f437e15869b4cfb0455fb3a472f0",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/instrumentation/pii_redaction.py": "cb93a5fc45643ed0c2d4ced536dbfc7c1699c63e039d30993ba1575a4f8a1646",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/instrumentation/logging.py": "37379249fbec8ed6a45ec1cf435d4e6f3b5e9215ddb259d23867b568825bebe7",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/instrumentation/_types.py": "0db30b27f0402fe2a763dc739ca089b661927ec0a9805a3fec96cc3648f59cd2",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/instrumentation/config.py": "f2bf233f862033c15d094249ba8b8c8c4dff39ca5290b31cb16aedb03324add8",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/instrumentation/version.py": "3259b81af99bffac90c7051bbf62ac73e0491572cf83d1f556dda257bc17ac0e",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/instrumentation/_errors.py": "e50c4ba18dcec8b17f5981f72620453f779d3998e2bc20d00270a57582e19978",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/instrumentation/_tracers.py": "bbc6773490852869d5a9d5be12739a656c98fef3dd3fd9e44b831fe31dfcb62b",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/instrumentation/_attributes.py": "760f6555e869f0082974fb2ec0505d520ba36a836b7a8512a4de54682ecd1efb",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/instrumentation/constants.py": "675c079da8f56f188015ccd655b3be9a0f7dfe630fdacd27ffc4ba0671712ce1",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/instrumentation/__init__.py": "ab37ae6c875f7c516806241339ad629965ee1f2a3add516b6d4d70f1d47eb4b9",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/instrumentation/context_attributes.py": "0cc6143240033e2c8f7eb9c00d041b278460196d5a6948a21998863da7039591",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/instrumentation/_protect_wrapper.py": "8c188508a060e2477776c18179d20f7234a490bd148af8ed478976db23cc8cde",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/instrumentation/helpers.py": "51de46653d9ca92f61cbd7dc75e26c60f6140f93a76f2de1a1f74779a03f9dee",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/auto/wire/__init__.py": "4ca5f53ab96c0f44ab05a3f75b3e4e88ea9d5dee11fc5a57ee755024c2abfbfd",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/auto/wire/_capture.py": "afa8538c404820fedde6f4e2471f7059c17c97dd22c5d6c1ae8f77f140a0ee95",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/auto/wire/_core.py": "726412b07cf49ce6fa73f6306e4802f15656d202da435957170e9256cdf1ca5e",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/auto/wire/_transports.py": "fd0b28bcf9e16f4c2bd51a6c60761a28bc8d4db260f574027f46dbd3c22b7d56",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/auto/wire/_privacy.py": "9c483b8ec7e88a3fcfe72849b3eb58ca65f4dec6a8bd9063e23b76b800fa136d",
        "/Volumes/nikm4/futureagi/tmp/bench-compare/python/lib/python3.12/site-packages/fi_instrumentation/auto/_sitecustomize/sitecustomize.py": "faa497ddc7d6ee9d5af5322882c32215066f9eaa0024ec359f5abcdfdcd50177"
      }
    },
    "node": {
      "kind": "versions",
      "runtime": "v26.4.0",
      "packages": {
        "openai": "7.46.0",
        "braintrust": "3.42.0"
      }
    }
  },
  "dependencies": {
    "node_profile_lock_sha256": "1228859705bee9d6ec8f67c5f81e37652926c14cdb6b66c2059aec71383b2af4"
  },
  "node_snapshot": {
    "version": "0.1.0",
    "source": "/Volumes/nikm4/futureagi/traceai-auto/typescript/packages/traceai_auto/dist/agent",
    "sha256": "8cc56237a46ab5a7d339b3a5f438e20c4b32676a91bfa135d89b748a9e8f93d3",
    "entry": "node --import @traceai/auto",
    "entry_note": "Scratch package maps @traceai/auto to unchanged relocatable dist/agent/preload.cjs (register/init). Current workspace root export only re-exports init and does not auto-start. No agent code edited."
  },
  "analysis_sha256": {
    "profile_compare.py": "ac1b6214c2f06209fb71e42e8f3bc4cbb8af8b0b85b5e411400a1bb4a5f00e1c",
    "profile_ranges.cjs": "9acf3657519c2e67be31099579c3567be0ccf6049ce49af86d3eb788cb8b74b5"
  },
  "analysis_note": "Attribution recomputed from unchanged saved raw profiles; workload harness hashes remain those of the actual execution."
}
```
