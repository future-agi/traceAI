# Doubleword (OpenAI-compatible) with traceAI

Trace Chat Completions through the official `openai` Python SDK at Doubleword's documented base URL, `https://api.doubleword.ai/v1` ([API reference](https://doubleword.ai/llms.txt)). This recipe uses `traceai-openai`; it needs no Doubleword-specific tracing package.

## Install

From this directory, install the dependency pins:

```bash
pip install -r requirements.txt
```

The plain install form is:

```bash
pip install traceAI-openai fi-instrumentation-otel openai
```

## Configure

| Variable | Required | Purpose |
|---|---|---|
| `FI_API_KEY` | For Future AGI export | Future AGI API key; tracer only. |
| `FI_SECRET_KEY` | For Future AGI export | Future AGI secret key; tracer only. |
| `DOUBLEWORD_API_KEY` | Yes | Doubleword key; OpenAI client only. Create it on the [API Keys page](https://app.doubleword.ai/api-keys). |
| `DOUBLEWORD_MODEL` | Yes, unless `--model` is supplied | Pick an available model from Doubleword's catalog using its [API reference](https://doubleword.ai/llms.txt). |
| `DOUBLEWORD_BASE_URL` | No | Defaults to `https://api.doubleword.ai/v1`; optional proxy override. |
| `FI_BASE_URL` | No | Optional Future AGI collector origin; tracer only. |
| `FI_PROJECT_NAME` | No | Observe project name; defaults to `doubleword-example`; tracer only. |

The application exits 2 for a missing or empty `DOUBLEWORD_API_KEY`, a missing or empty `DOUBLEWORD_MODEL` without `--model`, or an invalid `DOUBLEWORD_BASE_URL`. It checks these before tracing or network calls. The recipe does not check the Future AGI keys itself.

On `api.doubleword.ai`, the URL must use HTTPS and the path must be exactly `/v1` or `/v1/`. Endpoint paths, queries, fragments, URL credentials, whitespace, control characters, and invalid host spellings fail early. Uppercase hosts and a trailing DNS dot do not bypass these checks. Customer proxies and loopback servers can use their own HTTP(S) paths; proxy host names may contain underscores inside a label (for example a Compose service name such as `myproj_proxy_1`). Validation returns allowed URLs unchanged; the OpenAI SDK adds a trailing slash to `client.base_url`.

## Run

Run from this directory. Replace the key placeholders with your own keys. The example uses `deepseek-ai/DeepSeek-V4-Flash`, listed in Doubleword's [API reference](https://doubleword.ai/llms.txt); choose a model available to your account from the [catalog](https://docs.doubleword.ai/inference-api/models) or `client.models.list()`.

```bash
export FI_API_KEY="placeholder-futureagi-api-key"
export FI_SECRET_KEY="placeholder-futureagi-secret-key"
export DOUBLEWORD_API_KEY="placeholder-doubleword-key"
export DOUBLEWORD_MODEL="deepseek-ai/DeepSeek-V4-Flash"
export DOUBLEWORD_BASE_URL="https://api.doubleword.ai/v1"
python src/app.py
python src/app.py --stream
```

You can also pass `--model` and `--prompt`. Set `FI_PROJECT_NAME` to send the traces to another Observe project; unset or empty, the project name defaults to `doubleword-example`.

## Code

The core flow below matches `src/app.py`. The application validates the URL, model, and Doubleword key before this flow. Instrument before creating the client. Future AGI credentials stay in the tracer environment.

```python
import os
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from openai import OpenAI
from traceai_openai import OpenAIInstrumentor

provider = register(project_name=os.environ.get("FI_PROJECT_NAME") or "doubleword-example",  # as in src/app.py
                    project_type=ProjectType.OBSERVE, set_global_tracer_provider=False, verbose=False)
OpenAIInstrumentor().instrument(tracer_provider=provider)
with OpenAI(base_url="https://api.doubleword.ai/v1", api_key=os.environ["DOUBLEWORD_API_KEY"]) as client:
    response = client.chat.completions.create(model=os.environ["DOUBLEWORD_MODEL"],
        messages=[{"role": "user", "content": "What is a rainbow?"}])
    print(response.choices[0].message.content or "")
provider.force_flush()
```

## What you see in Future AGI

The fixtures export one `ChatCompletion` span with `gen_ai.span.kind=LLM` per completed chat call. Up to `traceai-openai` 0.1.10, the provider field says `openai`: `gen_ai.provider.name` is the shared instrumentor's label (later versions, future-agi/traceAI#249, say `doubleword` for `api.doubleword.ai`), including when the client uses Doubleword's host.

Current `traceai-openai` behavior:

- Non-streamed `gen_ai.request.model` is the model id the provider returns in its response. `gen_ai.request.parameters` contains the model id sent in the request. The fixtures deliberately return a different, synthetic dated id so this distinction is tested.
- Up to 0.1.10, streamed and failed calls omit `gen_ai.request.model`; later versions record the requested model there. The requested model remains in the `gen_ai.request.parameters` JSON unless invocation parameters are hidden.
- Chat `output.value` contains the assistant content. Streams accumulate that content into one span. A stream with no output text exports an empty string, with no raw-response fallback. A non-streamed response with empty content exports no `output.value`. Extra response metadata is not copied into chat `output.value`.
- The tests pin `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, and `gen_ai.usage.total_tokens`. Missing usage is omitted, never set to zero. Default stream fixtures omit usage; `stream_options={"include_usage": True}` with a final usage chunk exports all three counts.
- The 401 and 429 fixtures produce ERROR spans with exception events. The recipe keeps the SDK's default retry policy; the error tests disable retries so each fixture sees one request.

Cost is unknown when the pricing table has no matching model. If a provider resolves an alias to a different response id, cost lookup keyed on one name may miss the other. These fixtures demonstrate that distinction; they do not establish Doubleword's model-resolution behavior or rates.

## Provider specifics

This recipe uses synchronous Chat Completions at `https://api.doubleword.ai/v1`.
The OpenAI SDK sends `Authorization: Bearer <key>`. Choose a model ID from the
[model catalog](https://docs.doubleword.ai/inference-api/models) or `client.models.list()`;
pass the exact returned ID, including its organization prefix. For dedicated
realtime deployments, Doubleword supplies your organization-specific model names.

Doubleword's [API reference](https://doubleword.ai/llms.txt) also documents async
inference on `client.chat.completions.create(...)` with
`extra_body={"service_tier": "flex"}`. The SDK versions covered by the fixtures
also accept the native `service_tier="flex"` keyword. Both forms reach the Chat
Completions request body in the fixtures; only the native keyword is captured in
`gen_ai.request.parameters` by the instrumentor. The Chat Completions CLI here
does not select an async tier. Doubleword separately documents
[Responses API async inference](https://docs.doubleword.ai/inference-api/async-inference)
and Autobatcher; those are not the only ways to select flex. Self-serve realtime
uses shared, concurrency-limited capacity; dedicated realtime is a separate
deployment option. A 429 with `inflight_limit_exceeded` indicates the account's
per-model concurrency limit.

The same [API reference](https://doubleword.ai/llms.txt) lists embeddings. An OpenAI-shaped embedding fixture at `/v1/embeddings` produces a `CreateEmbeddingResponse` span with `gen_ai.span.kind=EMBEDDING` and provider `openai` (`doubleword` in later versions). The model id from the response appears in `embedding.model_name`; up to 0.1.10 `gen_ai.request.model` is absent, and later versions set it too. Requested model parameters and input/total token counts are recorded.

## Privacy

Set `FI_HIDE_INPUTS=true` to mask input attributes, or `FI_HIDE_OUTPUTS=true` to mask output attributes. `TraceConfig(hide_inputs=True)` is also available when configuring the instrumentor directly. Tests include visible controls and confirm that the provider still receives the prompt. Future AGI masking does not change the provider's own logging.

For chat, `output.value` is assistant content, including when extra response metadata is supplied. Input masking alone leaves assistant output visible; output masking leaves input visible.

Embedding spans also export raw response JSON under `embedding.embeddings`, vectors included. `FI_HIDE_EMBEDDING_VECTORS=true` removes the indexed `embedding.embeddings.0.embedding.vector` attribute but, up to `traceai-openai` 0.1.10, does **not** mask the vectors in `embedding.embeddings`; later versions strip them there too. The tests check the visible control and accept either version's handling of the raw JSON. Input/output masking is not a blanket scrubber for embedding attributes.

## Limits / not covered

The Batch API is not covered by this recipe ([Doubleword API reference](https://doubleword.ai/llms.txt)).

- The Anthropic-compatible Messages API and Dedicated deployments are outside this recipe ([Doubleword API reference](https://doubleword.ai/llms.txt)).
- Responses API, tools, structured outputs, cancellation, console imports, and pricing-table changes are outside this recipe.

## Tests

Run from the repository root. The suite refuses non-loopback network connections. App subprocesses use a guard that blocks DNS and socket connections before reaching any outside host.

Repository source:

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python/examples/doubleword/src:python:python/frameworks/openai:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \
  pytest python/examples/doubleword/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

For the SDK floor, replace `openai==3.24.0` with `openai==1.69.0` in that command.

Published packages:

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python/examples/doubleword/src:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \
  --with 'traceAI-openai==0.1.10' --with 'fi-instrumentation-otel==1.1.0' \
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \
  pytest python/examples/doubleword/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

| Python | `openai` | traceAI packages | Local check configuration |
|---|---|---|---|
| 3.10, 3.11, 3.12, 3.13 | 3.24.0 | `traceai-openai` and `fi_instrumentation` from this repository | Commands above |
| 3.11 | 1.69.0 (the `traceai-openai` floor) | from this repository | Commands above |
| 3.11 | 3.24.0 | published `traceAI-openai==0.1.10` and `fi-instrumentation-otel==1.1.0` from PyPI | Commands above |

The chat fixtures use `deepseek-ai/DeepSeek-V4-Flash`, a model id listed in Doubleword's [API reference](https://doubleword.ai/llms.txt). Its current catalog availability is not asserted. The dated response model and embedding model are synthetic fixture ids.

## Official API references

- [Realtime inference](https://docs.doubleword.ai/inference-api/realtime-inference)
- [API keys](https://docs.doubleword.ai/inference-api/creating-an-api-key)
- [Model catalog](https://docs.doubleword.ai/inference-api/models)
- [Async Responses API](https://docs.doubleword.ai/inference-api/async-inference)
