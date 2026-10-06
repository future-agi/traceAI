# OrcaRouter (OpenAI-compatible) with traceAI

This recipe traces Chat Completions through the official `openai` Python SDK at `https://api.orcarouter.ai/v1`. It uses `traceai-openai`; no OrcaRouter tracing package is needed.

## Install

From this directory:

```bash
pip install -r requirements.txt
```

Or install the packages directly:

```bash
pip install traceAI-openai fi-instrumentation-otel openai
```

The requirements pin `openai==3.24.0`, `traceAI-openai==0.1.10`, and `fi-instrumentation-otel==1.1.0`. The tested versions are listed under [Tests](#tests).

## Configure

| Variable | Purpose | Sent to |
|---|---|---|
| `FI_API_KEY` | Future AGI API key | Tracer only |
| `FI_SECRET_KEY` | Future AGI secret key | Tracer only |
| `ORCAROUTER_API_KEY` | OrcaRouter API key; required | OpenAI client only |
| `ORCAROUTER_MODEL` | Requested model, for example `orcarouter/auto`; required unless `--model` is given | OpenAI client |
| `ORCAROUTER_BASE_URL` | Optional override; defaults to `https://api.orcarouter.ai/v1` | OpenAI client |
| `FI_BASE_URL` | Optional Future AGI collector origin | Tracer only |

The app checks `ORCAROUTER_API_KEY` and `ORCAROUTER_MODEL` (unless `--model` supplies the model). Missing or empty values exit 2 before tracing. A refused `ORCAROUTER_BASE_URL` also exits 2. The recipe does not check the Future AGI keys.

On `api.orcarouter.ai`, the URL must use HTTPS and exactly `/v1` or `/v1/`. The root also serves other protocol surfaces. Endpoint URLs such as `/v1/chat/completions`, queries, and fragments are refused. Host comparisons handle case and trailing dots. Whitespace, control characters, non-ASCII or malformed IDNA hosts, and URL credentials are refused before tracing. Valid customer proxy and loopback HTTP or HTTPS URLs are accepted for testing or custom deployments. Validation returns the supplied URL unchanged. The OpenAI SDK then adds a trailing slash: the default client base URL is `https://api.orcarouter.ai/v1/`, and the request goes to `https://api.orcarouter.ai/v1/chat/completions`.

The hosted URL and OpenAI compatibility are documented at [OrcaRouter introduction](https://docs.orcarouter.ai/introduction).

## Run

From this directory, replace these placeholders with your own keys:

```bash
export FI_API_KEY="placeholder-futureagi-api-key"
export FI_SECRET_KEY="placeholder-futureagi-secret-key"
export ORCAROUTER_API_KEY="placeholder-orcarouter-key"
export ORCAROUTER_MODEL="orcarouter/auto"
python src/app.py
python src/app.py --stream
```

Use `--model` to override `ORCAROUTER_MODEL`, or `--prompt` to supply a question. These commands make real provider calls when configured with real keys. The tests below use placeholders and local fixtures.

## Code

This is the essential flow from `src/app.py`. Instrument before creating the client. Put `src/` on `PYTHONPATH` to import `make_client` from `app`.

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_openai import OpenAIInstrumentor
from app import make_client

provider = register(
    project_name="orcarouter-example", project_type=ProjectType.OBSERVE,
    set_global_tracer_provider=False, verbose=False,
)
OpenAIInstrumentor().instrument(tracer_provider=provider)
with make_client() as client:  # OpenAI(base_url=..., api_key=...) inside the helper
    response = client.chat.completions.create(
        model="orcarouter/auto", messages=[{"role": "user", "content": "What is a coral reef?"}],
    )
provider.force_flush()
```

## What you see in Future AGI

The fixtures produce one `ChatCompletion` span with `gen_ai.span.kind=LLM` per call. The provider field says `openai`: `gen_ai.provider.name` is the shared instrumentor's label.

For a successful non-streamed call, `gen_ai.request.model` is the model id the provider returns in its response. The requested model stays in the JSON attribute `gen_ai.request.parameters`. For streamed and failed calls, current `traceai-openai` leaves `gen_ai.request.model` absent; the requested model is still in `gen_ai.request.parameters`. Hiding invocation parameters with `FI_HIDE_LLM_INVOCATION_PARAMETERS=true` removes that JSON attribute, so streamed and failed spans record no model at all (pinned by a test).

The tests pin `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, and `gen_ai.usage.total_tokens` when the response includes usage. Missing usage is omitted, never replaced with zero. The default stream fixture exports none of these attributes. A stream with `stream_options={"include_usage": True}` and a final usage chunk exports all three. The same instrumentor accumulates streamed output text in `output.value`. These are fixture observations, not a guarantee that the hosted gateway supplies stream usage.

The 401 and 429 fixtures raise the SDK's `AuthenticationError` and `RateLimitError`. Each exports one ERROR span with an exception event. Error tests disable SDK retries so a fixture produces one request. Gateway errors such as a wrong or non-OrcaRouter key (401), an unfunded wallet or rate limits come from OrcaRouter: the SDK raises them and the span records them as ERROR. They are not Future AGI ingest failures.

## Provider specifics

OrcaRouter documents its OpenAI surface at `/v1`, `orcarouter/auto`, and fallback chains through `extra_body.models` at [OrcaRouter introduction](https://docs.orcarouter.ai/introduction). This recipe covers that OpenAI surface.

The gateway can resolve `orcarouter/auto` to an upstream model; see [OrcaRouter tracing and model resolution](https://www.arize.com/docs/ax/integrations/llm-providers/orcarouter/orcarouter-tracing). The same page says `orcarouter/auto` routes to paid upstream models and requires a funded OrcaRouter wallet; without one the gateway returns an error such as `403 insufficient_user_quota`. A fallback chain may also be served by a model other than the first one requested; OrcaRouter's `X-Orca-*` response headers report which model served a request ([Observability](https://docs.orcarouter.ai/operations/observability)). Our synthetic fixture requests `orcarouter/auto` and returns `deepseek/deepseek-chat-2026-09-01`. The latter is a test id, not a model availability claim. The non-streamed span records the returned id in `gen_ai.request.model`, while `gen_ai.request.parameters` retains `orcarouter/auto`. A cost lookup keyed on one name may miss the other; these tests do not validate prices or costs.

For `extra_body={"models": ["a/one", "b/two"]}`, the fixture receives the `models` list unchanged in the request body. Both names are synthetic fixture entries. In current `traceai-openai`, `models` and `extra_body` are absent from `gen_ai.request.parameters`: the instrumentor reads the SDK's `json_data` before the extra body is merged. The requested `model` remains in parameters.

## Privacy

`FI_HIDE_INPUTS=true` (or an instrumentor `TraceConfig(hide_inputs=True)`) masks input attributes only. A prompt marker disappears from the exported attributes and events in the control test. The provider still receives the prompt. Future AGI masking does not change the provider's own logging.

For a non-streamed Chat Completions call, `output.value` is the assistant text. Unrecognised top-level response fields are not exported: the test adds one, verifies that the SDK receives it and that the exported span omits it while keeping the assistant text. Model, usage and output-message attributes are still exported as described above. Streams export accumulated text with no raw-response fallback. Set `FI_HIDE_OUTPUTS=true` to mask output attributes; controls cover both non-streamed responses and streamed text.

## Limits / not covered

- Native Anthropic and Gemini surfaces on OrcaRouter, video endpoints, and named-router configuration. The [vendor introduction](https://docs.orcarouter.ai/introduction) describes the separate surfaces and routing features.
- OrcaRouter-Lite self-hosting, a separate project: [OrcaRouter-Lite](https://github.com/Continuum-AI-Corp/OrcaRouter-Lite).
- Tool calling, vision, reasoning, embeddings, and Responses API behavior. Vendor compatibility claims at the [introduction](https://docs.orcarouter.ai/introduction) do not establish coverage by this test suite.
- Hosted model availability, routing decisions, billing, and costs.
- Native export. OrcaRouter's [Observability page](https://docs.orcarouter.ai/operations/observability) documents a Prometheus/OpenMetrics workspace-metrics scrape, an `X-Orca-Request-Id` response header and console request logs, but no trace export (checked 2026-10-06). This recipe traces on the client with `traceai-openai`: calls made without the instrumented SDK are not traced, and OrcaRouter metrics and console logs are not imported.
- Not tested against the live provider (no paid call); the tests use a local fake of the OpenAI API.

## Tests

Run from the repository root against repository source:

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python/examples/orcarouter/src:python:python/frameworks/openai:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \
  pytest python/examples/orcarouter/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

Against published packages:

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="python/examples/orcarouter/src:python/tests" \
  uv run --no-project --python 3.11 \
  --with 'openai==3.24.0' --with httpx --with 'wrapt<2' \
  --with 'traceAI-openai==0.1.10' --with 'fi-instrumentation-otel==1.1.0' \
  --with opentelemetry-api --with opentelemetry-sdk --with opentelemetry-instrumentation \
  --with opentelemetry-exporter-otlp-proto-http --with requests --with protobuf \
  --with opentelemetry-proto --with pydantic --with jsonschema --with pytest \
  pytest python/examples/orcarouter/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

For the SDK floor, replace the `openai==3.24.0` pin with `openai==1.69.0`. For the other Python versions, change `--python`. The suite uses MockTransport at the documented host and local servers bound to `127.0.0.1`. Child processes use a socket guard that refuses external DNS and connections before they happen. A readiness sentinel and a negative control verify that the guard is installed and works.

| Python | `openai` | traceAI packages | Result |
|---|---|---|---|
| 3.10, 3.11, 3.12, 3.13 | 3.24.0 | `traceai-openai` and `fi_instrumentation` from this repository | Full suite |
| 3.11 | 1.69.0 (the `traceai-openai` floor) | from this repository | Full suite |
| 3.11 | 3.24.0 | published `traceAI-openai==0.1.10` and `fi-instrumentation-otel==1.1.0` from PyPI | Full suite |
