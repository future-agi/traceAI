# TraceAI Cerebras instrumentation

Trace calls from the native [Cerebras Cloud SDK](https://github.com/Cerebras/cerebras-cloud-sdk-python) in Future AGI, including chat, text completions, tool calls, token usage, and Cerebras timing attributes.

## Install

Use Python 3.10–3.13:

```bash
pip install traceai-cerebras
```

The package installs `cerebras-cloud-sdk`. Chat tracing supports SDK 1.0.0 and later. Text tracing is enabled when your SDK exposes `client.completions.create`.

## Configure tracing

Set your Cerebras and Future AGI credentials in the environment. Choose a model available to your Cerebras account.

```bash
export CEREBRAS_API_KEY="YOUR_CEREBRAS_API_KEY"
export CEREBRAS_MODEL="YOUR_MODEL_ID"
export FI_API_KEY="YOUR_FUTURE_AGI_API_KEY"
export FI_SECRET_KEY="YOUR_FUTURE_AGI_SECRET_KEY"
```

Register the instrumentor before making requests. This example keeps request and response content out of exported traces; remove the two `hide_*` settings only if you want that content recorded.

```python
import os

from cerebras.cloud.sdk import Cerebras
from fi_instrumentation import TraceConfig, register
from fi_instrumentation.fi_types import ProjectType
from traceai_cerebras import CerebrasInstrumentor

provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="cerebras-app",
)
CerebrasInstrumentor().instrument(
    tracer_provider=provider,
    config=TraceConfig(hide_inputs=True, hide_outputs=True),
)

try:
    with Cerebras() as client:
        response = client.chat.completions.create(
            model=os.environ["CEREBRAS_MODEL"],
            messages=[{"role": "user", "content": "Suggest a name for a hiking app."}],
        )
        print(response.choices[0].message.content)
finally:
    provider.force_flush()
```

Open the `cerebras-app` project in Future AGI to inspect the request's LLM span. Instrumentation records one span per completion, not one span per streamed chunk. It does not emit OpenTelemetry metrics or logs.

## Stream a response

Use the SDK stream as a context manager so it closes and the span finishes even if you stop reading early:

```python
with Cerebras() as client:
    with client.chat.completions.create(
        model=os.environ["CEREBRAS_MODEL"],
        messages=[{"role": "user", "content": "Write a short hiking checklist."}],
        stream=True,
    ) as stream:
        for chunk in stream:
            for choice in chunk.choices or []:
                delta = getattr(choice, "delta", None)
                if delta and delta.content:
                    print(delta.content, end="")
```

`AsyncCerebras` uses the same instrumentor. Await `create`, use `async for` to read chunks, and `async with` or `await stream.close()` to close the stream. Explicit close works before the first chunk is read. Fully consuming a stream also finishes its span.

## Trace text completions

If your SDK exposes the text-completion resource and your model supports it, use it normally:

```python
with Cerebras() as client:
    response = client.completions.create(
        model=os.environ["CEREBRAS_MODEL"],
        prompt="Three essentials for a day hike:",
        max_tokens=100,
    )
    print(response.choices[0].text)
```

Sync, async, and streaming text calls use the same configuration. A text request creates a `Cerebras Text Completions` span; a chat request creates a `Cerebras Chat Completions` span.

## Recorded attributes

| Attribute | Value |
|---|---|
| `gen_ai.provider.name` | `cerebras` |
| `gen_ai.operation.name` | `chat` or `text_completion` |
| `gen_ai.request.model` | The model you requested |
| `gen_ai.response.model` | The model returned by Cerebras |
| `gen_ai.input.messages.{i}.message.*` | Chat message role, content, and tool-call metadata |
| `gen_ai.output.messages.{i}.message.*` | Response content, reasoning content parts, and tool calls |
| `gen_ai.tool.definitions.{i}.tool.json_schema` | Request tool definitions |
| `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` / `gen_ai.usage.total_tokens` | Token counts returned by the SDK |
| `gen_ai.usage.input_tokens.cache_read` | Cached input tokens, when returned |
| `gen_ai.usage.output_tokens.reasoning` | Reasoning tokens, when returned |
| `cerebras.queue_time` / `cerebras.prompt_time` / `cerebras.completion_time` / `cerebras.total_time` | Cerebras processing times in seconds, when returned |

Input/output privacy settings apply to message content, raw request/response JSON, tool arguments, and reasoning. Request transport headers are never recorded. Model attribution, token counts, and timing remain available when content is hidden.

Streaming tool-call fragments are assembled per choice and tool index. The last reported non-null token and timing fields are retained even if the stream ends with an empty chunk. For an early close, only the chunks already consumed are recorded; usage and timing are absent if Cerebras has not sent them yet.

## Choose the matching client integration

Keep `CerebrasInstrumentor` for native `Cerebras` and `AsyncCerebras` clients. If your application already uses the OpenAI client pointed at `https://api.cerebras.ai/v1`, use [traceAI-openai](../openai) instead. The OpenAI instrumentor does not instrument native Cerebras clients, and the native adapter retains Cerebras-specific timing fields. Use one instrumentor for the client making the request; changing instrumentors alone does not migrate your clients.

## Troubleshooting

- **No span appears:** instrument before making the request and flush the tracer provider before a short-lived process exits.
- **A stream's span is missing:** exhaust the stream or close it with a context manager or its `close` method.
- **Text completions are unavailable:** use an SDK version that exposes `client.completions` and a model supporting that endpoint.
- **Content is hidden:** this setup uses `TraceConfig(hide_inputs=True, hide_outputs=True)`. Keep those settings for metadata-only tracing.

## License

Apache-2.0
