# Qwen (OpenAI-compatible) with traceAI

Trace Qwen Chat Completions through the official `openai` Python SDK using Alibaba Cloud Model Studio's [OpenAI-compatible API](https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope). This recipe uses the existing `traceai-openai` instrumentor; it needs no Qwen-specific tracing package or DashScope SDK.

## Install

From `python/examples/qwen/`, install the dependency pins:

```bash
pip install -r requirements.txt
```

The plain install form is:

```bash
pip install traceAI-openai fi-instrumentation-otel openai
```

## Configure

| Variable | Required | Purpose |
| --- | --- | --- |
| `FI_API_KEY` | For Future AGI export | Future AGI API key; tracer only. |
| `FI_SECRET_KEY` | For Future AGI export | Future AGI secret key; tracer only. |
| `DASHSCOPE_API_KEY` | Yes | Model Studio API key for the selected region and workspace; OpenAI client only. |
| `DASHSCOPE_MODEL` | Yes, unless `--model` is supplied | A model ID available in your region and workspace. The example uses `qwen-plus`. |
| `DASHSCOPE_BASE_URL` | No | Your console workspace's OpenAI-compatible SDK base URL. Defaults to `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`. |
| `FI_BASE_URL` | No | Future AGI collector origin; defaults to `https://api.futureagi.com`. The tracer adds `/tracer/v1/traces`. |
| `FI_PROJECT_NAME` | No | Observe project name. Defaults to `qwen-openai-recipe`. Goes to the tracer only. |

