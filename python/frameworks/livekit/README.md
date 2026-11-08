# traceAI-livekit

OpenTelemetry instrumentation for LiveKit agents integrated with Future AGI.

This package provides automatic attribute mapping from LiveKit's native OpenTelemetry instrumentation to Future AGI's semantic conventions, ensuring rich and structured traces in your dashboard.

## Installation

```bash
pip install traceai-livekit
```

## Quick Start

To instrument your LiveKit agent, simply initialize `fi-instrumentation` and enable the attribute mapping **inside your agent's entrypoint**.

```python
from livekit.agents import AgentServer, JobContext
from fi_instrumentation.otel import register, ProjectType
from traceai_livekit import enable_http_attribute_mapping

server = AgentServer()

@server.rtc_session()
async def entrypoint(ctx: JobContext):
    # 1. Initialize TraceAI (inside entrypoint for multiprocessing safety)
    register(
        project_name="My LiveKit Agent",
        project_type=ProjectType.OBSERVE,
        set_global_tracer_provider=True,
    )
    
    # 2. Enable LiveKit attribute mapping
    # This automatically hooks into LiveKit's telemetry system
    enable_http_attribute_mapping()
    
    # ... rest of your agent logic ...
    await ctx.connect()

if __name__ == "__main__":
    server.run()
```

`enable_http_attribute_mapping()` (or `enable_grpc_attribute_mapping()`) hands the provider to LiveKit for you. If you call
LiveKit's `set_tracer_provider` yourself on the provider `register()` returned, call it after `enable_*`: before it, the call
drops the Future AGI exporter. LiveKit installs its privacy filter once per provider, so an `allow_pii` passed in a later call is
ignored. To keep conversation content out of your traces, set `LIVEKIT_TELEMETRY_ALLOW_PII=false` before calling `enable_*`
(livekit-agents 1.8 and later).

### Flushing at the end of a job

On Linux a LiveKit job process ends without running `atexit`, and LiveKit flushes only its own processors, so the spans
still queued when a job ends, such as the root `agent_session` span, used to never reach Future AGI. `enable_*` now sends
them twice over:

- **When the job shuts down**, if `enable_*` runs inside a job (the usual place, the entrypoint): this sends the voice
  trace, `agent_session` included, as soon as the call ends.
- **When the process exits**, just before it stops: this sends what LiveKit ends after the shutdown callbacks, its own
  `job_shutdown` span, and the job's root `job_entrypoint` span when tracing is set up before the job starts. With
  set-up inside the entrypoint, LiveKit starts `job_entrypoint` before tracing is on, so it is never recorded and the
  job trace arrives rooted at `job_shutdown`.

Each waits at most 3 seconds for the collector, well inside the 10 seconds LiveKit gives a job process to exit, so its own
clean-up still runs when the collector is slow.

If you set tracing up outside a job (for example at import time, in the job process), only the process-exit flush runs.
To also send each call's trace as soon as the call ends, call `flush_on_job_shutdown()` in each job entrypoint:

```python
from traceai_livekit import flush_on_job_shutdown

@server.rtc_session()
async def entrypoint(ctx: JobContext):
    flush_on_job_shutdown()
    ...
```

Do not pass `provider.force_flush` to `ctx.add_shutdown_callback`: LiveKit calls it with the shutdown reason, which
`force_flush` takes as its timeout, so it fails and nothing is flushed.

Limits:
- A process LiveKit kills (for example when a worker drains and the job overruns its exit time) sends nothing more.
- Before livekit-agents 1.3, LiveKit closes the session at the same time as the job-shutdown flush, so that flush can miss
  `agent_session`; the process-exit flush still sends it.
- A second `AgentSession` in one job that is still closing when the shutdown callbacks run is sent by the process-exit
  flush, not the job-shutdown one.
- In LiveKit Cloud observability mode the session's parent span is recorded on LiveKit's own provider and never reaches
  Future AGI, so the trace has no stored root there.

## Recording metadata on a session trace

`record_egress` records metadata only: it never records, opens, downloads or uploads media. Each call creates and promptly ends one INTERNAL `livekit.recording.snapshot` span with FI kind `CHAIN`, parented to the supplied session span. Multiple recordings and start/stop/update responses remain separate snapshots instead of overwriting a root.

Use an explicitly owned session root/envelope before starting recording. An ended span or a `NonRecordingSpan` holding its saved context is valid for later callbacks. Without `session_span`, the helper uses the current valid OTel span; it does not discover the native LiveKit root or search ancestors. Without a valid parent it returns `False` and creates no orphan recording trace.

