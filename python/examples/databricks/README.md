# Databricks (OpenAI-compatible) with traceAI

Trace Chat Completions through the official `openai` Python SDK at your Databricks workspace's documented OpenAI-compatible base URL. This recipe uses `traceai-openai`; it needs no Databricks tracing package.

## Install

From this directory, install the pinned dependencies:

```bash
pip install -r requirements.txt
```

Or install the packages directly:

```bash
pip install traceAI-openai fi-instrumentation-otel openai
```

The requirements pin `openai==3.24.0`, `traceAI-openai==0.1.10`, and `fi-instrumentation-otel==1.1.0`. The test suite also passes on the `traceai-openai` SDK floor, `openai==1.69.0` (see Tested versions).

## Configure

| Environment variable | Required | Purpose |
|---|---|---|
| `FI_API_KEY` | Yes | Future AGI API key; tracer only. |
| `FI_SECRET_KEY` | Yes | Future AGI secret key; tracer only. |
| `DATABRICKS_TOKEN` | Yes | Databricks token from your secret store; OpenAI client only. Never commit it. |
| `DATABRICKS_MODEL` | Yes, unless using `--model` | Your model service name or serving endpoint name. |
| `DATABRICKS_BASE_URL` | Yes | Your workspace-specific SDK base URL. There is no public default. |
| `FI_BASE_URL` | No | Future AGI collector origin; uses the tracer's default when unset. |

Choose one of these two different surfaces. Replace `<workspace-host>` with the host from your workspace URL:

| Surface | Documented base URL form | `model` example |
|---|---|---|
| Unity Gateway model services | `https://<workspace-host>/ai-gateway/mlflow/v1` | `system.ai.claude-sonnet-4-5` |
| Model Serving serving endpoints | `https://<workspace-host>/serving-endpoints` | `openai-chat-endpoint` |

Use your own model service or endpoint name. Both names above are examples. `DATABRICKS_BASE_URL` is required and replaces the base URL for either surface.

## Run

Replace all key and workspace placeholders before running. This example chooses model services; use the second URL form and your endpoint name for Model Serving.

```bash
export FI_API_KEY="placeholder-fi-api-key"
export FI_SECRET_KEY="placeholder-fi-secret-key"
export DATABRICKS_TOKEN="placeholder-databricks-key"
export DATABRICKS_BASE_URL="https://<workspace-host>/ai-gateway/mlflow/v1"
export DATABRICKS_MODEL="system.ai.claude-sonnet-4-5"
python src/app.py
python src/app.py --stream
```

You can override the model with `--model` and the question with `--prompt`. Missing configuration or a refused URL exits with code 2 before tracing starts. The URL check returns allowed URLs unchanged. The OpenAI SDK then adds a trailing slash to its stored `base_url` and appends `/chat/completions` for this call.

## Code

Instrument before creating the client. Validate the workspace URL with the helper in [src/app.py](src/app.py).

```python
import os
from openai import OpenAI
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_openai import OpenAIInstrumentor
from app import check_base_url
provider = register(project_name="databricks-openai-recipe", project_type=ProjectType.OBSERVE,
                    set_global_tracer_provider=False, verbose=False)
OpenAIInstrumentor().instrument(tracer_provider=provider)
client = OpenAI(api_key=os.environ["DATABRICKS_TOKEN"],
                base_url=check_base_url(os.environ["DATABRICKS_BASE_URL"]))
answer = client.chat.completions.create(model=os.environ["DATABRICKS_MODEL"],
    messages=[{"role": "user", "content": "What is a lakehouse?"}])
print(answer.choices[0].message.content)
provider.force_flush()
```

## What you see in Future AGI

A Chat Completions call produces one `ChatCompletion` span with `gen_ai.span.kind=LLM`. On a successful non-streamed response, `gen_ai.request.model` is the model id the provider returns in its response. The fixtures return the model service or endpoint name sent in the request.

The provider field says `openai` (`gen_ai.provider.name`). This is the shared instrumentor's label for this recipe.

