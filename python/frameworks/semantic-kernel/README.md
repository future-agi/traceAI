# traceAI-semantic-kernel

Future AGI tracing for [Semantic Kernel](https://github.com/microsoft/semantic-kernel) **for Python**, built on Semantic Kernel's own OpenTelemetry diagnostics.

> **Experimental upstream diagnostics. Python only.** This is not the Java package (`java/traceai-java-semantic-kernel`), not .NET Semantic Kernel, and not Microsoft Agent Framework (use `traceAI-agent-framework` for that). Semantic Kernel marks its GenAI diagnostics `@experimental`, so the span names and keys below can change in a future Semantic Kernel release.

## Connector support

Use this adapter with Semantic Kernel's `OpenAIChatCompletion` or Azure OpenAI's `AzureChatCompletion`. Both use Semantic Kernel's shared model diagnostics, including streaming. Agent and tool spans come from Semantic Kernel's native diagnostics; this package maps them rather than adding a second tracing layer.

> **Azure AI Inference is not supported by this adapter.** `AzureAIInferenceChatCompletion` is a different connector from `AzureChatCompletion`. Its model calls use Azure SDK tracing and create fresh diagnostics settings per call. `instrument()` does not enable those settings, route the Azure SDK's tracer, or apply this adapter's content and privacy controls to Azure SDK spans. Do not turn on the `SEMANTICKERNEL_EXPERIMENTAL_GENAI_*` flags as a workaround: Azure SDK content recording can remain enabled even with `sensitive=False` or `FI_HIDE_INPUTS` / `FI_HIDE_OUTPUTS`. If your deployment is Azure OpenAI, use `AzureChatCompletion`; that is not a replacement for an Azure AI Inference endpoint.

Other connectors are not a blanket support promise: this adapter only configures Semantic Kernel's shared diagnostics and processes spans whose instrumentation scope is `semantic_kernel` or `semantic_kernel.*`. It does not configure separate provider SDKs or add missing model or retrieval spans.

## What it does

`SemanticKernelInstrumentor().instrument()`:

1. Turns on Semantic Kernel's **shared** GenAI diagnostics in process. You do not export `SEMANTICKERNEL_EXPERIMENTAL_GENAI_ENABLE_OTEL_DIAGNOSTICS`. The shared diagnostics modules build their `ModelDiagnosticSettings` objects when imported, so setting the environment variable afterwards does not update those instances; `instrument()` sets their fields instead. Calling it before or after you build the kernel both work. This does not enable the separate Azure AI Inference path described above.
2. Points Semantic Kernel's module-level tracers at the tracer provider you pass. Semantic Kernel creates them from the *global* provider, and `fi_instrumentation.register()` does not set the global provider by default.
3. Installs a mapping-only span processor ahead of the exporter: span kind, provider alias, total tokens, session, content default.

It does **not** wrap `Kernel.invoke`, `KernelFunction.invoke` or any connector method, opens no spans of its own, and does not depend on `wrapt`. Semantic Kernel creates every span you see.

## Install

```bash
pip install traceAI-semantic-kernel
```

`semantic-kernel` (MIT) is a dependency floor only (`>=1.38.0,<2`); it is not vendored.

## Use

```bash
export FI_API_KEY="YOUR_API_KEY"
export FI_SECRET_KEY="YOUR_SECRET_KEY"
```

```python
from fi_instrumentation import register, using_session
from fi_instrumentation.fi_types import ProjectType
from traceai_semantic_kernel import SemanticKernelInstrumentor

trace_provider = register(project_type=ProjectType.OBSERVE, project_name="my-sk-app")

# Enables native diagnostics in process. Does not wrap Kernel.invoke.
SemanticKernelInstrumentor().instrument(tracer_provider=trace_provider, sensitive=False)

# ... build your Kernel / ChatCompletionAgent as usual ...
with using_session("conversation-123"):   # Semantic Kernel emits no thread id; set the session yourself
    response = await agent.get_response("What is the weather in Paris?")

trace_provider.force_flush()   # short scripts: the batch exporter sends in the background
```

`instrument()` is process-wide and idempotent: calling it twice, or on two instances, installs one processor. `uninstrument()` restores Semantic Kernel's switches and tracers and removes the processor. If you also put a `SemanticKernelSpanProcessor` on the provider yourself, `instrument()` does not take it over: it adds its own (with a warning), and `uninstrument()` removes and shuts down only that one. Use one or the other.

A runnable example with a mocked model (no model key) is in [`examples/basic_agent.py`](examples/basic_agent.py).

## Ordering and other instrumentors

- **Call `instrument()` after any `add_span_processor()` of your own on the provider `register()` returned.** `fi_instrumentation`'s `TracerProvider.add_span_processor` treats the processor chain `register()` set up as a replaceable default: its first call shuts down and empties the whole chain before adding yours (`fi_instrumentation/otel.py`, `TracerProvider.add_span_processor`; existing `fi_instrumentation` behaviour). That also removes this package's processor, while `instrument()` still reports instrumented and Semantic Kernel keeps emitting, so spans export without span kinds, session copy or `TraceConfig` hiding. If you must add a processor later, call `uninstrument()` and then `instrument()` again (covered by `test_later_add_span_processor_drops_ours_until_reinstrumented`).
- **Do not also enable a client-level instrumentor for the same model calls** (`traceAI-openai`, `traceAI-anthropic`, `traceAI-litellm`, ...). Semantic Kernel already puts `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` on its `chat` span, and this package keeps them there. A client instrumentor adds a second `LLM` span for the same request with the same tokens, and the processor only touches Semantic Kernel spans, so Observe's trace and session token sums count every model call twice. Use one or the other: this package for the Semantic Kernel tree, or the client instrumentor alone.

## Sensitive content

These controls apply only to Semantic Kernel-scoped spans. They do not filter spans or logs emitted independently by Azure SDKs or other instrumentors.

> **Warning.** `sensitive=True` turns on Semantic Kernel's *sensitive* diagnostics. Semantic Kernel then puts agent input and output messages, tool-call arguments and tool results on spans, this package adds model-call prompts and completions to the `chat` spans, and all of it is copied to `input.value` / `output.value`. Leave `sensitive=False` (the default) unless you want that text stored in Future AGI.

- With `sensitive=False`, `instrument()` forces Semantic Kernel's sensitive switch off even if your environment or `.env` turned it on (a warning is logged), and the processor removes `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`, `input.value` and `output.value` from Semantic Kernel spans.
- With `sensitive=True`, `TraceConfig` and its environment variables still apply. `instrument(config=TraceConfig(hide_inputs=True))` or `FI_HIDE_INPUTS=true` drops tool arguments, agent and model-call input messages, `input.value` and `input.mime_type`; `hide_outputs` / `FI_HIDE_OUTPUTS=true` drops tool results, agent and model-call output messages, `output.value` and `output.mime_type`. The keys are removed, not replaced with `__REDACTED__`. Without `config`, a `TraceConfig()` is built from the environment when `instrument()` runs. Other `TraceConfig` fields (`hide_input_messages`, `hide_output_messages`, `hide_input_text`, `hide_output_text`, image and embedding limits, `pii_redaction`, `hide_llm_invocation_parameters`) are not applied by this package. A `config` that is not a `TraceConfig` raises `TypeError`.
- Model-call (`chat`) messages: Semantic Kernel writes them only to Python `logging` (`model_diagnostics/decorators.py` `_set_completion_input` / `_set_completion_response`), not to the span. With `sensitive=True`, `instrument()` puts a proxy in front of that module's `logger` that also appends each message to the current `chat` span as `gen_ai.input.messages` / `gen_ai.output.messages` (JSON lists of Semantic Kernel's message dicts), and `uninstrument()` puts the original back. The proxy passes every call on to the real logger unchanged, so the records still reach whatever handlers your Python logging has at INFO. With `sensitive=False` no proxy is installed.
- Errors: Semantic Kernel records the exception on the span (type, message and a stack trace with local file paths). The processor always removes `exception.stacktrace`. With `sensitive=False` it also removes `exception.message` and cuts the ERROR status description down to the exception type (`RuntimeError`), because messages often echo tool arguments or model output. `exception.type`, `error.type` and the ERROR status stay. With `sensitive=True` the message is kept.

