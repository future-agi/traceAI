# NVIDIA DGX Cloud Lepton (OpenAI-compatible) with traceAI

This recipe traces Chat Completions through the official `openai` Python SDK at an OpenAI-compatible NVIDIA DGX Cloud Lepton LLM endpoint. It uses `traceai-openai`; there is no separate Lepton tracing package.

## Install

From this directory, install the version pins:

```bash
pip install -r requirements.txt
```

The plain installation form is:

```bash
pip install traceAI-openai fi-instrumentation-otel openai
```

## Configure

There is no shared public base URL. The endpoint URL from the API tab, or `lep endpoint get -n <name>`, looks like `https://<workspace>-<endpoint>.xenon.lepton.run`. Set `LEPTON_ENDPOINT_URL` to that URL plus `/v1`, as NVIDIA documents for LLM endpoints. The documented base-URL form is `<ENDPOINT_URL from the API tab>/v1`. Replace the placeholder before running the app.

| Environment variable | Required | Purpose |
|---|---|---|
| `FI_API_KEY` | Yes | Future AGI API key; goes to the tracer only. |
| `FI_SECRET_KEY` | Yes | Future AGI secret key; goes to the tracer only. |
| `LEPTON_API_TOKEN` | Yes | Non-empty token shown in the endpoint's API tab; goes to the OpenAI client only. |
| `LEPTON_ENDPOINT_URL` | Yes | Endpoint URL from the endpoint details page, API tab, plus `/v1`. No default. |
| `LEPTON_MODEL` | Yes, unless `--model` is supplied | Model served by your endpoint. |
| `FI_BASE_URL` | No | Override the Future AGI collector origin; used by the tracer only. |

Missing values and refused URLs exit with code 2 before tracing starts. This recipe requires a non-empty `LEPTON_API_TOKEN`. Create the endpoint with an access token, as NVIDIA recommends over making it public. Use the token the API tab shows.

## Run

From this directory:

```bash
export FI_API_KEY='placeholder-futureagi-key'
export FI_SECRET_KEY='placeholder-futureagi-secret'
export LEPTON_API_TOKEN='placeholder-lepton-token'
export LEPTON_ENDPOINT_URL='<ENDPOINT_URL from the API tab>/v1'
export LEPTON_MODEL='nvidia/Nemotron-Research-Reasoning-Qwen-1.5B'
# Replace the URL and key placeholders with your own values first.
python src/app.py
python src/app.py --stream
```

You can also supply `--model` and `--prompt`. The example model is the model in NVIDIA's endpoint tutorial. Set the name to the model your endpoint actually serves.

## Code

The essential sequence from `src/app.py` is below. To use `from app import check_base_url`, put `src/` on `PYTHONPATH` or run the snippet from `src/`.

```python
import os
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_openai import OpenAIInstrumentor
from openai import OpenAI
from app import check_base_url
base_url = check_base_url(os.environ["LEPTON_ENDPOINT_URL"])
api_key = os.environ["LEPTON_API_TOKEN"]
model = os.environ["LEPTON_MODEL"]
provider = register(project_name="lepton-openai-recipe", project_type=ProjectType.OBSERVE, set_global_tracer_provider=False, verbose=False)
OpenAIInstrumentor().instrument(tracer_provider=provider)
client = OpenAI(base_url=base_url, api_key=api_key)
response = client.chat.completions.create(model=model, messages=[{"role": "user", "content": "What is a comet?"}])
print(response.choices[0].message.content)
provider.force_flush()
```

The app validates the URL, model and non-empty token before registering tracing. It instruments before creating the client and flushes after the call. It reports request failures without printing the endpoint URL.

## What you see in Future AGI

- One `ChatCompletion` span with `gen_ai.span.kind=LLM` per tested call.
- For a non-streamed response, `gen_ai.request.model` is the model id the provider returns in its response.
- Current `traceai-openai` leaves `gen_ai.request.model` absent on streamed and failed calls. The requested model remains in the `gen_ai.request.parameters` JSON unless `FI_HIDE_LLM_INVOCATION_PARAMETERS=true`.
- Token usage is present only when the response includes usage. Missing usage is omitted, never filled with zero.
- Streaming text is accumulated by the same instrumentor. The default fixture exports no usage attributes. With `stream_options={"include_usage": True}` and a final usage chunk, the fixture exports input, output and total token counts. Your endpoint must support this option to use it.
- Request errors are recorded as span errors with an exception event.
- The provider field says `openai`. This is the shared instrumentor's label, including for a Lepton endpoint.

The span resource uses the project name passed to `register()`.

## Provider specifics

