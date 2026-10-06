# Azure AI Foundry (OpenAI-compatible) with traceAI

This recipe traces Chat Completions through the official `openai` Python SDK at your Azure resource's `/openai/v1/` endpoint; model is your deployment name, not a model id.
It uses `traceai-openai`, with no separate provider package.

## Install

Requires Python 3.10 or later.

From this example directory, install the pinned dependencies:

```bash
pip install -r requirements.txt
```

The plain package form is:

```bash
pip install traceAI-openai fi-instrumentation-otel openai
```

## Configure

| Variable | Required | Purpose |
|---|---|---|
| `FI_API_KEY` | Yes | Future AGI API key; tracer only. |
| `FI_SECRET_KEY` | Yes | Future AGI secret key; tracer only. |
| `AZURE_OPENAI_API_KEY` | Yes | Azure resource API key; OpenAI client only. |
| `AZURE_OPENAI_DEPLOYMENT` | Yes, unless `--model` is used | Your deployment name, not an underlying model id. |
| `AZURE_OPENAI_BASE_URL` | Yes | Your resource-specific inference base URL. There is no public default. |
| `FI_BASE_URL` | No | Future AGI collector origin; defaults to `https://api.futureagi.com`. |
| `FI_HIDE_INPUTS` | No | Set to `true` to hide prompt text in exported spans. |
| `FI_HIDE_OUTPUTS` | No | Set to `true` to hide response text in exported spans. |

The client receives the Azure key. The tracer receives the Future AGI keys.
The example uses the Future AGI project name `azure-ai-foundry`.

## Run

Replace the placeholders before running. A template URL that still contains
`YOUR-RESOURCE-NAME` or angle brackets fails before tracing or a network call.

```bash
export FI_API_KEY="placeholder-futureagi-key"
export FI_SECRET_KEY="placeholder-futureagi-secret"
export AZURE_OPENAI_API_KEY="placeholder-azure-key"
export AZURE_OPENAI_BASE_URL="https://YOUR-RESOURCE-NAME.openai.azure.com/openai/v1/"
export AZURE_OPENAI_DEPLOYMENT="your-deployment-name"
python src/app.py
python src/app.py --stream
```

Use `--prompt "What is a rainbow?"` to change the question or
`--model your-deployment-name` to override the deployment environment variable.
Missing Azure configuration and refused URLs return exit code 2 before tracing.

## Code

Instrument before creating the client. [src/app.py](src/app.py) contains the CLI
and the URL validation used here.

```python
import os
from openai import OpenAI
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_openai import OpenAIInstrumentor
from app import check_base_url
base_url = check_base_url(os.environ["AZURE_OPENAI_BASE_URL"])
provider = register(project_name="azure-ai-foundry", project_type=ProjectType.OBSERVE,
                    set_global_tracer_provider=False, verbose=False)
OpenAIInstrumentor().instrument(tracer_provider=provider)
client = OpenAI(api_key=os.environ["AZURE_OPENAI_API_KEY"], base_url=base_url)
response = client.chat.completions.create(model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
                                         messages=[{"role": "user", "content": "What is a rainbow?"}])
print(response.choices[0].message.content)
provider.force_flush()
```

## What you see in Future AGI

The fixture tests produce one LLM span per call: `ChatCompletion` for Chat
Completions and `Response` for the Responses API. The provider field is `openai`
(`gen_ai.provider.name`). This is the shared instrumentor's label for both Azure
host forms. The span does not say `azure`.

For a successful non-streamed response, `gen_ai.request.model` is the model id
the provider returns in its response. The fixtures return the deployment name
that was requested. Azure's `model` request parameter is your deployment name.
Azure may return the underlying model name rather than your deployment name;
`gen_ai.request.model` then shows the returned name, and your deployment name is
in `gen_ai.request.parameters`. Cost lookup keyed on one name may miss the other.

Token usage is recorded only when the response has `usage`. Missing usage
attributes are omitted, never replaced with 0. The Chat Completions stream is
accumulated by the same instrumentor. Its default test stream has no usage
attributes. With `stream_options={"include_usage": True}` and a final usage
chunk, the instrumentor records input, output and total token counts.

In the current `traceai-openai` version, streamed and failed Chat Completions calls omit `gen_ai.request.model`.
On the Responses API, a failed call records the requested deployment, and a successful Responses call records the model the response returns.
The requested deployment remains in the
`gen_ai.request.parameters` JSON unless `FI_HIDE_LLM_INVOCATION_PARAMETERS=true`.
Errors set the span status to ERROR and record an exception event.

The non-streamed Responses fixture records the full parsed response as Python
dictionary text in `output.value`, including its output text. It also records
the model the response returns (the fixture returns a different model from the
deployment requested) and usage, including the supplied reasoning and cache
token counts. Responses streaming is not tested here.

