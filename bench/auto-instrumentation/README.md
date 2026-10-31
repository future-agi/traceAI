# Offline auto-agent release gate

A Python **3.12, standard-library-only orchestrator** and ordinary tiny HTTP apps.
`results.json` and `laptop-smoke.txt` contain the recorded laptop smoke and its
Markdown table. Run the oracle's standard-library self-tests with
`python3.12 /path/to/traceai-auto/bench/auto-instrumentation/run.py --quick --self-test`;
this checks collector decoding, chunked/gzip delivery and failure detection under
the same host gate/lock, without benchmarking any other language.

It measures the real zero-code entry, not an instrumentor called inside a timed loop.
No real provider, Future AGI service, API key, package download, or external network is
needed. Both mocks bind to **127.0.0.1 only**, on ephemeral ports. Do not run concurrent
invocations against the same output file.

## Cloud lane: one command

Provision the language toolchains, Python agent dependencies and prebuilt agents
**before** running this offline lane. From any working directory:

```sh
python3.12 /path/to/traceai-auto/bench/auto-instrumentation/run.py --full --require-all > /path/to/traceai-auto/bench/auto-instrumentation/cloud-summary.md
```

Exit **0** means every required adapter's correctness and byte checks passed.
A missing toolchain/agent is a clear `skipped` row; `--require-all` makes any skip fail
the release gate. Without that flag all languages are optional; any correctness
failure still fails, and an entirely skipped run fails. Select a smaller required
release matrix with `--require python,node,go`, for example. The authoritative data
is `results.json` beside this README; stdout is its Markdown summary table. An
existing result is replaced unless `--append` is requested. Use `--output` to keep
cloud evidence separate from the supplied laptop smoke.

Full defaults: **1,000 measured calls per mode per pair, 20 warmups, 5 alternating
AB/BA pairs, 15 zero-call startup pairs**. That is 20,000 measured AI calls per
adapter across off/on (10,000 in each state), plus warmups. Adapters run sequentially, never concurrently.

## Safe laptop smoke

Quick is a smoke test, **not a benchmark**, and defaults to Python + Node only:
20 measured calls per mode per pair, 3 warmups, 2 workload pairs, 3 startup pairs.
To run one language at a time and merge the evidence:

```sh
python3.12 /path/to/traceai-auto/bench/auto-instrumentation/run.py --quick --languages python --python /path/to/provisioned/python3.12 --require python
python3.12 /path/to/traceai-auto/bench/auto-instrumentation/run.py --quick --languages node --append --require python,node
```

Every probe/build/child checks `~/.futureagi-local/bin/host-status.sh` immediately
before execution, waits 60 seconds and rechecks if free memory is below 20% or load
exceeds 30. Missing/unparseable laptop status fails closed. Linux VMs without that
script use `/proc/meminfo` MemAvailable and `os.getloadavg()` at the same limits.
**All child steps** take the shared `/Volumes/nikm4/futureagi/tmp/traceai-auto-review/heavy.lock`
when that directory exists; otherwise they share this harness's `.work/heavy.lock`.
Only empty locks older than 25 minutes are reclaimed, matching the owner's lock
policy. Locks are released in `finally`, including child failure/interruption.
Do not run this on Windows (`wait4` and Unix process groups are required).

Installs are never performed. All new caches, temporary files and fixture builds
stay in `.work/<invocation-id>/` under this directory (therefore on /Volumes/nikm4
on the laptop), and are removed by default. Logs remain as evidence. `--keep-work`
retains them for diagnosis. Inherited provider credentials, agent options, proxies
and FI/OTel configuration are cleared; dummy local keys and loopback endpoints are
set explicitly. Go/NuGet builds cannot download dependencies; .NET CLI telemetry
and Go telemetry are disabled. Use an externally firewalled cloud VM for a
kernel-enforced no-egress guarantee; environment routing is not a sandbox against
an arbitrary malicious dependency.

## Adapters and offline prerequisites