Token usage appears in `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, and `gen_ai.usage.total_tokens` when the response has usage. Missing usage is omitted, never filled with zero.

Streaming accumulates the answer in `output.value` through the same instrumentor. A default stream without usage exports no usage attributes. With `stream_options={"include_usage": True}` and a final usage chunk, all three usage attributes are exported. Your endpoint must support that option to use it.

Current `traceai-openai` behavior omits `gen_ai.request.model` on streamed and failed calls. The requested model remains in the `gen_ai.request.parameters` JSON unless `FI_HIDE_LLM_INVOCATION_PARAMETERS=true`. Errors have span status ERROR and an exception event.

## Provider specifics

### Model services

For Unity Gateway model services, use `/ai-gateway/mlflow/v1` and send your model service name as `model`. The Databricks documentation's example.staging.cloud.databricks.com is a placeholder, not a real host. Replace it with your workspace host; the app refuses the sample host.

### Serving endpoints

For Model Serving serving endpoints, use `/serving-endpoints` and send your endpoint name as `model`. These two suffixes select different surfaces. Do not combine them. The token comes from `DATABRICKS_TOKEN`; keep it in a secret store.

`model` is the model service or endpoint name, not a frozen foundation-model id. A pricing row keyed by a foundation-model id will not match an endpoint name. An endpoint name needs a matching pricing row for cost lookup.

The app refuses workspace roots, `/api/2.0/serving-endpoints`, and `/serving-endpoints/<name>/invocations`. The last form is a REST invocation URL, not an SDK base URL. It also refuses `/ai-gateway/gemini` and `/ai-gateway/anthropic`, which are gateway native APIs for other SDKs. Databricks workspace hosts must use `https://`; plain `http://` would send the token in cleartext before any redirect. A query string or fragment on a workspace URL is refused. Placeholder checks include percent-encoded forms. Host checks ignore case and trailing dots. Customer proxies and loopback URLs are allowed unchanged.

### Embeddings, only when the endpoint task is embeddings

Use `client.embeddings.create(model="system.ai.gte-large-en", input="A short example.")` only when your selected endpoint task is embeddings. Use your own embedding service name. A chat endpoint can reject this call; that is an endpoint task mismatch.

The gateway fixture produces one `CreateEmbeddingResponse` span with `gen_ai.span.kind=EMBEDDING` and provider `openai`. It records the response model in `embedding.model_name`, not `gen_ai.request.model`. It exports input and total token usage, with no output token usage. Input text is exported in `input.value` and `embedding.embeddings.0.embedding.text` by default. The recipe's CLI runs Chat Completions.

### Inside the workspace

`databricks_openai.DatabricksOpenAI` 0.17.1 subclasses `openai.OpenAI` without overriding its request method, so `traceai-openai` wraps it the same way; this recipe does not test it. This is a source-checked observation. The helper is not a recipe dependency.

## Privacy

For Chat Completions, set `FI_HIDE_INPUTS=true` or `FI_HIDE_OUTPUTS=true` before tracing starts to keep prompt or response text out of the exported span. `TraceConfig(hide_inputs=True)` passed to the instrumentor is another way to hide chat inputs. The provider still receives the prompt. Future AGI masking does not change the provider's own logging or Databricks payload logs.

The embedding fixture pins exported input text separately. These chat masking tests do not establish masking of embedding-specific text attributes.

## Limits / not covered

- SQL `ai_query` and AI Functions.
- The MLflow Deployments SDK and Databricks-internal MLflow trace forwarding.
- Agent / Genie code and other agent frameworks.
- The Gemini/Anthropic gateway native APIs.
- `DatabricksOpenAI` execution; the helper is source-checked only.
- Direct REST invocation calls, workspace provisioning, and live workspace calls.
- Not tested against the live provider (no paid call); the tests use a local fake of the OpenAI API.

The fixtures use these syntactic test hosts, not real ones, with `httpx.MockTransport`:

```text
https://dbc-00000000-0000.cloud.databricks.com/ai-gateway/mlflow/v1
https://dbc-00000000-0000.cloud.databricks.com/serving-endpoints
```

## Tests

Run from the repository root. The suite checks both surfaces, credentials, spans, usage, streaming, errors, chat masking, embeddings, URL refusal, and CLI subprocesses. Vendor requests use fixtures or a server on `127.0.0.1`. Subprocess guards refuse other hosts before DNS and record refused attempts. A negative control proves the guard is active.

Repository source:

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python/examples/databricks/src:python:python/frameworks/openai:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \
  pytest python/examples/databricks/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

Published packages:

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python/examples/databricks/src:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \
  --with 'traceAI-openai==0.1.10' --with 'fi-instrumentation-otel==1.1.0' \
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \
  pytest python/examples/databricks/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

Tested versions (for the floor row, change `openai==3.24.0` to `openai==1.69.0` in the repository-source command):

| Python | `openai` | traceAI packages | Result |
|---|---|---|---|
| 3.10, 3.11, 3.12, 3.13 | 3.24.0 | `traceai-openai` and `fi_instrumentation` from this repository | Full suite |
| 3.11 | 1.69.0 (the `traceai-openai` floor) | from this repository | Full suite |
| 3.11 | 3.24.0 | published `traceAI-openai==0.1.10` and `fi-instrumentation-otel==1.1.0` from PyPI | Full suite |
