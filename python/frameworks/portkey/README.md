# Portkey tracing

Trace synchronous and asynchronous Portkey chat and saved-prompt completions with Future AGI. Streaming spans stay open until exhaustion, close, or failure.

## Install

```bash
python -m pip install --upgrade traceAI-portkey portkey-ai
```

The integration supports Portkey 1.1.2 and later. Use the current Portkey SDK for async support.

## Configure tracing

Set `FI_API_KEY`, `FI_SECRET_KEY`, `PORTKEY_API_KEY`, and `PORTKEY_VIRTUAL_KEY` in your application's environment or secret manager.

```python
import os
from fi_instrumentation import TraceConfig, register
from fi_instrumentation.fi_types import ProjectType
from portkey_ai import Portkey
from traceai_portkey import PortkeyInstrumentor

provider = register(project_type=ProjectType.OBSERVE, project_name="portkey_project")
PortkeyInstrumentor().instrument(
    tracer_provider=provider,
    config=TraceConfig(
        hide_inputs=True,
        hide_outputs=True,
        hide_llm_invocation_parameters=True,
    ),
)
client = Portkey(
    api_key=os.environ["PORTKEY_API_KEY"],
    virtual_key=os.environ["PORTKEY_VIRTUAL_KEY"],
)
completion = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Write a six-word story about a robot discovering music."}],
)
print(completion.choices[0].message.content)
provider.force_flush()
```

These privacy settings omit prompt and response content, invocation parameters, and error details. Disable them only if your data policy allows recording that content. Transport headers and routing credentials are excluded from request attributes.

Configure one tracer provider for your application. If you already have one, pass it to `instrument()` rather than calling `register()` again. Do not enable Portkey's separate framework-telemetry provider alongside this setup. An OpenAI instrumentor is not required for native Portkey chat or saved-prompt completions.

## Streaming and async calls

The same instrumentation covers `AsyncPortkey` and `client.prompts.completions.create(...)`. It does not instrument embeddings or Responses API calls.

Always consume or close streams. A stream that is abandoned without either cannot export its completion span.

```python
stream = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Suggest three names for a neighborhood bakery."}],
    stream=True,
)
try:
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            print(chunk.choices[0].delta.content, end="")
finally:
    stream.close()
provider.force_flush()
```

For `AsyncPortkey`, await the request, use `async for`, and close with `await stream.close()` in `finally`. Closing early leaves success unset; exhaustion records success, and creation or iteration failures (including cancellation) record errors. Usage attributes are present only when the upstream response supplies usage.

Open **portkey_project** in Future AGI **Observe** to inspect your spans. Call `PortkeyInstrumentor().uninstrument()` to restore all patched chat and saved-prompt methods.