The default is the legacy international (Singapore) host. Alibaba recommends a workspace-dedicated domain for production; the legacy hosts are still accepted ([Base URL overview](https://www.alibabacloud.com/help/en/model-studio/base-url)). Copy the API Host from your console workspace, add `/compatible-mode/v1`, and set `DASHSCOPE_BASE_URL`. The API key, workspace, region, and billing plan must match the endpoint. Obtain a key using [Alibaba's API-key instructions](https://www.alibabacloud.com/help/en/model-studio/get-api-key).

The application exits 2 for a missing or empty model/key or an invalid base URL, before tracing or network calls. It does not validate Future AGI credentials itself.

## Run

Run from `python/examples/qwen/`. Replace the key placeholders with your own keys. `qwen-plus` is the model used in Alibaba's [streaming example](https://www.alibabacloud.com/help/en/model-studio/stream); select a model available to your region and account from the [model catalog](https://www.alibabacloud.com/help/en/model-studio/models).

```bash
export FI_API_KEY="placeholder-futureagi-key"
export FI_SECRET_KEY="placeholder-futureagi-secret"
export DASHSCOPE_API_KEY="placeholder-qwen-key"
export DASHSCOPE_MODEL="qwen-plus"
# Singapore shared endpoint; use your console workspace's SDK base when available.
export DASHSCOPE_BASE_URL="https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
python src/app.py
python src/app.py --stream
```

Use `--prompt "What is a rainbow?"` to change the prompt. `--model` overrides `DASHSCOPE_MODEL`. Set `FI_PROJECT_NAME` to send traces to another Observe project; unset or empty, they go to `qwen-openai-recipe`. Streaming requests set `stream_options={"include_usage": True}` and consume the final usage chunk, which can have an empty `choices` list.

## Code

Instrument before creating the client. The runnable [src/app.py](src/app.py) also validates the base URL, model, and key before tracing, and handles streaming. This core flow honors the same project and endpoint overrides:

```python
import os
from openai import OpenAI
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_openai import OpenAIInstrumentor

provider = register(
    project_name=os.environ.get("FI_PROJECT_NAME") or "qwen-openai-recipe",
    project_type=ProjectType.OBSERVE,
    set_global_tracer_provider=False,
    verbose=False,
)
OpenAIInstrumentor().instrument(tracer_provider=provider)
base_url = os.environ.get("DASHSCOPE_BASE_URL") or "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
try:
    with OpenAI(base_url=base_url, api_key=os.environ["DASHSCOPE_API_KEY"]) as client:
        response = client.chat.completions.create(
            model=os.environ["DASHSCOPE_MODEL"],
            messages=[{"role": "user", "content": "What is a rainbow?"}],
        )
        print(response.choices[0].message.content or "")
finally:
    provider.force_flush()
```

## What you see in Future AGI

The shared instrumentor emits a `ChatCompletion` span with `gen_ai.span.kind=LLM` per completed chat call. Non-streamed `gen_ai.request.model` is the model id the provider returns in its response; `gen_ai.request.parameters` contains the requested ID unless invocation parameters are hidden.

Up to `traceai-openai` 0.1.10, the provider field says `openai`, including for Qwen. Later versions ([#249](https://github.com/future-agi/traceAI/pull/249)) label the DashScope hosts and `*.maas.aliyuncs.com` workspace hosts as `dashscope`; custom proxy hosts keep `openai`.

- Streamed output accumulates in `output.value`. Up to 0.1.10, streamed and failed calls have no `gen_ai.request.model` attribute; later versions record the requested model there. The requested model remains in `gen_ai.request.parameters` unless `FI_HIDE_LLM_INVOCATION_PARAMETERS=true`.
- When the provider supplies usage, the span records `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, and `gen_ai.usage.total_tokens`. Missing usage is omitted, never set to zero. Without `include_usage`, the OpenAI-compatible stream does not return usage by default; [Alibaba documents](https://www.alibabacloud.com/help/en/model-studio/stream) requesting it in the last chunk.
- Failed calls produce ERROR spans with exception events. The recipe keeps the SDK's default retry policy; the error fixtures disable retries so each case sees one request.

## Provider specifics

### Regions and endpoints

The [Base URL overview](https://www.alibabacloud.com/help/en/model-studio/base-url) and [regional endpoint table](https://www.alibabacloud.com/help/en/model-studio/regions) list the following pay-as-you-go SDK bases. Replace `{WorkspaceId}` with your actual workspace ID from the console; do not pass the placeholder to the SDK.

| Region | Shared DashScope base | Workspace-dedicated base |
| --- | --- | --- |
| China (Beijing) | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` |
| Singapore | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | `https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1` |
| US (Virginia) | `https://dashscope-us.aliyuncs.com/compatible-mode/v1` | `https://{WorkspaceId}.us-east-1.maas.aliyuncs.com/compatible-mode/v1` |
| China (Hong Kong) | `https://cn-hongkong.dashscope.aliyuncs.com/compatible-mode/v1` | `https://{WorkspaceId}.cn-hongkong.maas.aliyuncs.com/compatible-mode/v1` |
| Japan (Tokyo) | Use your workspace endpoint | `https://{WorkspaceId}.ap-northeast-1.maas.aliyuncs.com/compatible-mode/v1` |
| Germany (Frankfurt) | Use your workspace endpoint | `https://{WorkspaceId}.eu-central-1.maas.aliyuncs.com/compatible-mode/v1` |

The SDK appends `/chat/completions` and sends `Authorization: Bearer <DASHSCOPE_API_KEY>`. Do not append that endpoint path yourself. Workspace endpoints require a key belonging to that workspace; shared endpoints accept keys from workspaces in the same region. Keys and model availability are region-specific. Use the exact Model Studio model ID, such as `qwen-plus`, rather than a Hugging Face repository name.

`check_base_url` rejects literal or repeatedly URL-encoded `{WorkspaceId}` placeholders. On `aliyuncs.com` hosts, it requires HTTPS and exactly `/compatible-mode/v1` or `/compatible-mode/v1/`: at most one trailing slash, with no path prefix, endpoint suffix, query, or fragment. All URLs must be absolute HTTP(S) URLs without URL credentials, whitespace, control characters, malformed ports, or invalid host spellings. Unicode dot spellings cannot bypass validation. Uppercase hosts and a single trailing DNS dot are checked the same way. Customer proxies and loopback servers may use their own paths; interior underscores in proxy host labels are allowed. Valid URLs are returned unchanged; the SDK adds a trailing slash to `client.base_url`.

### Errors

The [Chat API reference](https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope) and [error-code guide](https://www.alibabacloud.com/help/en/model-studio/error-code) describe provider errors:

- HTTP 400: invalid request; check parameters and model capabilities.
- HTTP 401 `invalid_api_key`: check the key and its region, workspace, and billing-plan match. A cross-region key can produce `Incorrect API key provided`. This comes from Model Studio, not a Future AGI authentication failure; `FI_API_KEY` and `FI_SECRET_KEY` are not involved in that provider request.
- HTTP 404: check the model ID, its availability/permissions, and the endpoint.
- HTTP 429: rate limit or quota/billing limit; inspect the provider message before retrying.
- HTTP 500: provider service error; retry according to the provider's guidance.

## Privacy

Set `FI_HIDE_INPUTS=true` to mask prompt attributes or `FI_HIDE_OUTPUTS=true` to mask response attributes in exported spans. You can also pass `config=TraceConfig(hide_inputs=True)` to `OpenAIInstrumentor().instrument(...)`, importing `TraceConfig` from `fi_instrumentation`.

The provider still receives the prompt. Future AGI masking does not change the provider's own logging. The Model Studio key belongs only to the OpenAI client; Future AGI keys belong only to the tracer.

## Limits / not covered

- Qwen-Audio uses the DashScope native protocol; use the DashScope protocol for that model family rather than this Chat Completions recipe.
- The DashScope native SDK/protocol, Responses API, realtime audio, tools, and structured outputs are outside this recipe.
- Choose model availability and workspace access in your Model Studio console. This recipe does not provision workspaces, select billing plans, or modify the pricing table.

## Tests

Run from the repository root. Provider traffic uses `httpx.MockTransport` or a loopback server, and OTLP exports go to a loopback receiver. App subprocesses install a socket/DNS guard before imports. Negative controls target the real provider hostname and require the guard to reject it before network access.

Repository source:

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python/examples/qwen/src:python:python/frameworks/openai:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \
  pytest python/examples/qwen/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

For the SDK floor, replace `openai==3.24.0` with `openai==1.69.0`.

Published packages (keep only the recipe and harness on `PYTHONPATH` so repository source does not shadow the installed packages):

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python/examples/qwen/src:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'openai==3.24.0' --with 'traceAI-openai==0.1.10' --with 'fi-instrumentation-otel==1.1.0' \
  --with httpx --with protobuf --with opentelemetry-proto --with pytest \
  pytest python/examples/qwen/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

[Recipe CI](../../../.github/workflows/openai-compatible-recipe-tests.yml) defines these three Python 3.11 cells:

| `openai` | traceAI packages |
| --- | --- |
| 3.24.0 | Repository source |
| 1.69.0 | Repository source (SDK floor) |
| 3.24.0 | Published `traceAI-openai==0.1.10` + `fi-instrumentation-otel==1.1.0` |

The fixtures cover chat, streaming with and without final usage, response-versus-request model IDs, provider errors, privacy controls, project-name precedence, strict URL validation, and CLI subprocesses. Synthetic response IDs and usage counts are fixture data, not provider output. Tests accept both the published instrumentor's provider/model attributes and the source behavior described above.

## Official API references

- [OpenAI-compatible Chat API](https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope)
- [Base URLs by region and plan](https://www.alibabacloud.com/help/en/model-studio/base-url)
- [Regions and endpoints](https://www.alibabacloud.com/help/en/model-studio/regions)
- [Obtain an API key](https://www.alibabacloud.com/help/en/model-studio/get-api-key)
- [Model catalog](https://www.alibabacloud.com/help/en/model-studio/models)
- [Streaming and token usage](https://www.alibabacloud.com/help/en/model-studio/stream)
- [Error codes](https://www.alibabacloud.com/help/en/model-studio/error-code)
