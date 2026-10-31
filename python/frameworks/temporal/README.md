# traceAI Temporal

Replay-safe tracing for `temporalio` Python clients and workers. Install
`traceAI-temporal`, then instrument **before constructing clients or workers**:

```python
from fi_instrumentation import init
from traceai_temporal import TemporalInstrumentor

provider = init()
TemporalInstrumentor().instrument(tracer_provider=provider)
# Client.connect(...), Worker(...), and workflow/activity registration are unchanged.
```

This package does not change the traceAI auto registry. Its OpenTelemetry entry
point is available to `opentelemetry-instrument` when the package is installed.
`instrument()` installs constructor hooks only: there are no network calls, service
RPC patches, new retry policies, or replacements for application interceptors.
Repeated `instrument()` calls are idempotent. `uninstrument()` restores constructor
hooks; clients/workers already created retain their configured interceptors. Worker
inheritance checks the SDK's active client configuration (including client plugins)
on SDKs that expose it, with a fallback for older versions. For clients with plugins,
a final configuration plugin adds tracing only after application plugins have run.
Cloning a client moves any retained tracing finalizer behind newly added application
plugins. The first explicitly configured `TemporalInterceptor` takes precedence
over automatic defaults, including when a clone retains an earlier default.
Worker tracing is selected from the final merged interceptor chain after worker
plugins run: exactly one workflow interceptor and one activity span per attempt,
with explicit configuration taking precedence over automatic defaults. Application
plugins run once per construction; other interceptors retain their order and
identity, and caller-owned lists/configuration are not mutated. This covers clients
created both before and after `instrument()` without duplicate framework spans.

## Explicit interceptors

```python
from temporalio.client import Client
from temporalio.worker import Worker
from traceai_temporal import TemporalInterceptor

client = await Client.connect(
    "localhost:7233",
    interceptors=[TemporalInterceptor(tracer_provider=provider)],
)
# Temporal inherits worker interceptors from the client; don't add it twice.
worker = Worker(client, task_queue="agents", workflows=[MyWorkflow], activities=[my_activity])
```

An existing uninstrumented client can instead be used with
`Worker(..., interceptors=[TemporalInterceptor(tracer_provider=provider)])`.
Configure every worker process with the same tracing provider/export destination.
Activities can use existing traceAI model instrumentors or the wire layer, which
remain enabled beneath framework spans. No model code belongs inside workflows:
perform network I/O in activities, as required by Temporal.

## Span tree and determinism

```
application span
  temporal.start_workflow <type>           CHAIN / CLIENT
    temporal.workflow <type>               AGENT / SERVER
      temporal.activity <name>              TOOL / SERVER (one per attempt)
        chat <model>                        LLM (wire or model instrumentor)
      temporal.workflow <child type>        AGENT / SERVER
        temporal.activity <name>            TOOL / SERVER
```

Only W3C `traceparent` is carried in a dedicated `_traceai-temporal` Temporal
header, preserving other headers. No baggage, tracestate, or application content
is added to that header. **Content capture is ON by default:** the client CHAIN
span records workflow-start arguments; workflow AGENT and activity TOOL spans
record arguments and successful results (activities separately for every attempt).
Arguments are a JSON array in `input.value`; results are JSON in `output.value`,
including `null` for a successful `None`. Both use `application/json` MIME types.
Failed/cancelled executions retain their inputs and an ERROR status with exception
type only; they do not fabricate a result or record raw exception text/stacks.
Successful asynchronous activity handoff (`activity.raise_complete_async()`) ends
the TOOL span with OK status and `temporal.activity.async_completion=true`, not a
false failure. Its eventual remote result is unavailable to the activity interceptor.
Continue-as-new has no result on the outgoing run.
Malformed/oversized headers are ignored. Suppression is propagated explicitly,
including through update-with-start. Local suppression from an outer workflow or
activity interceptor is preserved when attaching a reconstructed/remote parent,
including when the worker constructor automatically appends tracing inside that
interceptor. Propagated suppression from the originating client applies across the
workflow's tasks, activities and child workflows. Local suppression wrapping only
`execute_workflow` is task-local: it covers work scheduled from that run task, but
not separate signal/update/query tasks (see Known limitations). Use the propagated
suppression header or the input/output hide controls for workflow-wide privacy.
Both normal workflow starts and
update-with-start calls create the CHAIN client span; an update targeting an
already-running workflow does not reparent that existing run.
`TraceConfig` is applied through FITracer. Set `FI_HIDE_INPUTS=true` and/or
`FI_HIDE_OUTPUTS=true` before initialization to hide the corresponding content
families, or configure the instrumentor/interceptor explicitly:

```python
from fi_instrumentation import TraceConfig

TemporalInstrumentor().instrument(
    tracer_provider=provider,
    config=TraceConfig(hide_inputs=True, hide_outputs=True),
)
# Explicit setup: TemporalInterceptor(provider, TraceConfig(hide_inputs=True))
```