| Adapter | Entry under test | Provision beforehand |
|---|---|---|
| `python` | `python -m fi_instrumentation.auto python app.py` (the `fi-instrument` CLI entry) | Python 3.12 + fi-instrumentation's dependencies + HTTPX. The repository package is used, not an older installed agent. No tracing imports in app. |
| `node` | `node --require dist/agent/preload.cjs app.cjs` | Node >=20.6 and built `typescript/packages/traceai_auto/dist/agent/` payload. No app dependencies. |
| `go` | Separately built app with one blank import; off binary has no agent | Go compatible with `go/auto/go.mod` (currently 1.26) and cached module dependencies. Set `TRACEAI_BENCH_GO_MOD_CACHE=/Volumes/nikm4/...` to an existing external-drive module cache. |
| `dotnet` | `DOTNET_STARTUP_HOOKS=<published agent DLL> dotnet Bench.dll` | .NET 10 SDK + complete `csharp/artifacts/auto/` publish directory. The fixture has no NuGet package dependencies; build uses an empty package-source configuration. |
| `java` | `java -javaagent:<jar> -cp <fixture> Bench` | Java/Javac >=17 + `java/traceai-javaagent/target/traceai-javaagent.jar`; fixture uses JDK HttpClient, no Maven or external dependencies. |
| `ruby` | `RUBYOPT=-rtraceai/auto` with `RUBYLIB=<repo>/ruby/lib` | Compatible Ruby + opentelemetry-sdk/exporter-otlp gems; missing work-in-progress source is an ordinary skip. Gem provisioning must stay on the external drive. |
| `launcher` | `traceai run -- python app.py` | Built `go/launcher/bin/traceai` and Python prerequisites. No Go build or installation in this adapter. |
| `launcher-node` | `traceai run -- node app.cjs` | Same launcher and built Node agent. |

`--python`, `--node`, `--go`, `--dotnet`, `--java`, `--javac`, `--ruby` select
executables. `--node-agent`, `--java-agent`, `--dotnet-agent`, `--ruby-lib` and
`--launcher` override payload paths. `--ruby-gem-home /Volumes/nikm4/...` selects
an already-provisioned gem tree as both GEM_HOME and GEM_PATH. The harness builds only its tiny Go/Java/.NET
fixtures, never modifies/rebuilds source trees outside this directory. Missing
cached dependencies/build prerequisites produce skips with build-log locations;
use required adapters to prevent those skips from silently weakening CI coverage.
Python and Node payloads are copied into per-invocation snapshots to prevent a
concurrent agent rebuild changing code halfway through a measurement. Hashes and
Python dependency versions are recorded with results.

## What the measurements mean

* Calls use persistent ordinary HTTP clients: HTTPX, global fetch, Go net/http,
  JDK HttpClient, .NET HttpClient, Ruby Net::HTTP. This isolates **wire capture**,
  not a dedicated SDK integration, and intentionally includes span/export work.
* JSON and SSE return unique per-call IDs/content, distinct request/response model
  names, and exactly 11 input / 7 output / 18 total tokens. SSE has a comment,
  role event, text deltas, a separate usage event, `[DONE]`, CRLFs, and a Unicode
  codepoint split across writes. There is a 2 ms pause between writes by default.
* Each received entity body is compared byte-for-byte to both the mock's expected
  body and its paired off/on body. Only byte counts/hashes, not full base64 copies,
  are retained in results; comparisons happen on the actual bytes before removal.
  Packet/chunk boundary equality is not claimed.
* Latency runs from immediately before the HTTP request to application body EOF
  and response closure. TTFC is the **first nonempty body bytes**, not first
  semantic token. Both absolute off/on and signed added-overhead distributions
  are reported in microseconds; relative overhead is `(on-off)/off * 100` per pair.
  p50/p95/p99 use linear interpolation at `(n-1)*q`. Overhead p95 is the percentile
  of paired differences, **not** subtraction of two marginal p95s. Negative
  differences are preserved and indicate noise, not proof of a speedup.
* Startup is before process creation to the parent's receipt of the app's first
  `ready` line, in a separate **zero-call** process. Observer scheduling is included.
  Repeats have warm filesystem/import/Node compilation caches, not cold-boot caches.
* Peak RSS is the individual child's `wait4().ru_maxrss`, normalized to bytes
  (macOS bytes, Linux KiB). It is not allocator attribution or process-tree RSS.
  Off/on RSS includes the same fixture workload and post-workload idle grace.