## Spans and attributes

Spans Semantic Kernel 1.44.1 emits once diagnostics are on, and what the processor adds:

| Span name | Source (installed `semantic_kernel/`) | Kind set (`fi.span.kind` and `gen_ai.span.kind`) |
|---|---|---|
| `chat <model>` / `text_completions <model>` | `utils/telemetry/model_diagnostics/decorators.py` 37-38, 322-364 | `LLM` |
| `invoke_agent <agent name>` | `utils/telemetry/agent_diagnostics/decorators.py` 35, 174-205 | `AGENT` |
| `execute_tool <plugin>-<function>` the model asked for: it carries `gen_ai.tool.call.id` (`kernel.py` 468-471) or runs as a direct child of `AutoFunctionInvocationLoop` (connectors that send no tool call id, such as Ollama) | `functions/kernel_function.py` 264, `model_diagnostics/function_tracer.py` 54-65 | `TOOL` |
| `execute_tool <plugin>-<function>` with neither (you invoked the function, e.g. `kernel.invoke` / `kernel.invoke_prompt`, or a function your tool calls) | same | `CHAIN` |
| `AutoFunctionInvocationLoop` | `connectors/ai/chat_completion_client_base.py` 137, 256, 410-424 | `CHAIN` |

Typical tree for an agent that calls one tool:

