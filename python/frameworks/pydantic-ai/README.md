# Pydantic AI tracing

Trace Pydantic AI agent runs, model requests, and tools in Future AGI using Pydantic AI's native OpenTelemetry instrumentation.

## Install

Use Python 3.10–3.13 and Pydantic AI 1.0 or later. Install the extra for your model provider; this example uses OpenAI.

```bash
pip install -U traceai-pydantic-ai 'pydantic-ai-slim[openai]>=1.0.0'
```

Pydantic AI 1.0 uses an older OpenTelemetry events API. If you must stay on 1.0, use OpenTelemetry SDK/exporter 1.37.0 and instrumentation 0.58b0. Otherwise, upgrade Pydantic AI and OpenTelemetry together.

## Configure and run

Set your Future AGI credentials and your own model-provider key in the environment:

```bash
export FI_API_KEY='<your-future-agi-api-key>'
export FI_SECRET_KEY='<your-future-agi-secret-key>'
export OPENAI_API_KEY='<your-openai-api-key>'
```

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from pydantic_ai import Agent
from traceai_pydantic_ai import PydanticAIInstrumentor

provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="pydantic-ai-app",
)
PydanticAIInstrumentor().instrument(tracer_provider=provider)

agent = Agent("openai:gpt-4o-mini", instructions="Use the calculator tool.")

@agent.tool_plain
def multiply(left: int, right: int) -> int:
    """Multiply two integers."""
    return left * right

try:
    result = agent.run_sync("What is 17 multiplied by 23?")
    print(result.output)
finally:
    provider.force_flush()
```

Open the `pydantic-ai-app` Observe project in Future AGI. The trace contains an agent span with model-request and tool spans beneath it. Prompts, responses, tool arguments, tool results, and exception details are **not captured by default**.

Configure the instrumentor once at startup. It passes your provider directly to native instrumentation through a semantic/privacy adapter; it does not change the global provider or patch `Agent` methods. Do not also call `Agent.instrument_all()` or instrument the same model calls with a separate provider integration. That can replace these settings or create duplicate model spans. Explicit per-agent instrumentation settings take precedence over this process-wide default.

## Capture content deliberately

To capture message content, tool inputs/results, and structured output, replace the startup call with:

```python
from fi_instrumentation.instrumentation import TraceConfig

PydanticAIInstrumentor().instrument(
    tracer_provider=provider,
    include_content=True,
    config=TraceConfig(hide_error_details=True, pii_redaction=True),
)
```

`TraceConfig` also reads the standard `FI_HIDE_*` and `FI_PII_REDACTION` environment variables. Any input/output/message/text/image hide flag disables **all** native content capture, even with `include_content=True`, because native serialized messages can mix directions and modalities. Error details are always hidden when content is disabled. With content enabled, `TraceConfig.hide_error_details` controls exception messages and stack traces.

Binary content is disabled in native settings. Arbitrary native metadata, tool schemas, formatted log messages, and combined conversation-history attributes are omitted. Model, agent, tool and conversation identifiers remain visible: do not put secrets in names or IDs. These settings affect spans created through this adapter, not other integrations or resource attributes you configure yourself. Regex PII redaction is not a guarantee that arbitrary secrets will be removed from opted-in content.

## Stream an answer

Keep the same provider and startup configuration. Use Pydantic AI's normal asynchronous context manager; do not `await agent.run_stream()` itself.

```python
import asyncio

async def stream_answer():
    async with agent.run_stream("What is 8 multiplied by 9?") as result:
        async for text in result.stream_text(delta=True):
            print(text, end="", flush=True)

try:
    asyncio.run(stream_answer())
finally:
    provider.force_flush()
```

`run`, `run_sync`, `run_stream`, and `iter` retain their native return values and lifetimes. Errors still reach your application. Use `output_type=YourPydanticModel` for structured output, and pass `previous_result.all_messages()` as `message_history` for a continuation.

## Span attributes and usage

| Span | Attributes |
| --- | --- |
| Agent | `gen_ai.span.kind=AGENT`, agent/model identifiers, `gen_ai.aggregated_usage.*` |
| Model request | `gen_ai.span.kind=LLM`, native provider/model identifiers, `gen_ai.usage.input_tokens`, `output_tokens`, and their sum in `total_tokens` |
| Tool | `gen_ai.span.kind=TOOL`, `gen_ai.tool.name`, tool-call ID when supplied |
| Content, when enabled | Native input/output messages plus `input.value` / `output.value` and MIME types; tool arguments/results; final agent output |

Count request tokens from **model spans only**. Agent aggregates sum that run's model requests, not a shared `RunUsage` object's cumulative balance; nested agents have their own totals. Cache/reasoning token fields are retained when the native model reports them and are not added again to total tokens. Unknown measurements are not estimated by this adapter. It exports traces, not metrics or logs.

## Compatibility and teardown

`PydanticAIInstrumentor().instrument(tracer_provider=...)` and `instrument_pydantic_ai(provider, use_builtin=...)` remain available. Both values of the legacy `use_builtin` argument now use native instrumentation. The earlier `pydantic_ai.run.*` attributes and synthetic chunk counters are replaced by native GenAI semantics. Update attribute-based queries accordingly.

The exported legacy `wrap_agent_run`, `wrap_tool_function`, attribute constants, and model-name helper remain importable for existing callers. They are not used by this native route; do not apply the legacy wrappers on top of it.

```python
PydanticAIInstrumentor().uninstrument()
```

Teardown restores the previous process-wide native setting only if this instrumentor still owns it. It does not overwrite settings installed later by your application, shut down your provider, or interrupt spans already in progress.

If no traces appear, confirm you passed the provider returned by `register()`, configured tracing before executing the agent, and did not set `agent.instrument = False`. Flush the provider before a short-lived process exits.

## Development

The package tests use Pydantic AI's test/function models and a localhost OTLP receiver. They require no model keys or remote collector.

```bash
pip install -e '.[dev]'
pytest tests -q
```