Hidden values are redacted and their MIME attributes are omitted. Content is kept
only in `input.*` / `output.*`, never copied into Temporal metadata or error text.
The core agent's S1 export-time sanitizer remains the final privacy boundary for
credentials, signed URLs, media, other input/output-family hide controls (including
text/messages/images), and application-supplied metadata. This package neither
maintains its own secret registry nor invokes a converter again for telemetry.
Already-decoded Python values are snapshotted deterministically before application
code can mutate arguments. Only exact builtins are traversed; unsupported objects
(including custom subclasses and raw bytes) become their type names without
calling `__str__`, `__repr__`, `__json__`, getters, or application converters.
Snapshots perform no I/O and read no environment, clock, or random source inside
the sandbox; masking/export happen outside it.

Workflow span IDs (and root trace IDs when no client context exists) are stable
SHA-256 derivatives of Temporal's unique run ID. This consumes neither wall-clock
randomness nor the workflow's PRNG. Only deterministic context/header operations
and bounded builtin content snapshots run in the sandbox. A Temporal extern
callback creates/ends the span in host-side
telemetry code on completion, with workflow start/completion timestamps. Only that
callback temporarily permits exporter I/O and lazy telemetry imports; restrictions
are restored before workflow code resumes. Use a BatchSpanProcessor for network
exporters to avoid blocking workflow-task completion. Replays
reconstruct the same identity without exporting again; live spans are never held
in a process-local workflow cache. Activity spans are ordinary live FITracer spans
so async, threaded synchronous, and separate-process activities inherit context.
Failures/cancellation record exception type only and preserve original exceptions.
Terminal signal-handler failures complete the same workflow span even while its run
method is waiting; replay-local completion state prevents a second completion in
that instance. On SDKs exposing `workflow.is_failure_exception`, the SDK classifies
terminal errors (including native workflow timeouts and configured failure types),
so ordinary workflow-task failures do not emit false failure completions.

## Tests

Install the `test` extra and run `python -m pytest tests -q`. Integration tests use
Temporal's real time-skipping environment, sandboxed workflows, history replay,
separate worker processes, and a local HTTP mock LLM endpoint (no provider keys).
Content tests cover default client/workflow/activity arguments and results,
TraceConfig and environment hide controls, input/output-family controls, S1
credential/signed-URL/media scrubbing, binary and oversized real-server payloads,
cyclic/branching structures, hostile serialization hooks, and failed attempts.
Agent-on/off application command/payload histories match (excluding server
identities/timestamps and the tracing headers intentionally added by the agent);
both histories replay with and without instrumentation and emit no replay spans.
The unsupported result-converter regression confirms baseline/instrumented runs
both remain running after task failure without stringifying unsupported results.
Regression coverage also includes baseline parity for native timeouts and signal
failures, replay silence, update-with-start parenting/suppression and exact return
and error identity, local suppression beneath outer workflow/activity interceptors
in either order (including constructor auto-instrumentation), and client-plugin
inheritance without duplicate client/workflow/activity spans in both startup orders.
Plugin regressions also cover prepending/appending explicit tracing, preserving
configuration/interceptor order, adding privacy plugins to cloned clients with
retained automatic defaults/finalizers, worker plugins appending explicit privacy
configuration after constructor instrumentation (with/without inherited tracing),
and original plugin exceptions. Additional regressions ensure unsupported custom
metaclasses cannot run equality hooks and async activity handoff preserves result
parity without a false failure. Real-server media tests separately cover tiny
indexed audio paths, flattened inline-data paths with MIME siblings, and nested
inline data, without modifying the application's payload/result. The S1 dependency
regression checks all five input/output surfaces across CHAIN, AGENT and TOOL spans
and still checks replay silence before reporting any media leak. Six additional
real-server cases verify that explicit `TraceConfig` and environment input/output
hide controls prevent export of each media shape while preserving application
results and replay silence. These mitigations do not waive the default-capture
release blocker below.
Downloads, temporary data and package caches are kept under `.cache/` on the same
volume as this package. The test-server download must be reachable on first run.
Validation on **2026-10-30**, Python 3.12.11: Temporal 1.35.0 produced **96 passed,
2 failed**; the cached 1.9.0 runtime produced **79 passed, 2 failed, 17 skipped**.
All 75 pre-existing regressions passed on 1.35.0, including child workflows and
activities in another worker process and a synchronous file exporter invoked by
the host-side extern callback. On 1.9.0, sixteen plugin cases require newer SDKs;
that SDK also treats native Python timeouts as workflow-task failures rather than
terminal failures (the deterministic unit tests cover the classifier fallback).
Independent real-server hand-checks confirmed plugin deduplication/privacy, local
workflow suppression, native timeout parity and zero replay exports.

