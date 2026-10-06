# Perplexity Agent API (OpenAI SDK, Responses) with traceAI

This recipe traces Perplexity's Agent API through the `openai` Python SDK's Responses interface at `https://api.perplexity.ai/v1`. It uses the existing `traceai-openai` instrumentor without a provider-specific package.

## Install

Install the pinned dependencies:

```bash
pip install -r requirements.txt
```

Or install the packages directly:

```bash
pip install traceAI-openai fi-instrumentation-otel openai
```

## Configure

| Environment variable | Required | Purpose |
|---|---|---|
| `FI_API_KEY` | Yes | Future AGI API key. Goes to the tracer only. |
| `FI_SECRET_KEY` | Yes | Future AGI secret key. Goes to the tracer only. |
| `PERPLEXITY_API_KEY` | Yes | Perplexity key. Goes to the OpenAI client only. |
| `PERPLEXITY_MODEL` | Yes, unless using `--model` | A model id from Perplexity's Agent API models page. The compatibility example uses `openai/gpt-5.6-sol`. |
| `PERPLEXITY_BASE_URL` | No | SDK base URL override. Defaults to `https://api.perplexity.ai/v1`. |
| `FI_BASE_URL` | No | Future AGI collector origin. Defaults to `https://api.futureagi.com`. |
| `FI_HIDE_INPUTS` | No | Set to `true` to hide prompt text in exported spans. |
| `FI_HIDE_OUTPUTS` | No | Set to `true` to hide answer text and raw response output in exported spans. |

The SDK represents the documented base URL as `https://api.perplexity.ai/v1/`, with a trailing slash. Requests go to `https://api.perplexity.ai/v1/responses`. The app validates the URL without rewriting it and checks the model and Perplexity key before setting up tracing.

## Run

From this example directory, replace the placeholders with your keys:

```bash
export FI_API_KEY="placeholder-fi-key"
export FI_SECRET_KEY="placeholder-fi-secret"
export PERPLEXITY_API_KEY="placeholder-perplexity-key"
export PERPLEXITY_MODEL="openai/gpt-5.6-sol"
python src/app.py
python src/app.py --stream
```

Use `--prompt "What is a solar eclipse?"` to change the question. `--model` takes precedence over `PERPLEXITY_MODEL`.

## Code

Instrument before creating the client:

```python
import os
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from openai import OpenAI
from traceai_openai import OpenAIInstrumentor

provider = register(project_name="perplexity-agent-api",
                    project_type=ProjectType.OBSERVE, set_global_tracer_provider=False)
OpenAIInstrumentor().instrument(tracer_provider=provider)
client = OpenAI(api_key=os.environ["PERPLEXITY_API_KEY"],
                base_url="https://api.perplexity.ai/v1")
response = client.responses.create(model=os.environ["PERPLEXITY_MODEL"],
                                   input="What is a solar eclipse?")
print(response.output_text)
provider.force_flush()
```

The full runnable version is [src/app.py](src/app.py). Streaming prints `response.output_text.delta` events and consumes the stream through completion so the instrumentor can finish the span.

## What you see in Future AGI

The fixtures produce one span named `Response` per call, with `gen_ai.span.kind=LLM`. On the Responses API, `gen_ai.request.model` records the model you requested, including on streamed and failed calls. The request model also appears in `gen_ai.request.parameters`.

The provider field is `openai` (`gen_ai.provider.name`). This is the shared instrumentor's label for calls made through the OpenAI SDK.

Token counts appear in `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` and `gen_ai.usage.total_tokens` when the response includes usage. Missing usage is omitted, never filled with zero. For streaming, the instrumentor takes the answer and token counts from the full response in `response.completed`. The fixture exports 17 input tokens, 11 output tokens and 28 total tokens on both successful paths. HTTP 401 produces an ERROR span with an exception event.

Search-result URLs and snippets appear inside `output.value` on non-streamed calls; streamed `output.value` contains only the answer text. The non-streamed value is the shared instrumentor's raw response representation, which includes the `search_results` item. Search results can therefore reach Future AGI inside raw output. There are no citation attributes or retriever spans. The tests verify that `FI_HIDE_OUTPUTS=true` removes the answer text, search-result URLs and snippets from the exported span, with an unmasked control. `usage.cost` can remain inside raw non-streamed output; it has no dedicated cost mapping.

