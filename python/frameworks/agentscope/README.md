# traceAI AgentScope

OpenTelemetry instrumentation for AgentScope 1.0.21–1.x and 2.x, using `FITracer`
and `TraceConfig`. Tested with the real AgentScope 1.0.21 and 2.0.11 packages;
the tests use a loopback OpenAI-compatible HTTP server, not paid API calls.

## Install and use

```sh
pip install traceAI-agentscope
```

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_agentscope import AgentScopeInstrumentor

provider = register(project_type=ProjectType.OBSERVE, project_name="agentscope-demo")
AgentScopeInstrumentor().instrument(tracer_provider=provider)
# Create/use AgentScope agents, toolkits, models and pipelines normally.
```

The package advertises the standard `opentelemetry_instrumentor` entry point
`agentscope`. With `FI_API_KEY` and `FI_SECRET_KEY` set, the installed core agent
and this integration can be enabled without application changes using
`opentelemetry-instrument python app.py`. It does **not** add itself to the
separate traceAI auto registry: `fi-instrument` alone enables transport coverage,
not AgentScope library spans. This package itself does not install a global
provider, exporter, signal handler or network client.
Repeated `instrument()` calls use OpenTelemetry's once-per-process instrumentor
lifecycle. The first provider/config wins until an explicit `uninstrument()`.
The auto bootstrap's disable-after-enable restriction still applies.

## Coverage

| Surface | Span kind | Mechanism |
| --- | --- | --- |
| 1.x agent `reply` (including through `agent(...)`) | `AGENT` | Public reply wrappers on existing and future subclasses |
| 2.x `Agent.reply` / `reply_stream` | `AGENT` | Native `MiddlewareBase.on_reply` for replies; public stream wrapper for deterministic close, including existing agents |
| Toolkit `call_tool_function` (1.x) / `call_tool` (2.x) | `TOOL` | Public API wrapper |
| `ChatModelBase` and model subclass `__call__` | `LLM` | Public API wrappers, including later-defined subclasses |
| 1.x sequential/fanout functions and classes | `CHAIN` | Function and internal alias wrappers |
| 2.x `GoalPipeline` / `TeamPipeline.reply_stream` | `CHAIN` | Public API wrapper |

AgentScope 1.x pre/post reply hooks do not deliver an error/cancellation callback;
using those alone would leak spans. Its optional native OTel decorators are left
unchanged and disabled by default. AgentScope 2.x's native middleware protocol is
used for agents; direct models, tools and pipelines still need wrappers because
native middleware only sees calls routed through an agent.

Spans include `gen_ai.span.kind`, `traceai.capture.layer=library`, operation,
agent/tool identity, flattened input/output messages, model/provider, a whitelist
of invocation parameters and tool schemas, response ID and token/cache usage
when supplied. Outputs contain text and tool-call metadata, not media bytes.
No arguments are mutated or generators consumed ahead of the caller. Responses
and chunks retain their identity; streaming results use a transparent proxy with
`__anext__`, `asend`, `athrow`, `aclose` and delegated helper attributes. Context
is active only during advancement, not while application code handles a chunk.
A context marker prevents wire double-capture even through intermediate native
SDK spans. LLM sessions also set standard OpenTelemetry suppression while the
model runs or advances its stream, preventing duplicate provider-instrumentor
spans (including `traceai_openai`). Direct SDK calls outside that operation still
trace normally. Agent/tool/chain spans do not suppress wire capture.

Content capture remains **on by default**. All attributes go through FI masking
and bounded best-effort structural/media scrubbing. The core agent's **shared
export-time sanitizer** and process-wide S5 known-secret registry are the
privacy boundary. Enabled transport hooks observe outgoing credentials even when
AgentScope suppresses the wire span or provider instrumentor, including dynamically
resolved SDK API keys and headers supplied through `UserDict`. Every registered
value is scrubbed across later model, agent, chain and unrelated application spans,
not just the trace that sent it. AgentScope does not inspect SDK clients, headers,
credential holders or key providers, and keeps no per-session credential metadata
or parent/child secret inheritance. It currently sees no extra credentials outside
transports that would require an integration-owned `register_secret` call.
Oversized text is omitted in full rather than exporting a prefix that could cut
through a credential. This package adds no competing exporter/processor.
Exception spans record the error type only, not messages or stack traces.
Instrumentation errors fail open without retrying the application operation.
Library spans measure framework lifecycle (including its own parsing/retries),
not transport-byte timing; R4-7 transport timing belongs to the wire layer.

## Tests

From the repository, install the local FI SDK and this package's test extra into
an isolated environment. Run `python -m pytest tests -q` from this directory.
Tests require no real credentials or external services; zero-code cases launch
isolated child processes. The checked matrix is Python 3.12 with
`tests/requirements-v1.txt` or
`tests/requirements-v2.txt`. Install one of these requirements files alongside
editable installs of the repository's `python/` SDK, `python/frameworks/openai/`
co-instrumentor, and this package. `test_regressions.py` covers custom auth-header
echoes with core S5 transport observation across stored/per-call headers,
dictionaries, mapping proxies, HTTPX containers and `UserDict`, including echoes
in agent/chain spans. Per-call tests compare with a baseline with both AgentScope
and transport hooks disabled, and check transmitted headers and unchanged responses.
`test_zero_code.py` starts fresh baseline and `opentelemetry-instrument` processes,
using real AgentScope/OpenAI clients and a loopback OTLP collector. Streaming and
non-streaming cases cover `UserDict` on both versions and an asynchronous SDK key
provider on 2.x (the installed OpenAI 1.x does not support that provider API).
They assert baseline response/header parity, exactly one key-provider invocation,
no secret anywhere in serialized OTLP, no duplicate wire span, and redaction in a
later unrelated native application trace. Each test isolates the core registry
so earlier observations cannot mask a missed credential. `test_privacy.py` checks
that projection does not inspect client or mapping hooks, explicit core secret
registration, and the documented manual-only boundary and hide controls.
Regression tests also cover privacy at text/stream-accumulation limits, both
provider-instrumentor installation orders with/without wire capture, and explicit
public agent/pipeline stream closure after a real model text delta. The 1.x
matrix deliberately uses MCP 1.x, OpenAI 1.x and SQLAlchemy's asyncio extra,
plus tqdm: upstream 1.0.21 otherwise has import/streaming failures even without
instrumentation. These compatibility choices are not silently imposed on apps.

## Known limitations

- **2026-10-30 — Manual / no-transport privacy boundary (ROUND5 B2 OTHER).**
  A standalone `AgentScopeInstrumentor`, or a one-line init path with transport
  hooks disabled, cannot discover runtime credentials from SDK clients, dynamic
  key providers or `extra_headers` (including `UserDict`). Without the agent's
  transport observation, the core registry knows only seeded FI/environment
  credentials and explicitly registered values; an unregistered credential echoed
  as ordinary text can remain visible. The same limitation applies to unmarked
  secrets that never cross an instrumented transport and application-transformed
  secrets such as split or re-encoded values. Use the zero-code agent with its
  transport hooks enabled, or call `fi_instrumentation.register_secret(value)`
  for known out-of-band values. For sensitive content beyond recognized values,
  use `FI_HIDE_INPUTS`, `FI_HIDE_OUTPUTS`, `FI_HIDE_INPUT_TEXT`,
  `FI_HIDE_OUTPUT_TEXT`, `FI_HIDE_INPUT_IMAGES`, and
  `FI_HIDE_LLM_INVOCATION_PARAMETERS` (or equivalent `TraceConfig` settings).
  Hide controls are opt-in; content capture remains on by default. S5 registration
  retains at most 1024 values of eight or more characters, evicting least recently
  observed/registered values; it is not arbitrary secret inference.

Dated 2026-10-30 (the following three telemetry limitations remain confirmed by
independent verification; they are not claimed fixed by the authentication-header
regressions):

- Handled AgentScope 2 error outcomes emitted only as
  `ReplyEndEvent(finished_reason=ReplyFinishedReason.ERROR)` and an error-finished
  `AssistantMsg` may export `OK`: event/types-module classes are not currently
  included in safe outcome extraction. Raised exceptions still mark spans as
  errors; the application's handled result is unchanged.
- Positional capture metadata is incomplete: AgentScope 1 functional pipelines
  taking `(agents, msg)` capture the first argument rather than the message, and
  both supported versions omit positional model tool schemas. Use `msg=` and
  `tools=` respectively when that telemetry is needed; execution is unchanged.
- A recoverable iterator-protocol error such as initial `asend(non_none)` can
  prematurely finish a span as `ERROR` even though the native async generator
  remains usable. Later valid consumption still works but cannot update that
  finished span's output. This rare protocol variant is not yet modeled.

- AgentScope 1.0.21's unconstrained transitive dependencies are not universally
  compatible: MCP 2.x breaks its imports; SQLAlchemy 2.1 without greenlet breaks
  its eager asyncio import; tqdm is undeclared; OpenAI 3.41's `ChoiceDelta.audio`
  default breaks its streaming parser. The recorded 1.x test requirements avoid
  these upstream issues. The integration does not change native retries/errors.
- This package uses the shared FI SDK wire marker and privacy helpers from the
  current repository. It must ship with a compatible FI SDK, not an older wheel
  lacking those APIs. It has not been independently verified across every
  Python/provider/AgentScope version allowed by its dependency ranges.

- AgentScope releases before 1.0.21, 3.x, realtime agents, embeddings, formatter
  calls and third-party agent subclasses overriding 2.x public entry points
  without delegating to `Agent` are not covered. Unsupported versions fail open.
- As with Python patch-based instrumentors, a function reference imported before
  instrumentation can bypass patched module aliases. Instrument before importing
  standalone pipeline functions. Normal model/agent instances created earlier
  are supported.
- Do not simultaneously enable AgentScope's own native OTel tracing decorators
  (1.x) or `TracingMiddleware` (2.x) for the same operation: those independently
  produce additional native spans. This integration never disables user tracing.
- Streaming uses a proxy, not exact `is` identity of the underlying iterator.
  Dropping an opened stream without exhausting or explicitly closing it cannot
  guarantee a completed span; instrumentation does not read/close it for you.
  A never-advanced async-generator API creates no span. Explicit early close
  records `traceai.stream.incomplete=true` without inventing an error and ends
  still-open descendant telemetry at the same boundary. This does not drive or
  force-close delegated application generators; native cleanup remains unchanged.
- Capture is bounded to 64 messages/blocks, 16,384 characters per text field,
  and eight levels/256 nodes for structured tool schemas/arguments. Oversized
  text fields and combined text/stream-delta output are omitted, not truncated.
  Non-text media and unknown application objects are omitted, not stringified.
  Arbitrary provider-specific parameters and raw request/response bodies are not
  recorded.
- Provider attribution is based on the model wrapper type; custom compatible
  endpoints can use another underlying provider. Tool execution errors returned
  as ordinary responses retain framework semantics rather than being raised.
- A patch layered by another instrumentor after this one is not clobbered during
  removal. Our retained inner wrapper becomes inert; reinstalling several such
  interleaved instrumentors is not a supported configuration.