Lepton here means [NVIDIA DGX Cloud Lepton endpoints](https://docs.nvidia.com/dgx-cloud/lepton/get-started/endpoint/). An endpoint is a running model that exposes an HTTP server.

Copy the endpoint URL and API key from the endpoint details page, in its [API tab](https://docs.nvidia.com/dgx-cloud/lepton/features/endpoints/create-llm/). You can also run `lep endpoint get -n <name>` and read `external_endpoint`. The endpoint URL looks like `https://<workspace>-<endpoint>.xenon.lepton.run`. Append `/v1` yourself when setting `LEPTON_ENDPOINT_URL`: NVIDIA's [LLM endpoint example](https://docs.nvidia.com/dgx-cloud/lepton/examples/endpoint/deploy-gpt-oss/) uses `<ENDPOINT_URL>/v1/chat/completions`. If the endpoint is not OpenAI-compatible, stop; this recipe does not apply.

`check_base_url()` returns an allowed URL exactly as given; it never appends `/v1`. For hosts under `*.xenon.lepton.run`, it requires HTTPS, rejects a host-only URL or `/` path, and refuses query strings and fragments. For every host, it refuses non-ASCII or malformed IDNA hostnames and credentials in the URL.

The tested OpenAI SDK appends a trailing slash to a non-empty base path and lower-cases the scheme and host (the path keeps its case). For the syntactic test host `https://ws0000-example.xenon.lepton.run/v1`, the client base URL is `https://ws0000-example.xenon.lepton.run/v1/` and the request URL is `https://ws0000-example.xenon.lepton.run/v1/chat/completions`. The suite also keeps `https://endpoint.example.invalid/v1` as a syntactic test host on the reserved `.invalid` domain. Neither test host is contacted; both use in-process fixtures.

Endpoints can be public and reachable by anyone with the URL. For a public endpoint, treat the URL like a secret and never commit it. The app does not print endpoint URLs in its configuration or request error messages. Legacy Lepton AI hosts and console hosts are refused even with upper-case or trailing-dot spellings. Literal and repeatedly percent-encoded placeholders are refused.

## Privacy

Set `FI_HIDE_INPUTS=true` to keep prompt text out of exported span attributes. Set `FI_HIDE_OUTPUTS=true` to keep response text out. You can also pass `TraceConfig(hide_inputs=True)` to the instrumentor. The endpoint still receives the prompt. Future AGI masking does not change the provider's own logging.

In the tested chat, streaming and HTTP 401 fixtures, the endpoint URL and host are absent from exported span attributes, events, status and resource values. This holds with the `FI_HIDE_*` flags enabled or disabled. The instrumentor does not export `server.address`, `url.full` or a base-URL attribute in these cases. This observation covers this recipe's instrumentor; extra HTTP instrumentation or logging has its own behavior.

The vendor token is also absent from the exported spans, resources and collector headers. Future AGI keys are used only for the collector.

## Limits / not covered

- Named legacy Lepton AI hosts: `api.lepton.ai`, `llm.lepton.run` and `sdxl.lepton.run`.
- Console URLs: `dashboard.dgxc-lepton.nvidia.com` and `dashboard.lepton.ai`.
- The `leptonai` photon client.
- NVIDIA's Python SDK. It is a workspace control-plane client for batch jobs, endpoints and secrets, not an inference client.
- Non-OpenAI-compatible endpoints, endpoint deployment, and APIs other than Chat Completions.
- Live endpoint calls. This recipe is not tested against the live provider (no paid call); the tests use a local fake of the OpenAI API and in-process fixtures.

## Tests

Run from the repository root against repository source:

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python/examples/lepton/src:python:python/frameworks/openai:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \
  pytest python/examples/lepton/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

Run from the repository root against published packages:

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python/examples/lepton/src:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'openai==3.24.0' --with 'traceAI-openai==0.1.10' \
  --with 'fi-instrumentation-otel==1.1.0' --with httpx --with 'wrapt<2' \
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \
  pytest python/examples/lepton/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

For the floor row, change `openai==3.24.0` to `openai==1.69.0` in the repository-source command. Tests use `httpx.MockTransport` at the syntactic test host and loopback subprocesses protected by a socket guard. No provider host is called.

| Python | `openai` | traceAI packages | Result |
|---|---|---|---|
| 3.10, 3.11, 3.12, 3.13 | 3.24.0 | `traceai-openai` and `fi_instrumentation` from this repository | Full suite |
| 3.11 | 1.69.0 (the `traceai-openai` floor) | from this repository | Full suite |
| 3.11 | 3.24.0 | published `traceAI-openai==0.1.10` and `fi-instrumentation-otel==1.1.0` from PyPI | Full suite |