```
invoke_agent Assistant                    AGENT
└─ AutoFunctionInvocationLoop             CHAIN
   ├─ chat gpt-4o-mini                    LLM   (finish_reason tool_calls)
   ├─ execute_tool Weather-get_weather    TOOL
   └─ chat gpt-4o-mini                    LLM   (finish_reason stop)
```

| Future AGI field | Key | Where it comes from |
|---|---|---|
| Span kind | `fi.span.kind`, `gen_ai.span.kind` | Set by the processor (table above). fi-collector reads `fi.span.kind` first. |
| Model | `gen_ai.request.model` | Semantic Kernel, passed through. No `gen_ai.response.model` upstream. |
| Provider | `gen_ai.provider.name` | Copied from Semantic Kernel's `gen_ai.system` when absent. `gen_ai.system` is kept. |
| Input / output tokens | `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | Semantic Kernel, on `chat` spans only (`decorators.py` 421-427). Streaming included (the OpenAI connector requests `include_usage`). |
| Total tokens | `gen_ai.usage.total_tokens` | Derived on `LLM` spans when both parts exist. |
| Token/cost on other spans | `semantic_kernel.usage.*` | Any promoted token/cost key on a non-`LLM` Semantic Kernel span is moved here so Observe's trace-wide sums count each Semantic Kernel model call once. Semantic Kernel 1.44.1 puts none there; this is a guard. It does not cover spans from a client-level instrumentor (see "Ordering and other instrumentors"). |
| Cost | none | Not emitted. Not invented. |
| Session | `session.id` | From `gen_ai.conversation.id` when present (Semantic Kernel 1.44.1 emits none), or from `using_session` / `using_attributes`, which the processor copies onto Semantic Kernel spans at start. |
| User | `user.id` | Only from `using_attributes(user_id=...)`. |
| Tool | `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.description` | Semantic Kernel, passed through. |
| Tool arguments / result | `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` → `input.value`, `output.value` | `sensitive=True` only (`kernel_function.py` 268-269, 283-288). |
| Agent | `gen_ai.agent.id`, `gen_ai.agent.name`, `gen_ai.agent.description`, `gen_ai.tool.definitions` | Semantic Kernel, passed through. |
| Agent messages | `gen_ai.input.messages`, `gen_ai.output.messages` → `input.value`, `output.value` | `sensitive=True` only (`agent_diagnostics/decorators.py` 208-227). Input is captured only when the messages are passed positionally (`decorators.py` 75). |
| Model-call messages | `gen_ai.input.messages`, `gen_ai.output.messages` → `input.value`, `output.value` on `chat` / `text_completions` spans | `sensitive=True` only. Recorded from Semantic Kernel's model-diagnostics log records (see "Sensitive content"); streaming included. A single completion is put on `output.value` as its message object (not a one-item list), which Observe's chat view shows as the assistant reply. |
| Errors | `error.type`, span status | Semantic Kernel sets `ERROR` status on model, agent and tool failures. The processor sets `ERROR` only if `error.type` is present and the status is unset; it never clears a status. |
| Other | `server.address`, `gen_ai.response.id`, `gen_ai.response.finish_reason` (singular), `gen_ai.request.*` sampling keys, `sk.available_functions` | Semantic Kernel, passed through. |

Not available: retrieval spans (memory/vector connectors were not inspected), cache and reasoning token counts, cost, a thread or conversation id from Semantic Kernel itself.

## Failure behavior

- Collector down or slow: the OTLP exporter logs and drops; your kernel call returns normally (covered by `test_collector_down_never_breaks_the_kernel`).
- The processor swallows its own exceptions and never raises into the OpenTelemetry SDK.
- `instrument()` does not raise into your startup when a Semantic Kernel module it expects cannot be imported (the diagnostics are experimental upstream and may move in a later release). It logs a WARNING naming the module, skips it, and instruments the rest. A provider that is not an OpenTelemetry SDK `TracerProvider` (for example the global `ProxyTracerProvider` before any provider is set) gets a WARNING and nothing is changed. `uninstrument()` is safe in both cases.
- A full batch queue (default 2048) drops spans inside OpenTelemetry; the agent does not see it.
- If a stream is abandoned mid-way, Semantic Kernel owns the span end; this package does not open a second span to close it.

## Compatibility

Requires Python 3.10–3.13 and `semantic-kernel>=1.38.0,<2`. Earlier Semantic Kernel releases use different diagnostic operation names and agent attributes. The diagnostics are experimental upstream; review this adapter's compatibility before upgrading Semantic Kernel.

If you install with `uv`, use `uv pip install --prerelease=allow traceAI-semantic-kernel`: Semantic Kernel requires the pre-release `azure-ai-agents` dependency.