```python
from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace
from traceai_livekit import record_egress, wrap_egress_client

# provider is the provider from register(); enable_* has already configured mapping.
policy = TraceConfig()
tracer = provider.get_tracer("my-agent-recordings")
root = FITracer(tracer, config=policy).start_span(
    "LiveKit Agent Session", fi_span_kind="agent"
)
saved_parent = trace.NonRecordingSpan(root.get_span_context())

# Bind the egress SERVICE, not the owning API context manager. Requests and
# response objects are forwarded unchanged; only successful responses are read.
egress = wrap_egress_client(
    api_owner.egress, session_span=saved_parent, room_name=room_name,
    conversation_id=conversation_id,  # optional: may differ from room_name
    tracer=tracer, trace_config=policy,
)
with trace.use_span(root, end_on_exit=False):
    info = await egress.start_room_composite_egress(start_request)
    # Start AgentSession under this context too, where the LiveKit mode supports it.

# Later, for a final EgressInfo-like response ALREADY received by your application:
recorded_locally = record_egress(
    final_info, session_span=saved_parent, room_name=room_name,
    conversation_id=conversation_id, tracer=tracer, trace_config=policy,
)
# End root at actual session shutdown; the application owns API stop/close.
# See examples/egress_agent.py for a bounded, idempotent shutdown callback.
```

`True` means a snapshot was locally created and ended. It does **not** confirm collector delivery, recording completion, an available final file, or a playable URL. `False` also covers tracing suppression, unsampled children, invalid/missing ids, room mismatch and instrumentation failure. Delegate failures/cancellation are not swallowed or logged by the proxy. A failed or cancelled start with no returned id can leave server-side state unknown; the adapter does not retry or discover that state.

### Metadata contract

These are package-local custom attributes, not standardized recording semantics or a dashboard-player API:

| FI export-copy attribute | Meaning |
| --- | --- |
| `livekit.recording.operation` | `snapshot` or a fixed captured method name |
| `livekit.recording.egress_id` | Bounded nonempty response id |
| `livekit.recording.status` | Recognized `EGRESS_*` label or decimal enum number; zero is retained, numbers are not interpreted |
| `livekit.recording.started_at_unix_nano`, `livekit.recording.ended_at_unix_nano` | Positive int64 Unix nanoseconds, only when supplied |
| `livekit.recording.room_name` | Explicit room, otherwise response room |
| `gen_ai.conversation.id` | Explicit conversation id, independently masked; otherwise the fully masked room passed through conversation masks. A withheld room supplies no implicit conversation; a redacted room supplies only its replacement |
| `livekit.recording.file_locations` | At most eight credential-free object locators or safe relative filenames |
| `livekit.recording.stream_locations` | At most eight stream origins; paths may be bearer keys and are omitted |

The helper's EgressInfo-like contract reads only `egress_id`, `room_name`, `status`, `started_at`, `ended_at`, `file_results` (`location`, or `filename` only when location is absent) and `stream_results` (`url`). Use a caller-normalized response if your SDK differs. Timestamps must already be Unix nanoseconds; there is no magnitude-based conversion. Missing/zero/invalid timestamps, unknown status text, unsupported legacy singular/segment/image results and absent locations are omitted. A successful start/stop may still be STARTING/ENDING with no final location; no duration or completion is inferred.

Ids/room/conversation are at most 256 characters; locations at most 2048. Oversize values are omitted, not truncated. Only the first eight elements of each results collection are examined. A nonempty explicit room that differs from the response room refuses capture. No request, upload configuration, auth header, protobuf serialization or media bytes are inspected.

### Privacy and adapter limits

The recording helper controls only the metadata it writes. Attributes, events or links that your own samplers, span processors or code add are exported as written; the recording policy does not filter them, except the helper's reserved keys, which export always re-checks.

