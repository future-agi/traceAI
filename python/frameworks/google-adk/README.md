# Google ADK tracing

Send Google ADK's native agent, model, tool, and workflow spans to Future AGI with FI span kinds and content controls.

## Install

Use Python 3.10–3.12. The package supports ADK 1.2.1 through 2.x and lets ADK select its compatible Google GenAI dependency.

```shell
pip install -U traceai-google-adk
```

Set `FI_API_KEY` and `FI_SECRET_KEY` in your application's environment. For Gemini, also supply your own `GOOGLE_API_KEY` and set `GOOGLE_MODEL` to a model available to your account.

## Configure tracing

Configure once at startup, before running agents. Use this adapter alone: do not combine it with another ADK or Google GenAI instrumentor.

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_google_adk import GoogleADKInstrumentor

provider = register(
    project_name="google_adk",
    project_type=ProjectType.OBSERVE,
)
GoogleADKInstrumentor().instrument(tracer_provider=provider)
```

`GoogleADKInstrumentor` uses ADK's existing OpenTelemetry spans; it does not wrap agent execution or replace streaming generators. It uses the provider you supply and leaves provider shutdown to your application. Only traces are exported; the adapter does not configure a metrics or logs pipeline.

## Run an agent

Add this after the tracing setup:

```python
import asyncio
import os

from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner
from google.genai import types


def multiply(left: int, right: int) -> int:
    """Multiply two integers."""
    return left * right


async def main():
    agent = Agent(
        name="calculator",
        model=os.environ["GOOGLE_MODEL"],
        instruction="Use the multiply tool for multiplication.",
        tools=[multiply],
    )
    runner = InMemoryRunner(agent=agent, app_name="calculator")
    try:
        await runner.session_service.create_session(
            app_name="calculator", user_id="example-user", session_id="example-session"
        )
        async for event in runner.run_async(
            user_id="example-user",
            session_id="example-session",
            new_message=types.Content(
                role="user", parts=[types.Part(text="What is 7 times 8?")]
            ),
        ):
            if event.is_final_response() and event.content:
                for part in event.content.parts or []:
                    if part.text:
                        print(part.text)
    finally:
        if hasattr(runner, "close"):
            await runner.close()


try:
    asyncio.run(main())
finally:
    provider.force_flush()
    provider.shutdown()
```

In Future AGI, open **Observe > Tracing** and select `google_adk`. Expand the native workflow/agent spans to inspect model calls, tool calls, timing, errors, and token counts supplied by ADK.

## Capture content deliberately

The default configuration hides inputs, outputs, and exception details. Session identifiers, agent/tool names, model names, and token counts remain visible; use non-sensitive names and identifiers.

To capture prompt, response, and tool content, pass an explicit configuration at setup:

```python
from fi_instrumentation import TraceConfig

GoogleADKInstrumentor().instrument(
    tracer_provider=provider,
    config=TraceConfig(hide_inputs=False, hide_outputs=False),
)
```

On ADK 2.13's default telemetry schema, also enable content on **spans**, not log events, before running the application:

```shell
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_ONLY
```

ADK's own content settings still apply: the adapter cannot recover content that ADK does not emit. `TraceConfig()` reads the `FI_HIDE_*` environment settings. Fine-grained input/output hiding conservatively hides the corresponding native JSON payload, rather than leaking content through an unmasked raw attribute. Raw SDK configuration, HTTP headers, and unrecognized ADK attributes are not forwarded.

## Upgrading from the execution-wrapper integration

Keep the same `GoogleADKInstrumentor().instrument(tracer_provider=...)` entry point. Span names and nesting now follow ADK's native schema rather than `invocation [app]` wrappers, and content is private unless you explicitly opt in. Update saved name filters accordingly. On the legacy schema, a `call_llm` container with a native model child is classified as `CHAIN`; model usage is counted only on the model span, not again on its containers.

Use current ADK for streaming and cancellation fixes. The adapter preserves upstream execution and cleanup behavior rather than patching older ADK generators. Initialize and uninstrument between runs, not during an active invocation. `GoogleADKInstrumentor().uninstrument()` restores ADK's original tracer bindings without shutting down your provider.