**Release-blocking core dependency (2026-10-30):** The two failing cases are
`test_real_structural_media_is_removed_by_final_s1_sanitizer[indexed_audio]` and
`[flattened_inline]`. The core S1 sanitizer currently retains base64 under
`input.audio.0` and `inlineData.data` with sibling `inlineData.mimeType="audio/wav"`
in captured JSON. Real workflows preserve their application results, but CHAIN,
AGENT and TOOL spans all still export this media. Nested inline-data scrubbing
passes. This is a core-owned sanitizer dependency, **not an accepted Known
limitation**: release remains blocked until the shared core fixes it in
`python/fi_instrumentation/auto/wire/_privacy.py` (outside this package). The two
default-capture regressions remain enabled, not skipped or marked `xfail`. This
package does not duplicate S1 media recognition or credential discovery. Until
that fix, use `FI_HIDE_INPUTS=true` and `FI_HIDE_OUTPUTS=true`, or equivalent `TraceConfig`
controls, to hide these arguments/results.
On this shared laptop, run suites with the host's `heavy.sh` helper; the worker
subprocess uses it too and budgets up to ten minutes for its slot to become free.
Elsewhere, `TRACEAI_TEST_HEAVY_HELPER` can select a helper; when the laptop-specific
helper is absent, normal CI subprocess execution is used.

## Known limitations

- **2026-10-30:** Content snapshots are capped at 4 MiB of ASCII JSON, 4,096
  visited nodes and 32 nesting levels, with a shared conservative budget reserving
  six bytes per character for JSON escaping plus structural overhead. Consequently
  a single string above roughly 640 Ki characters can be omitted even if its
  unescaped form is below 4 MiB. Oversized values/cyclic or excessively branching
  structures use `<omitted: content limit>` markers rather than raw prefixes.
  Custom objects/subclasses, dataclasses, protobuf objects and raw bytes are type
  names, not re-serialized through application hooks. Non-string dictionary keys
  become type names and their values are omitted; a key that exceeds the text
  budget is omitted together with its value so credential/media field names cannot
  be lost while their content survives. Integers outside signed 64-bit range become
  `int`, and non-finite floats become JSON strings. Use
  `FI_HIDE_INPUTS` / `FI_HIDE_OUTPUTS` or the corresponding `TraceConfig` controls
  when arguments/results should not be captured. The core agent supplies the S1
  sanitizer and S5 process-wide secret registry; manual paths without transport
  hooks cannot discover otherwise-unmarked credentials that never crossed an
  instrumented transport, and application-transformed secrets are not guaranteed
  to be recognized. Use the hide controls for these cases.
- **2026-10-30:** Workflow spans export only after completion/continue-as-new. An
  unfinished workflow or a run terminated directly by the server has no completed
  AGENT span yet; its already-exported activity spans can reference that pending
  parent. A hard-killed activity process can likewise lose its in-flight span.
- **2026-10-30:** Uses Temporal's experimental `unsafe_extern_functions` API (also
  used by its OpenTelemetry integration) and FITracer's SDK ID-generator hook.
  Upgrade Temporal/OTel together with the replay and worker-process regression tests.
- **2026-10-30:** Signals, queries, updates, Nexus operations and schedules do not
  receive dedicated spans or sender-context links, so their handler arguments and
  results are not captured separately. Update-with-start instruments the start half
  and its workflow arguments only, not the update's arguments/result; updates to an
  existing workflow do not reparent it or change its
  original suppression decision. Workflow-originated activities and child workflows
  are covered; continue-as-new propagates the previous run.
- **2026-10-30:** On older SDKs without `workflow.is_failure_exception` (including
  1.9.0), custom workflow failure-exception types outside `FailureError` are not
  marked complete on exception. Their Python timeouts normally fail a workflow
  task, not the workflow, and must not emit completion spans. SDK 1.35.0's public
  classifier covers native timeouts and configured workflow failure types.
- **2026-10-30:** Suppression from an outer interceptor wrapping only
  `execute_workflow` is local to that asyncio task, not a workflow-wide policy.
  Separate signal/update/query tasks do not inherit it automatically. A signal can
  schedule an unsuppressed activity/child workflow or terminally fail the workflow,
  exporting its stored input on completion. Suppress the originating client call
  (propagated suppression) or set `FI_HIDE_INPUTS=true` / `FI_HIDE_OUTPUTS=true`
  before initialization, or equivalent `TraceConfig` controls, when content must
  remain hidden across all workflow tasks.
- **2026-10-30:** Asynchronous activity completion is observed only through the
  successful handoff: its TOOL span ends OK with
  `temporal.activity.async_completion=true`, without a result. The remote async
  handle's eventual success/failure is not observed by this interceptor; a workflow
  awaiting it still captures its own final result/error normally.
- **2026-10-30:** Workflow result conversion happens after the inbound interceptor
  returns. An unsupported result or a failing result converter can therefore emit
  an OK completion before serialization fails the workflow task, and its live task
  retry can emit another false OK with the same span ID while the workflow remains
  running. Reproduced on Temporal 1.35.0: one failed workflow task can leave the
  workflow `RUNNING` with two `OK` completions sharing the same span ID. No durable
  exactly-once or post-serialization completion guarantee is provided; return values
  must be supported by the configured converter.
- **2026-10-30:** No durable exactly-once exporter across a worker crash at completion;
  stable IDs let backends deduplicate retried completion exports. Thread-pool sync
  activities are supported; process-pool activity executors are not validated.
- **2026-10-30:** Worker-only roots synthesize a sampled parent context for descendants;
  custom root samplers may choose a different sampling decision for the completed
  workflow span. Prefer instrumented clients with parent-based sampling throughout.