* First trace is process creation to the collector receiving the complete body
  of the first OTLP export containing a span, without a diagnostic/test span.
  Python requests `OTEL_BSP_SCHEDULE_DELAY=200`; Go and Java hard-code 200 ms;
  .NET hard-codes 1,000 ms; Node uses SimpleSpanProcessor (no scheduled batch delay).
  Ruby receives the same 200 ms environment setting; verify its eventual package
  documentation. Queue size 65,536, batch size 512 and exporter timeout 3,000 ms
  are requested through OTel env settings where honored. Each adapter records its
  known batch policy. There is a uniform 1.5 s idle grace outside call timers,
  then natural process exit; apps never explicitly initialize or flush tracing.
  Go's blank import has no atexit; the idle grace is essential for this fixture,
  not a guarantee of delivery for production programs that exit immediately.
* Correctness includes warmups: exactly one unique exported span per call, exact
  span name, request/response model, input/output/total tokens, output text,
  response ID and OK status. It rejects duplicate span IDs, duplicate/missing call
  IDs, extras/unmapped spans, decode errors and app failures. Each workload also
  sends a health GET and an invalid-body POST to the AI-looking URL; neither may
  create a span. Off and zero-call startup processes must export zero spans.

The OTLP collector accepts HTTP JSON or protobuf, Content-Length or chunked request
bodies, and gzip compression; protobuf decoding uses a small standard-library wire
reader. Raw timings, normalized received spans, server request IDs, run failures,
commands, payload hashes and host safety observations are in `results.json`.

## Braintrust / the owner's 10x bar

**No Braintrust measurements have been collected here. Neither passing correctness
nor the supplied laptop smoke substantiates a “10x Braintrust” claim.** This harness
has no Braintrust auto-agent adapter. It can consume externally collected,
same-schema `product=braintrust` evidence via `--braintrust-results FILE` only when
host and workload configuration match exactly and both correctness checks pass.
`--require-10x` additionally gates on at least 10x lower positive p95 **added call
latency** in both JSON and SSE for each measured adapter. Nonpositive/noisy added
overheads are inconclusive. This narrow definition is not a universal latency,
startup, RSS or quality claim; the external evidence is not independently verified.

Not yet covered: real SDK/library-layer coverage and deduplication under existing
model spans, provider failures/cancellation, compression of provider responses,
slow-consumer backpressure, other providers/transports, throughput/concurrency,
production WAN/service behavior, process-tree memory, cold caches, or a controlled
cloud benchmark run. The non-Python/Node adapters are supplied but were not run on
the laptop.

## SDK/provider/Braintrust comparison (separate harness)

`compare/run.py` adds a **Python 3.12 stdlib-only orchestrator** for actual provider
SDK applications under `baseline`, `traceai`, and `braintrust`. The preceding
Braintrust section describes the original `run.py` wire-only gate; it is unchanged.
The new comparison has its own schema and **does not feed or satisfy that gate's
`--require-10x` option**. Its checked-in evidence is the generated Markdown
tables/limitations in `compare/results.md`; the raw `compare/results.json` (about
16 MB) is kept locally and is not committed.

This is intentionally **quick mode only, NOT a benchmark**: default 3 timed calls
plus 1 warmup/tool/scenario; N is capped at 20. Python runs first, then Node; tools
and scenarios are sequential. Do not run concurrent invocations. Exit 0 means
application parity and mock/collector protocol checks passed, **not** complete
coverage, privacy certification, or a performance/10x win. Coverage gaps and zero
spans remain visible rather than silently skipping scenarios.

### Provision once on the external drive

The orchestrator never installs or rebuilds agents. Every install and every run
must use the shared `heavy.sh` host gate. All environments, caches, fixture
variants, logs and Node dependency trees stay under
`/Volumes/nikm4/futureagi/tmp/bench-compare`. Only PyPI/npm registries are used;
Braintrust source clones are read-only references, never executable dependencies.
Do not use Docker, real provider accounts or real credentials.

The supplied `requirements-python.txt` and `package.json`/`package-lock.json` pin
the published dependencies used for the recorded run. `uv` and Node must already
be installed. From any working directory (substitute your checkout path if needed):

