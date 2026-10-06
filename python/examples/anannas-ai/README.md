# Anannas AI (OpenAI-compatible) with traceAI

Trace Chat Completions through the official `openai` Python SDK at `https://api.anannas.ai/v1` ([Anannas quickstart](https://docs.anannas.ai/)). This recipe uses the existing `traceAI-openai` instrumentor; no Anannas package is needed.

## Install

From this recipe directory:

```bash
pip install -r requirements.txt
```

The file pins the tested versions (see [Tests](#tests)). To install without pins:

```bash
pip install traceAI-openai fi-instrumentation-otel openai
```

## Configure

| Variable | Required | Purpose |
|---|---|---|
| `FI_API_KEY` | For Future AGI export | Future AGI API key; tracer only. |
| `FI_SECRET_KEY` | For Future AGI export | Future AGI secret key; tracer only. |
| `ANANNAS_API_KEY` | Yes | Anannas key; OpenAI client only, sent as a bearer token. |
| `ANANNAS_MODEL` | Yes, unless `--model` is given | Model ID, for example `openai/gpt-5-mini`. |
| `ANANNAS_BASE_URL` | No | Overrides `https://api.anannas.ai/v1`; customer proxies and loopback servers are accepted. |
| `FI_BASE_URL` | No | Future AGI collector origin. `register()` uses its default when unset. |
| `FI_PROJECT_NAME` | No | Observe project name. `register()` defaults to `DEFAULT_PROJECT_NAME`. |
| `FI_HIDE_INPUTS` | No | Set to `true` to mask input attributes. |
| `FI_HIDE_OUTPUTS` | No | Set to `true` to mask output attributes. |

Anannas key, bearer authentication, and model examples come from the [Anannas quickstart](https://docs.anannas.ai/). Future AGI keys go only to the tracer as `X-Api-Key` and `X-Secret-Key`. The vendor key goes only to the OpenAI client.

Missing or empty `ANANNAS_MODEL` (without `--model`) or `ANANNAS_API_KEY` exits 2 before tracing. Invalid `ANANNAS_BASE_URL` also exits 2 before tracing. The recipe does not check the Future AGI keys.

## Run

```bash
export FI_API_KEY="placeholder-fi-api-key"
export FI_SECRET_KEY="placeholder-fi-secret-key"
export FI_PROJECT_NAME="anannas-ai-example"
export ANANNAS_API_KEY="placeholder-anannas-key"
export ANANNAS_MODEL="openai/gpt-5-mini"
python src/app.py
python src/app.py --stream
```

Replace the placeholder keys with your own. Use `--prompt "What is a rainbow?"` to change the prompt or `--model openai/gpt-5-mini` to override the environment model.

## Code

The essential sequence from `src/app.py` is registration, instrumentation, client construction, and the call:

```python
import os
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_openai import OpenAIInstrumentor
from openai import OpenAI
provider = register(
    project_name="anannas-ai-example", project_type=ProjectType.OBSERVE,
    set_global_tracer_provider=False, verbose=False,
)
OpenAIInstrumentor().instrument(tracer_provider=provider)
client = OpenAI(base_url="https://api.anannas.ai/v1", api_key=os.environ["ANANNAS_API_KEY"])
response = client.chat.completions.create(
    model=os.environ["ANANNAS_MODEL"], messages=[{"role": "user", "content": "What is a rainbow?"}])
print(response.choices[0].message.content)
provider.force_flush()
```

The app validates configuration, supports streaming, closes the client, and flushes tracing in `finally`. The SDK represents this base URL as `https://api.anannas.ai/v1/` and joins requests to exactly `https://api.anannas.ai/v1/chat/completions`. The validator returns accepted URLs unchanged.

## What you see in Future AGI

The fixtures export one `ChatCompletion` span per completed test call, with `gen_ai.span.kind=LLM` in the registered Observe project. The provider field says `openai`; `gen_ai.provider.name` is the shared instrumentor's label.

Current `traceAI-openai` behavior is:

- For a successful non-streamed call, `gen_ai.request.model` is the model ID the provider returns. The test requests `openai/gpt-5-mini` and returns the synthetic fixture ID `openai/gpt-5-mini-2026-09-01`; the dated ID lands in `gen_ai.request.model`.
- With invocation parameters enabled, `gen_ai.request.parameters` keeps the requested ID, including its provider prefix. Streaming and failed calls currently omit `gen_ai.request.model`, while retaining that requested ID in parameters.
- `output.value` is assistant content. Streaming accumulates the text through the same instrumentor. A stream with no text records `''`, with no raw-response fallback.
- When usage is supplied, tests pin `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, and `gen_ai.usage.total_tokens`. Missing usage omits all three; it is unknown, never zero. The default stream fixture has no usage. A final usage chunk with `stream_options={"include_usage": True}` supplies the same three attributes in the fixture test.
- A 401 raises `AuthenticationError`; a 402 raises `APIStatusError`. Both export an ERROR span with an exception event.

Cost lookup keyed on the requested model name may miss a different returned name. This recipe does not map names or add cost attributes. The dated model and streaming usage option are fixture coverage, not claims about Anannas resolving that model or supporting that option.

## Provider specifics

The [Anannas quickstart](https://docs.anannas.ai/) and the [Langfuse integration](https://langfuse.com/integrations/gateways/anannas) use `https://api.anannas.ai/v1`, the default here. Some older pages show `https://anannas.ai/v1`; the recipe accepts that host with the same URL rules.

Model IDs use the form `provider/model-name`, such as `openai/gpt-5-mini` or `anthropic/claude-3-sonnet`. Anannas documents `POST /v1/chat/completions`, `GET /v1/models`, bearer authentication, and SSE streaming with `stream: true` ([Anannas quickstart](https://docs.anannas.ai/)). This recipe uses only Chat Completions and preserves model IDs.

Both Anannas hosts require HTTPS and exactly `/v1` or `/v1/`. Root URLs, endpoint paths such as `/v1/chat/completions` and `/v1/models`, other paths, queries, and fragments are refused. Whitespace, control characters, Unicode or invalid IDNA hosts, and URL credentials are refused before tracing. Valid proxy and loopback URLs keep their supplied spelling and paths.

Anannas documents 400, 401, 402, 429, and 500 errors with `{"error": {"message": "...", "type": "invalid_request_error"}}`; 402 means insufficient credits ([Anannas quickstart](https://docs.anannas.ai/)). Tests cover 401 and 402. Native OTLP or trace export is not documented in the cited [Anannas quickstart](https://docs.anannas.ai/).

## Privacy

`FI_HIDE_INPUTS=true` masks input attributes only. `FI_HIDE_OUTPUTS=true` masks output attributes. Set both to keep prompt and assistant text out of the exported span. Tests compare each flag and both flags against an unmasked control, for chat and streaming.

The provider still receives the prompt. Hiding inputs does not hide a response that echoes the prompt; output masking is separate. Future AGI masking does not change the provider's own logging. Chat Completions exports assistant content in `output.value`, including accumulated stream text, rather than the raw response. Extra raw-response fixture data is absent; a no-text stream exports an empty string.

## Limits / not covered

- The Anannas dashboard and its analytics.
- Provider routing settings beyond `model`.
- Model catalog synchronization, native export, and historical traces.
- Other SDK surfaces, tool calls, cancellation, and interrupted streams.
- Vendor support for the fixture's `stream_options` usage option.
- This recipe is not tested against the live provider (no paid call); the tests use a local fake of the OpenAI API and `httpx.MockTransport`.

## Tests

Run from the repository root. Both commands use the same fixtures. Subprocesses install a guard that refuses non-loopback DNS and socket connections; positive loopback and negative vendor-host controls check it. The pytest process also refuses non-loopback traffic.

Repository source:

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python/examples/anannas-ai/src:python:python/frameworks/openai:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \
  pytest python/examples/anannas-ai/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

Published packages:

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python/examples/anannas-ai/src:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'traceAI-openai==0.1.10' --with 'fi-instrumentation-otel==1.1.0' \
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \
  pytest python/examples/anannas-ai/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

For the `openai==1.69.0` floor, replace the SDK pin in the repository-source command.

| Python | `openai` | traceAI packages | Result |
|---|---|---|---|
| 3.10, 3.11, 3.12, 3.13 | 3.24.0 | `traceai-openai` and `fi_instrumentation` from this repository | Full suite |
| 3.11 | 1.69.0 (the `traceai-openai` floor) | from this repository | Full suite |
| 3.11 | 3.24.0 | published `traceAI-openai==0.1.10` and `fi-instrumentation-otel==1.1.0` from PyPI | Full suite |