- Helper-authored values pass existing FI `TraceConfig.mask` gates for both the destination alias and source key **before** span creation. Locations also pass the `output.value` policy, so output hiding applies. Both the helper policy and any supplied FITracer policy apply. A withheld helper value is not placed into attributes, events, links, exception text, logs, baggage, context keys, resources or the helper's boolean result.
- FI export re-applies those policies by **key name**, not writer provenance, to the nine source/alias pairs in the metadata table (including `lk.pii.room_name` and `gen_ai.conversation.id`) and `session.id`. This also covers reserved attribute keys in application-created events and links; unrelated keys, event names, exception text and link targets are not filtered by the recording policy. Missing sources do not recreate aliases. A masking `Exception` replaces the affected value with `__REDACTED__` rather than exporting raw metadata or exception text. Export rechecking may transform an already-masked value again: custom masks should be idempotent.
- Capture uses a per-call copy of the ordinary OTel SDK tracer without changing the supplied tracer/provider, sampler, or application callbacks. A lifecycle adapter retains the started child for `finally` cleanup and carries the policy tuple on a local instrumentation-scope copy for delayed/batched FI export. Normal scope fields and attributes are unchanged; no response values, policy data or identity registry are serialized. Application samplers/processors are not sandboxed or restricted. Unknown/custom tracers, subclasses, nested tracer proxies or inaccessible FI configuration fail closed before starting a span. Minimum-FI/SDK and LiveKit-copy compatibility still require verification.
- Through creation **and ending**, the helper attaches a fresh context containing only the selected parent, including its trace state. It does not inherit ambient FI session/user/metadata attributes or baggage. Suppression is checked first; child termination and restoration of the caller context run in `finally`, including when `start_span` fails after SDK allocation. Initial extraction/masking failures still create no child.
- The helper writes sensitive live-span values only under `lk.pii.*`; its aliases are created on the FI export copy. LiveKit PII opt-out and Cloud redaction can therefore remove helper-authored ids, room, conversation and locations before aliasing. The exact `livekit.recording.snapshot` and supported `livekit.recording.<method>` names remain reserved across tracer scopes: loss of the operation attribute to sampling or SDK limits cannot enable generic room-to-conversation fallback. Ordinary non-recording spans keep their existing mapping.
- File locations allow `http`, `https`, `s3`, `gs` or safe relative names. Stream locations allow `http`, `https`, `rtmp`, `rtmps`, `rtsp`, `rtsps`, but only their origin survives. Malformed/unsupported locations, controls, backslashes and unsafe relative paths are omitted. All query parameters, fragments and userinfo are dropped. Credential-free locators are intentionally **not usable presigned playback links**. This stricter local projection supplements FI masks; it does not claim reuse of an independently verified shared URL/credential sanitizer.
- The opt-in proxy recognizes coroutine functions named `start_room_composite_egress`, `start_web_egress`, `start_participant_egress`, `start_track_composite_egress`, `start_track_egress`, `stop_egress`, `update_stream`, and `update_layout`. This is a structural service contract, not a verified SDK-version matrix. Non-coroutine/decorated methods and unrelated attributes pass through; non-info responses produce no snapshot. Use the manual helper when recognition is unsupported.
- The wrapper freezes its parent at creation; an unbound wrapper stays capture-disabled even if a different task later has a current span. Rewrapping with omitted/identical options is idempotent; conflicting supplied bindings raise a value-free `ValueError`. No global room registry or response history is kept.
- Calls on the original service, other processes and server-side completion events are not intercepted. There is no polling or webhook server. Your application supplies final updates and distributed trace-context propagation. The API owner remains responsible for shutdown; special-method/context-manager protocols are not proxied.
- The envelope is not a promise that FI receives LiveKit Cloud's native root. The earlier Cloud-root limitation still applies. Native `AgentSession` parent inheritance and real SDK compatibility must be validated for the deployed mode.

The pinned development lock contains LiveKit Agents 1.8.6, API 1.2.1, protocol 1.1.27, FI 1.3.0 and OpenTelemetry SDK 1.44.0. Those pins are **verification targets, not executed compatibility results for this feature**. Fake-service tests do not establish SDK schemas, timestamp semantics, native-session linkage or a playback UI.

## Compatibility

*   **livekit-agents**: 1.x. 1.8 is the tested version (it needs Python 3.10 or later). Since 1.7, LiveKit names its content
    attributes `lk.pii.*` (for example `lk.pii.user_input`); both those names and the earlier ones are mapped to `input.value`
    and `output.value`.
*   **Python**: 3.10 to 3.13.
*   **fi-instrumentation-otel**: 1.3.0 or later; the recording helpers' privacy tests fail with 1.1.0. 1.3.0 needs Python 3.10
    or later and sets the default base64 image cap to 0 (see its changelog). 1.0 moved span attributes from the `llm.*` names to the OpenTelemetry GenAI `gen_ai.*` names, and
    renamed the `SpanAttributes` constants with them. Code that only calls `register()` is unaffected; if your code sets or reads
    those attributes directly, see the
    [mapping from the old names](https://github.com/future-agi/traceAI/blob/main/docs/OTEL_GENAI_SEMANTIC_CONVENTIONS.md#migration-from-openinference-llm).

## Features

*   **Automatic Attribute Mapping**: Converts LiveKit attributes (like `lk.user_input`, `gen_ai.usage.input_tokens`) to Future AGI standard attributes (`input.value`, `llm.tokenCount.prompt`).
*   **Raw Data Preservation**: Keeps original LiveKit attributes and adds `raw.input` / `raw.output` for full debugging context.
*   **Multiprocessing Support**: Designed to work with LiveKit's worker process model.
*   **Event Parsing**: Extracts LLM outputs from `gen_ai.choice` events when available.

## Examples

Check the `examples/` directory for agent examples (validate recording SDK compatibility before deployment):
*   `simple_agent.py`: A basic voice assistant agent.
*   `egress_agent.py`: An agent with opt-in Egress metadata capture, an explicit session envelope and bounded stop/close cleanup. The modern `LiveKitAPI().egress` request shape and native-session linkage require the deployment's SDK/mode validation.