```sh
S=/Volumes/nikm4/futureagi/tmp/bench-compare
C=/Volumes/nikm4/futureagi/traceai-auto/bench/auto-instrumentation/compare
H=/Volumes/nikm4/futureagi/tmp/traceai-auto-review/heavy.sh
mkdir -p "$S"
export TMPDIR="$S" UV_CACHE_DIR="$S/uv-cache" npm_config_cache="$S/npm-cache"
bash "$H" uv venv --python 3.12 "$S/python"
bash "$H" uv pip install --python "$S/python/bin/python" --index-url https://pypi.org/simple -r "$C/requirements-python.txt"
bash "$H" uv pip install --python "$S/python/bin/python" --index-url https://pypi.org/simple /Volumes/nikm4/futureagi/traceai-auto/python
cp "$C/package.json" "$C/package-lock.json" "$S/"
bash "$H" npm ci --prefix "$S" --registry https://registry.npmjs.org --ignore-scripts --no-audit --no-fund
```

The Python application uses the freshly installed local **wheel** and
`FI_AUTO_INSTRUMENT=true`, exercising the wheel `.pth` zero-code startup, not an
instrumentor called from the fixture. Python dependencies are shared between all
three modes in the isolated environment; no optional native traceAI provider
instrumentation packages are added. `--python` can select another provisioned
interpreter (the orchestrator itself must still be run with Python 3.12).

Node requires the already-built `typescript/packages/traceai_auto/dist/agent/`
relocatable payload. The harness snapshots that directory without changing agent
code into a scratch `@traceai/auto` package and launches
`node --import @traceai/auto`. **That scratch package maps the import to the built
register/preload entry. The current workspace root export only re-exports `init`
and does not auto-start.** This measures the zero-code register bundle, not proof
that the literal published root-package import works. The mapping, hashes and
actual commands are recorded; no workspace `node_modules` or builds are created.

### Run the oracle and a sequential comparison

```sh
bash "$H" python3.12 -I "$C/run.py" --quick --self-test
bash "$H" python3.12 -I "$C/run.py" --quick --languages python --n 3 --warmup 1
bash "$H" python3.12 -I "$C/run.py" --quick --languages node --n 3 --warmup 1 --append
```

`--append` requires matching host, configuration and harness hashes, and merges
languages rather than comparing unrelated runs. Default output is
`compare/results.json`; the adjacent `.md` file is generated automatically. Use
`--output "$S/custom-results.json"` to keep an earlier run's data. A diagnostic
subset can use `--scenarios openai_chat,transcription` (same subset for an append).
All supported scenario names appear in `compare/scoring.py`; `langchain` is
Python-only and `vercel` Node-only. Logs and generated variants remain under
`$S/runs/<invocation>/` for diagnosis; raw exports and command/agent provenance
are also embedded in the JSON evidence. Missing dependencies and failing apps
are reported as failures, never converted into coverage passes.

### Fair Braintrust setup

Public documentation was read through indexed excerpts; no benchmark process
contacts a Braintrust domain. Exact documentation URLs, published package
versions, inserted setup code and unified application diffs are recorded.

| Scenarios | Braintrust Python | Braintrust Node |
|---|---|---|
| OpenAI chat / stream / embeddings / tool calls / transcription / TTS / deliberate 400 | `init_logger` then recommended `auto_instrument()` before SDK imports | `initLogger` plus recommended `--import braintrust/hook.mjs` |
| Anthropic messages stream; Google GenAI generateContent | Same native auto configuration | Same native auto hook |
| LangChain / Vercel AI SDK app | LangChain global auto callback + native OpenAI instrumentation | Native AI SDK hook + underlying OpenAI hook |
| Raw HTTP OpenAI-compatible gateway; Together SDK (no native wrapper in inspected matrices) | Auto setup plus generic `traced(..., metadata=...)(invoke)` | Auto hook plus `wrapTraced(invoke, {event:{metadata:...}})` |