## Provider specifics

Microsoft documents both of these resource-specific base URL forms:

```text
https://YOUR-RESOURCE-NAME.openai.azure.com/openai/v1/
https://YOUR-RESOURCE-NAME.services.ai.azure.com/openai/v1/
```

Both end in `/openai/v1/`. The SDK joins `chat/completions` or `responses` to
that path. If the slash is omitted after `/openai/v1`, the SDK appends it.
The example's validator returns allowed URLs exactly as supplied; it does not
rewrite them. Azure hosts must use `https://` and have the path `/openai/v1` or
`/openai/v1/`, with no query string (the v1 API needs no `api-version`), no
fragment and no percent-encoded path. Credentials in the URL are refused.
Proxy hosts and loopback URLs are also allowed.

`model` means your model deployment name, for example `my-gpt-deployment`,
not a model id. Read the API key from `AZURE_OPENAI_API_KEY`.
Microsoft recommends the Responses API for Azure OpenAI models. Chat
Completions remains available for models that support it on the same base URL:

```python
response = client.responses.create(
    model=os.environ["AZURE_OPENAI_DEPLOYMENT"], input="What is a rainbow?"
)
print(response.output_text)
provider.force_flush()
```

For Microsoft Entra ID, use Microsoft's token-provider sample with
`azure.identity` and the same `base_url` on the
[v1 API page](https://learn.microsoft.com/en-us/azure/foundry/openai/api-version-lifecycle).
This recipe does not implement token acquisition. Tracing uses the same
instrumentation because it is the same OpenAI client.

The tests use these syntactic test hosts, not real Azure resources:

```text
https://test-resource-0000.openai.azure.com/openai/v1/
https://test-resource-0000.services.ai.azure.com/openai/v1/
```

MockTransport handles those URLs in-process without DNS or an Azure connection.

### Not this page: Foundry Agent Service

Foundry agents are not this page. Agent Service, agent threads and evaluations
are separate surfaces. A project endpoint such as
`https://YOUR-RESOURCE-NAME.services.ai.azure.com/api/projects/YOUR-PROJECT-NAME`
is not a model inference base URL.

`AzureAIOpenTelemetryTracer` exports to Application Insights. That setup does
not send agent spans to Future AGI through this recipe. This example does not
retarget that tracer or implement agent tracing.

## Privacy

Set `FI_HIDE_INPUTS=true` and/or `FI_HIDE_OUTPUTS=true` to keep prompt or response
text out of exported spans. The tests include visible control runs and verify
both flags. You can also pass `TraceConfig(hide_inputs=True)` as the instrumentor's
`config`. `FI_HIDE_LLM_INVOCATION_PARAMETERS=true` omits request parameters.

On non-streamed Responses calls, `output.value` holds the whole response object,
which can echo request fields such as `instructions`. `FI_HIDE_INPUTS` does not cover
that field; set `FI_HIDE_OUTPUTS=true` as well if those must stay out of the export.

Azure still receives the prompt. Future AGI masking does not change Azure's
own logging or data handling.

## Limits / not covered

- The `AzureOpenAI` client with `api-version`, including older deployment-scoped URLs.
- The `azure-ai-inference` SDK and its `/models` endpoint.
- Foundry agents, threads, evaluations and Application Insights export.
- A bare Azure resource root or the project endpoint as an inference base URL.
- Responses streaming, async calls and other SDK surfaces in this example's tests.
- Live Azure calls: not tested against the live provider (no paid call); the tests use a local fake of the OpenAI API.

The recipe does not change the shared instrumentor's `openai` provider label.

## Tests

Run from the repository root. The suite uses MockTransport and local servers
on `127.0.0.1`. Child processes load a loopback guard that rejects other hosts
before DNS. Its negative control proves the guard is active.

Repository source:

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python/examples/azure-ai-foundry/src:python:python/frameworks/openai:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \
  pytest python/examples/azure-ai-foundry/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

Published packages:

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python/examples/azure-ai-foundry/src:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'openai==3.24.0' --with 'traceAI-openai==0.1.10' --with 'fi-instrumentation-otel==1.1.0' \
  --with httpx --with 'wrapt<2' --with pytest \
  pytest python/examples/azure-ai-foundry/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

For the floor row, change `openai==3.24.0` to `openai==1.69.0` in the repository-source command.

| Python | `openai` | traceAI packages | Result |
|---|---|---|---|
| 3.10, 3.11, 3.12, 3.13 | 3.24.0 | `traceai-openai` and `fi_instrumentation` from this repository | Full suite |
| 3.11 | 1.69.0 (the `traceai-openai` floor) | from this repository | Full suite |
| 3.11 | 3.24.0 | published `traceAI-openai==0.1.10` and `fi-instrumentation-otel==1.1.0` from PyPI | Full suite |
