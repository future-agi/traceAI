# Trace Fireworks with the OpenAI integration

Use `traceAI-openai` to trace Fireworks' OpenAI-compatible Python API. You do not need a separate Fireworks instrumentor for new applications.

## Install

Use Python 3.10–3.13 and OpenAI SDK 1.69.0 or later.

```bash
pip install --upgrade traceAI-openai 'fi-instrumentation-otel>=1.2.2' openai
```

Set `FIREWORKS_API_KEY`, `FIREWORKS_MODEL`, `FI_API_KEY` and `FI_SECRET_KEY` in your application environment. Use a Fireworks model ID available to your account.

## Trace a request

```python
import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from openai import OpenAI
from traceai_openai import OpenAIInstrumentor

provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="fireworks-app",
)
OpenAIInstrumentor().instrument(tracer_provider=provider)

try:
    with OpenAI(
        api_key=os.environ["FIREWORKS_API_KEY"],
        base_url="https://api.fireworks.ai/inference/v1",
    ) as client:
        response = client.chat.completions.create(
            model=os.environ["FIREWORKS_MODEL"],
            messages=[{"role": "user", "content": "Suggest two names for a weather app."}],
        )
        print(response.choices[0].message.content)
finally:
    provider.shutdown()
```

In a long-running application, shut down the provider only when the application stops. Use `AsyncOpenAI` for asynchronous requests; the same instrumentor handles both clients and streaming responses.

The span includes the Fireworks provider, model, elapsed time, messages, tool-call arguments and token usage when returned by the API. Tool-call arguments belong to the model span; this does not trace the execution of your tool functions. This route covers the OpenAI-compatible API, not the separate `fireworks-ai` SDK or its training and management APIs.

## Protect request and response content

Tracing captures content by default. Pass a `TraceConfig` when enabling the instrumentor to hide sensitive content while retaining model and usage metadata:

```python
from fi_instrumentation import TraceConfig

OpenAIInstrumentor().instrument(
    tracer_provider=provider,
    config=TraceConfig(
        hide_inputs=True,
        hide_outputs=True,
        hide_llm_invocation_parameters=True,
        hide_error_details=True,
    ),
)
```

Use this call **instead of** the default instrumentation call above, not after it. These settings change telemetry, not the request sent to Fireworks.

## Migrate from `traceAI-fireworks`

`FireworksInstrumentor` is deprecated but its import, `instrument()` and `uninstrument()` entry points remain available. Existing applications using the older OpenAI SDK floor of 1.0.0 can continue using it while upgrading.

1. Upgrade to OpenAI SDK 1.69.0 or later and install `traceAI-openai`.
2. Replace the `FireworksInstrumentor` import and setup with `OpenAIInstrumentor` as above. Move your existing `tracer_provider` and `TraceConfig` arguments to that call.
3. Remove the old setup and uninstall `traceAI-fireworks` after checking that nothing else in your application imports it.

If both updated instrumentors are enabled during migration, `OpenAIInstrumentor` owns the request span. Its provider and privacy configuration apply; the deprecated instrumentor does not add a second span. Uninstrumenting either route does not disable the other route.

## References

- [Fireworks OpenAI compatibility](https://docs.fireworks.ai/tools-sdks/openai-compatibility)
- [traceAI OpenAI integration](https://github.com/future-agi/traceAI/tree/dev/python/frameworks/openai)