The generic bridges log real application input/output and known model/provider
metadata. They are explicitly **manual**, not native HTTP/Together integrations;
the harness does not invent usage, TTFT or audio attributes for them. Flush is
outside call timers after the same one-second grace in all modes. Setup cost
counts executable added/deleted lines, including Braintrust's documented flush:
4 Python / 3 Node native, plus one line for the manual bridge; traceAI has zero
application-line changes. Installation/provisioning and launch are counted as two
logical command steps (Node traceAI's installation step is bundle staging), not
cloud-account/UI work. A zero LOC denominator produces `null`, never infinity
or a manufactured “10x” ratio.

### Measurement, scoring and safety boundaries

* Reuses parent `mock_servers.py` (server lifecycle, deterministic OpenAI/SSE,
  JSON/protobuf decoder), `measure.py` (process lifecycle, wait4 RSS, quantiles,
  signed paired differences), and `safety.py` (host gate/serialization/environment
  helpers), without modifying them.
* Mocks add Anthropic SSE, Gemini, tool calls, embeddings, multipart transcription,
  a deterministic 0.1-second PCM WAV, an explicit provider error, and native
  Braintrust login/project/log/attachment endpoints. All bind only to 127.0.0.1.
* Every app gets closed HTTP/HTTPS/ALL proxies at `127.0.0.1:9`, loopback-only
  provider/platform URLs, dummy canary keys, cleared inherited credentials and a
  fake HOME. Python socket and Node socket/DNS/fetch guards add defense in depth;
  they are not an OS firewall against arbitrary malicious native code.
* Semantic completeness is equal-weight required fields **decoded from exports**
  per actual call, including warmups. Model/provider identity, input/output,
  exact token usage, tool payload, recorded latency/semantic TTFT, error status
  and applicable audio transcript/duration have explicit checks. Embedding
  dimension summaries count as output without requiring raw vectors. Generic
  protocol labels do not receive exact SDK-provider identity credit. The raw
  gateway requires only `openai_compatible`.
* Native partial Braintrust log rows are merged by record ID, not counted as
  extra spans. Framework/provider children are scored together within their
  call's epoch interval. Node millisecond timestamps use ±1 ms tolerance and
  a 4 ms inter-call gap outside call timers. All raw spans, field hits, unmatched
  spans and duplicates-by-count remain inspectable.
* Parity compares full SDK JSON/chunks within a language, except LangChain's
  generated random message ID is omitted and Vercel compares its exposed
  text/finishReason/usage/toolCalls result. Deterministic mock Date headers avoid
  scoring transport-clock differences as application corruption.
* Overhead includes p50/p99 signed paired call differences, absolute ratios,
  spawn-to-ready startup and individual-process peak RSS. Startup is one warm-cache
  sample/tool/scenario, not a repeated cold-start experiment. No concurrency,
  controlled cloud performance, real provider/WAN behavior or statistical
  confidence is claimed. Nonpositive added-overhead ratios are inconclusive.
* Privacy scans actual exported bodies and attachment uploads for dummy credential
  and raw-media canaries. Collector auth headers are excluded; zero exports means
  unobserved. A raw-media export is a measured default, not automatically a
  security defect. No content-off setting is used to make coverage look cheaper.

The generated report includes per-metric ratios and an explicit **“Where traceAI
is NOT ahead”** section. The current smoke does not establish the owner's 10x bar.

### Comparison verification corrections (schema 2)

The following **supersedes the comparison harness's earlier single-process timing
and TTFT descriptions above**, not the original wire-only gate. Schema 1 evidence
must not be appended to schema 2 evidence.

* Default `--cycles 1` runs **six repetitions of each scenario**, covering all six
  permutations of baseline/traceAI/Braintrust in adjacent reversed pairs. Each
  tool occupies each ordinal position twice and each pair's precedence balances
  3:3. `--cycles 2` repeats this complete schedule; there is no unbalanced timing
  mode. Applications, tools, scenarios and adapters remain strictly sequential.
  N=3 plus one warmup is **per tool per repetition**, giving 18 measured and six
  warmup calls/tool/scenario at the default setting. This is still **NOT a
  benchmark**. Complete counterbalance and application parity are prerequisites
  for displaying performance ratios, not evidence of statistical confidence.
* Each measured call is paired with the same call index in **its repetition's
  baseline** before signed differences are pooled. Startup/RSS show medians and
  distributions across the repeated workload processes, not one process and not
  a separate zero-call or cold-start experiment. Every repetition's actual launch
  order, calls, exports, commands and log path remains in `results.json` under
  `repetitions`; the tool-level records are aggregates, not extra executions.
* Streaming apps independently timestamp their **first SDK item (TTFC)** and
  **first nonempty text delta (semantic TTFT)**. Both OpenAI's role-only event
  and Anthropic's empty/header events precede a fixed **30 ms pause before text**.
  Exported timing names **and numerical coincidence** cannot establish semantics.
  Source-reviewed trigger contracts, implementation paths and hashes are recorded
  per language/run. A known first-body/SDK-item timer remains TTFC even when
  buffering makes its timestamp coincide with text; it **never** earns semantic
  TTFT credit. Only a verified text-aware trigger is eligible, and its value must
  additionally align to independently observed first text within 3 ms, plus 1 ms
  for Node timestamp rounding, with the two observed events at least 10 ms apart.
  The inspected Python Braintrust Anthropic implementation ignores empty/header
  events and is eligible only under its exact reviewed source hash. Braintrust
  OpenAI and Node's generic messages/create stream paths timestamp first items;
  traceAI's inspected field timestamps nonempty body chunks. Both are consistently
  nonsemantic, regardless of spelling or coincidental alignment. Unknown source,
  unknown units, absent/ambiguous references and out-of-window semantic values
  abstain. The report lists first-chunk/item, semantic and unresolved counts;
  JSON retains source contracts, event alignment and distances. This is a
  fixture-specific check, not a universal proof or a claim that SDK items equal
  HTTP body chunks.
* The append fingerprint now includes shared **`measure.py`, `mock_servers.py`
  and `safety.py`**, in addition to comparison Python/MJS sources and dependency
  pin files. A change to any of these rejects append. Fingerprints are also
  checked at every repetition and before the final save; changed source fails
  the gate. Existing agent snapshots/version provenance remains unchanged.
* The stdlib self-test now has **16 tests**, including misleading timing names
  for both products, correctly aligned semantic timings with reviewed triggers,
  buffered first-chunk/text coincidence receiving no semantic credit, ambiguous
  and missing references, the delayed empty-event fixtures, full order balance,
  within-repetition pairing, incomplete-cycle ratio suppression, and append
  rejection after changing each shared helper.

Reproduce the corrected evidence (no installation/rebuild is performed):

```sh
bash "$H" python3.12 -I "$C/run.py" --quick --self-test
bash "$H" python3.12 -I "$C/run.py" --quick --languages python --n 3 --warmup 1 --cycles 1
bash "$H" python3.12 -I "$C/run.py" --quick --languages node --n 3 --warmup 1 --cycles 1 --append
```

Counterbalancing removes the permanent product/order assignment; it does not
eliminate shared-host drift, nonlinear carryover, clock uncertainty, warm caches,
or tiny-N noise. The added mock delay is identical for all tools, included in
call latency, and deliberately unlike a realistic provider latency distribution.

### Additive caller-path profiling mode

`compare/profile_compare.py` is a separate, quick-only diagnostic mode. It leaves
`compare/results.json` and `results.md` untouched and generates `compare/profile.json`
and `profile.md` (only `profile.md` is committed). It selects OpenAI chat, stream and embeddings in Python and Node,
with separate **unprofiled**, **CPU-profiled**, and **internal-timer** processes.
Default N=20, five warmups and two reverse-order plain repetitions; N is capped at
20, warmups at five, repetitions at two. All conditions and languages run
sequentially through the existing host gate/lock and loopback mock environment.
It is not the comparison harness's complete six-permutation performance gate.

Provision the two additional published Node dependencies only into scratch (no
agent rebuild, repository environment change, Docker or real endpoint access):

```sh
S=/Volumes/nikm4/futureagi/tmp/bench-compare
C=/Volumes/nikm4/futureagi/traceai-auto/bench/auto-instrumentation/compare
H=/Volumes/nikm4/futureagi/tmp/traceai-auto-review/heavy.sh
bash "$H" env TMPDIR="$S" npm_config_cache="$S/profile-npm-cache" npm install \
  --prefix "$S/profile-deps" --registry=https://registry.npmjs.org \
  --ignore-scripts --no-audit --no-fund --save-exact \
  @opentelemetry/sdk-trace-base@2.2.0 acorn@8.15.0
bash "$H" python3.12 -I "$C/test_profile.py"
bash "$H" python3.12 -I "$C/profile_compare.py" --quick --n 20 --warmup 5 --repetitions 2
```

Use `--languages python` or `node`, `--scenarios openai_chat,openai_stream,embeddings`,
`--modes plain,cpu,internal`, and `--output "$S/custom-profile.json"` for bounded
subsets. A full report needs all three modes; CPU-only runs abstain from normalized
added-cost estimates because they have no unprofiled baseline. No append is offered.
`--quick --reanalyze --output "$S/custom-profile.json"` recomputes attribution
from saved raw profiles without SDK/provider calls; original workload hashes are
preserved alongside separate analysis hashes. Ambiguous reused minified symbols
abstain instead of borrowing a keepNames label from another lexical scope.
All variants, snapshots, raw `.pstats`/`.cpuprofile` files, logs and caches remain
under `$S/profiles/<invocation>/`. Acorn only reads the unchanged minified bundle
and its keepNames labels for function-range attribution; it never rewrites it.
The Node sampler uses the built-in inspector CPU profiler at a 100 us interval,
with untimed named codec/copy shims to expose otherwise opaque builtins.

Python uses the already-provisioned comparison wheel and its `.pth` startup;
Node snapshots the prebuilt zero-code bundle. The simple/batch experiment swaps
only the live processor in the **harness process**, retaining the exact guarded
exporter, privacy overlay and coverage callback. The Node batch arm uses the
published OpenTelemetry BSP; it is not a claim that the agent has a public batch
option. Privacy/content settings remain enabled throughout. Provider request and
unique telemetry counts (including warmups), credential export canaries, collector
errors and application parity are checked; this diagnostic is not a replacement
for the complete semantic/privacy release oracle.

Raw Python cProfile process-CPU costs are perturbed; internal Python timers are
foreground-thread-only. A thread-local clock is invalid for cross-thread profiler
events; negative profiler self times fail closed. Node samples are active-wall
estimates, including inter-call drains/export callbacks, not exact main-thread CPU.
The report preserves raw figures, independent fine-grained boundary timing and a
clearly labelled **heuristic** CPU-normalized cost partition, never an exact
p50 decomposition. Missing samples are not proof of zero cost; Braintrust's zero
privacy-stage attribution is not equivalent privacy coverage. Node bundle
file:line:column locations are authoritative when no source maps exist; original
TypeScript locations are not invented. Proposed S9 latency targets and release
budgets are explicitly unachieved engineering estimates requiring a dedicated
runner, not conclusions from this shared-laptop smoke.

### Profiling verification corrections (profile schema 2)

This **supersedes the heuristic normalized partition and achievable-target
wording above**. The Python per-event process-clock callback has a dominant,
instrumentation-dependent observer effect: a positive/stable unprofiled CPU
anchor does not validate raw stage proportions. **All normalized stage estimates
are now withheld for both languages**, including in `profile.json` (null estimates,
`normalization_reliable=false`). Raw perturbed microseconds and percentages,
file/line hot functions and independently timed boundaries are retained. They
are diagnostic observations, not an unprofiled p50/CPU decomposition. A new
observer-effect audit compares plain/CPU/internal p50s and exposes raw-attributed
versus plain-added CPU without using those ratios as correction factors. Restoring
any unprofiled stage estimates requires a separate lower-overhead sampling and
boundary-calibration experiment; no such validation is claimed here.

Node attribution now stops at provider-SDK/HTTP-client and application/harness
frames rather than walking through them to an instrumentation ancestor. The only
harness exceptions are the named codec/copy shims, and those still cannot cross
an intervening SDK/application boundary. Braintrust's `traceStreamingCall` serves
both streaming and non-streaming calls: generic `stream`/`generator` names are
wrapper work, not SSE reassembly. Only reviewed chunk aggregation functions
(currently `aggregateChatCompletionChunks`) receive that classification. Tests
include SDK `request`/`APIPromise` leaves and deterministic
`Braintrust wrapper -> OpenAI parseResponse -> profileJSONParse` stacks.

Prior numeric S9 ranges and absolute ceilings remain **unverified planning
hypotheses**, not achievable estimates or accepted release gates. Achievable
p50/p99 and approved budgets are null in the JSON. The report discloses how much
each candidate ceiling permits above the observed Braintrust p50; these gaps
are not approved privacy surcharges. A competitive gate must use a dedicated-runner
Braintrust floor plus an explicit, justified, separately approved privacy surcharge
as well as an absolute ceiling. Tiny/negative/noisy overheads cannot establish
that floor; p99 achievability remains unresolved.

Recompute the checked-in report from unchanged saved profiles, without new SDK
calls or modifying agent code, and run the bounded regression tests:

```sh
bash "$H" python3.12 -I "$C/test_profile.py"
bash "$H" python3.12 -I "$C/profile_compare.py" --quick --reanalyze
```

Saved workload/profiler hashes remain the original execution provenance; separate
analysis hashes and `reanalyzed_at` identify the corrected interpretation. No
`results.json`/`results.md` performance evidence is rewritten by this correction.