With OpenAI 3.24.0, the extra `search_results` item parses and `response.output_text` returns the message text. Serializing that item emits Pydantic warnings; it does not prevent the fixture span from exporting.

## Provider specifics

Perplexity documents the [Agent API through the OpenAI SDK's Responses interface](https://docs.perplexity.ai/docs/agent-api/openai-compatibility). The canonical Agent API endpoint is `/v1/agent`; `/v1/responses` is its OpenAI SDK alias. `client.responses.create` uses that alias automatically. Pass the base URL `https://api.perplexity.ai/v1`, rather than an endpoint path.

Presets are a Perplexity SDK feature. With the OpenAI SDK, pass a model id from the Agent API models page.

Sonar Chat Completions support ended on 27 September 2026. Existing requests are being reformulated as Agent API requests, gradually by model. New code should use the Agent API. See the [Sonar migration overview](https://docs.perplexity.ai/docs/agent-api/migrate-from-sonar/overview).

| Refused SDK base URL | Reason and replacement |
|---|---|
| `https://api.perplexity.ai` or its root `/` | Former Sonar Chat Completions base URL. Use `https://api.perplexity.ai/v1` with `client.responses.create`. |
| `https://api.perplexity.ai/v1/sonar` | Endpoint path. The SDK appends `/responses`; use the documented base URL. |
| `https://api.perplexity.ai/v1/agent` | Canonical endpoint path. Use the documented SDK base URL for the `/v1/responses` alias. |
| `https://api.perplexity.ai/router/v1` or `/router` | The Router API is a separate private-preview product, outside this recipe. |

Host checks also catch upper-case and trailing-dot spellings. The documented base URL with or without a trailing slash is allowed unchanged. Loopback URLs are allowed for tests.

## Privacy

`FI_HIDE_INPUTS=true` keeps prompt text out of the exported span. `FI_HIDE_OUTPUTS=true` keeps answer text and the raw response, including search-result URLs and snippets, out of the exported span. The tests check both against visible controls. You can also pass `TraceConfig(hide_inputs=True)` to the instrumentor.

Perplexity still receives the prompt. Future AGI masking does not change Perplexity's own logging.

## Limits / not covered

- Sonar Chat Completions through `chat.completions.create`.
- The native `perplexity` SDK (`from perplexity import Perplexity`) and its presets.
- The Search API, Router API (private preview) and Embeddings API.
- Async or background runs.
- Citation attributes or retriever spans.
- `usage.cost` mapping.
- Live provider calls. This recipe was not tested against the live provider (no paid call); the tests use a local fake of the OpenAI API and in-process fixtures.

The fixtures include Perplexity's `search_results` item first, followed by the assistant message. They check SDK parsing and `response.output_text`. They do not establish compatibility with other response shapes or models.

## Tests

Run from the repository root. The subprocess tests use a socket guard that refuses external DNS and connections before networking. A ready marker, empty log on successful loopback calls and deliberate refused attempts verify the guard.

Repository source:

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python/examples/perplexity/src:python:python/frameworks/openai:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \
  pytest python/examples/perplexity/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

Published packages:

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python/examples/perplexity/src:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \
  --with 'traceAI-openai==0.1.10' --with 'fi-instrumentation-otel==1.1.0' \
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \
  pytest python/examples/perplexity/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

For the floor row, change `openai==3.24.0` to `openai==1.69.0` in the repository-source command.

| Python | `openai` | traceAI packages | Result |
|---|---|---|---|
| 3.10, 3.11, 3.12, 3.13 | 3.24.0 | `traceai-openai` and `fi_instrumentation` from this repository | Full suite |
| 3.11 | 1.69.0 (the `traceai-openai` floor) | from this repository | Full suite |
| 3.11 | 3.24.0 | published `traceAI-openai==0.1.10` and `fi-instrumentation-otel==1.1.0` from PyPI | Full suite |
