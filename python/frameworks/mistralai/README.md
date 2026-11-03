# Mistral AI tracing

Trace Mistral chat completions, embeddings, and conversations in Future AGI, including model names, messages, token usage, tool calls, and errors.

## Install

Use Python 3.10–3.13 and the current Mistral SDK:

```sh
pip install traceAI-mistralai 'mistralai>=3.6,<4'
```

Set `FI_API_KEY`, `FI_SECRET_KEY`, and `MISTRAL_API_KEY` in your environment. Use your own Mistral API key for model requests.

## Trace a completion

```python
import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from mistralai.client import Mistral
from traceai_mistralai import MistralAIInstrumentor

provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="mistral-app",
)
MistralAIInstrumentor().instrument(tracer_provider=provider)

try:
    with Mistral(api_key=os.environ["MISTRAL_API_KEY"]) as client:
        response = client.chat.complete(
            model="mistral-small-latest",
            messages=[{"role": "user", "content": "Suggest three names for a neighborhood bookstore."}],
        )
        print(response.choices[0].message.content)
finally:
    provider.shutdown()
```

Open **Observe** in Future AGI and select `mistral-app`. The completion appears as an LLM span with the model and returned usage.

The integration routes the SDK's native OpenTelemetry spans through your provider. Do not add another Mistral instrumentor or configure a second Mistral exporter. Clients created before instrumentation are supported too. Configure tracing once at startup; finish active requests and close streams before uninstrumenting.

## Stream a response

Use the stream's context manager so the connection and span close even when you stop reading early:

```python
with client.chat.stream(
    model="mistral-small-latest",
    messages=[{"role": "user", "content": "Describe a cozy bookstore."}],
) as stream:
    for event in stream:
        print(event.data.choices[0].delta.content or "", end="")
```

For async requests, use `await client.chat.complete_async(...)` or `async with await client.chat.stream_async(...)`. Keep the client open until the stream is closed. Token usage is available when the service returns it; an early-closed stream may not receive usage.

## Control captured content

Messages and tool arguments can contain sensitive data. To keep model names, token counts, timing, and error status without recording input or output content, replace the instrumentor call above with:

```python
from fi_instrumentation import TraceConfig

MistralAIInstrumentor().instrument(
    tracer_provider=provider,
    config=TraceConfig(hide_inputs=True, hide_outputs=True),
)
```

This also hides error details by default. Embedding vectors are not captured.

## Supported APIs

- **SDK 3.6–3.x:** native sync/async chat and agent completions, streaming, embeddings, and `beta.conversations.start`, `append`, and `restart`, including their async and streaming variants. Conversation streaming errors and final usage are captured on the native span.
- **SDK 1.1–1.x:** the existing `MistralAIInstrumentor` entry point continues to support chat and agent completions and streams, plus embeddings. Keep `from mistralai import Mistral` for this SDK line. Conversation tracing requires SDK 3.6 or later.
- SDK 1.0, 2.x, and 3.0–3.5 are outside the supported range. Pin one of the ranges above rather than overriding pip's dependency check.

## Troubleshooting

- **`cannot import name Mistral`**: use `from mistralai.client import Mistral` with SDK 3.6 or later. The top-level import belongs to SDK 1.x.
- **Missing streaming spans**: fully consume or explicitly close the stream before flushing or shutting down your tracer provider.
- **Duplicate spans**: remove other Mistral SDK instrumentors. This integration adapts native telemetry rather than adding a second completion span.
