# Trace DeepSeek with the OpenAI integration

Use `traceAI-openai` to trace DeepSeek Chat Completions and Responses in Future AGI. The shared integration preserves OpenAI's synchronous and asynchronous streaming interfaces and records DeepSeek reasoning and cache fields when the API returns them.

## Install

Use Python 3.10–3.13 and OpenAI SDK 1.69 or newer.

```bash
pip install --upgrade traceAI-openai fi-instrumentation-otel openai
```

## Configure and run

Set `DEEPSEEK_API_KEY`, `FI_API_KEY`, and `FI_SECRET_KEY` in your environment. Set `DEEPSEEK_MODEL` to a model available to your DeepSeek account. Obtain a DeepSeek key from the [DeepSeek platform](https://platform.deepseek.com/).

```python
import os

from fi_instrumentation import TraceConfig, register
from fi_instrumentation.fi_types import ProjectType
from openai import OpenAI
from traceai_openai import OpenAIInstrumentor

provider = register(project_type=ProjectType.OBSERVE, project_name="deepseek-app")
OpenAIInstrumentor().instrument(
    tracer_provider=provider,
    config=TraceConfig(hide_inputs=True, hide_outputs=True),
)

try:
    with OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url="https://api.deepseek.com",
    ) as client:
        response = client.chat.completions.create(
            model=os.environ["DEEPSEEK_MODEL"],
            messages=[{"role": "user", "content": "What is 15% of 240?"}],
        )
        print(response.choices[0].message.content)
finally:
    provider.force_flush()
    provider.shutdown()
```

Open **Observe** and select `deepseek-app`. The model span has provider `deepseek`, the model name, and token counts when returned by the API. Prompt and output text are hidden with the configuration above; set the corresponding options to `False` only when you intend to capture that content. Reasoning and tool-argument aliases follow the output privacy policy. To also hide invocation parameters and exception details, use `hide_llm_invocation_parameters=True` and `hide_error_details=True`. Tool definitions remain visible; do not put secrets in tool names, descriptions, or schemas.

The instrumentor wraps OpenAI SDK requests process-wide, including clients configured for other providers. Provider attribution uses the API host, not the requested model name; custom proxies retain the `openai` fallback.

## Streaming and Responses

Use the same instrumented client for Chat Completions streaming. Keep the stream in a context manager so early exits also close the HTTP response and finish its span.

```python
with client.chat.completions.create(
    model=os.environ["DEEPSEEK_MODEL"],
    messages=[{"role": "user", "content": "Explain prompt caching in one sentence."}],
    stream=True,
    stream_options={"include_usage": True},
) as stream:
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            print(chunk.choices[0].delta.content, end="")
```

Run this block inside the client's `with` block above. For async code, use `AsyncOpenAI`, await `create()`, and use `async with` / `async for`.

DeepSeek's [Responses API](https://api-docs.deepseek.com/guides/responses_api) uses the same base URL and shared instrumentor:

```python
response = client.responses.create(
    model=os.environ["DEEPSEEK_MODEL"],
    input="Explain prompt caching in one sentence.",
)
print(response.output_text)
```

## Migrate from `DeepSeekInstrumentor`

`DeepSeekInstrumentor` remains available for existing Chat Completions applications, including OpenAI SDK 1.0–1.68. It is deprecated, not removed. It does not instrument the Responses API; upgrade the SDK and switch to `OpenAIInstrumentor` for that API.

Replace the `traceai_deepseek` import and `DeepSeekInstrumentor().instrument(...)` call with the shared setup above. Upgrade both tracing packages before enabling both entry points during migration. With the updated packages, the shared instrumentor owns the request and its tracer provider and privacy configuration apply. An older shared package without DeepSeek extraction keeps the legacy behavior (including duplicate spans) rather than silently losing reasoning and cache fields. Removing either instrumentor leaves the other operational.

The shared route preserves the Chat Completions `deepseek.reasoning_content`, `deepseek.output_reasoning`, `deepseek.prompt_cache_hit_tokens`, `deepseek.prompt_cache_miss_tokens`, `deepseek.reasoning_tokens`, response ID, finish reason, and tool-call aliases when present. Token/cache counters are span attributes, not a separate metrics stream. Span names become `ChatCompletion` / `Response`; `output.value` contains the shared integration's text/tool summary rather than the legacy raw response JSON. Request tools use the shared semantic attributes rather than `deepseek.tools_count`.

Do not uninstall the legacy package until every application has switched its imports and no longer needs the older SDK floor.

## Troubleshooting

- **No spans:** instrument before creating the client, check your Future AGI keys, and flush the provider before a short-lived process exits.
- **Provider is `openai`:** use the exact `api.deepseek.com` API host. A custom proxy is not automatically attributed to DeepSeek.
- **Missing streaming token counts:** request `stream_options={"include_usage": True}` and consume the stream through its final usage chunk.
- **401 or model error:** check your DeepSeek key and the model's availability. See the [DeepSeek API documentation](https://api-docs.deepseek.com/).

## License

Apache-2.0
