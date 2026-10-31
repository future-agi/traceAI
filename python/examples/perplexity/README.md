# Perplexity Agent API (OpenAI SDK, Responses) with traceAI

Trace Perplexity's Agent API in Future AGI through the OpenAI Python SDK. Use the shared `traceAI-openai` package; no separate Perplexity tracing package or gateway is required.

## Install

From this example directory:

```bash
pip install -r requirements.txt
```

The recipe pins `traceAI-openai==0.1.11`, which attributes requests to `api.perplexity.ai` as `perplexity`. For the same setup in your application:

```bash
pip install 'traceAI-openai==0.1.11' 'fi-instrumentation-otel==1.1.0' 'openai==3.24.0'
```

## Configure

Set your Future AGI and Perplexity credentials separately. The tracer uses the Future AGI keys; the OpenAI client uses only your Perplexity key.

```bash
export FI_API_KEY="your-futureagi-api-key"
export FI_SECRET_KEY="your-futureagi-secret-key"
export PERPLEXITY_API_KEY="your-perplexity-api-key"
export FI_PROJECT_NAME="perplexity-agent-api"
export PERPLEXITY_MODEL="your-agent-api-model-id"
```

Choose `PERPLEXITY_MODEL` from Perplexity's [Agent API models](https://docs.perplexity.ai/docs/agent-api/models).

Optional settings:

| Variable | Purpose |
|---|---|
| `PERPLEXITY_BASE_URL` | SDK base URL; defaults to `https://api.perplexity.ai/v1`. |
| `FI_BASE_URL` | Future AGI collector origin; defaults to `https://api.futureagi.com`. |
| `FI_HIDE_INPUTS` | Set to `true` to hide prompt text in exported spans. |
| `FI_HIDE_OUTPUTS` | Set to `true` to hide answer text and raw response output. |
| `FI_HIDE_LLM_INVOCATION_PARAMETERS` | Set to `true` to hide invocation parameters, including custom metadata. |

## Run

```bash
python src/app.py --prompt "What is a solar eclipse?"
python src/app.py --stream
```

Use `--model` to override `PERPLEXITY_MODEL`. Consume streams through completion to capture the returned model, answer and usage.

To instrument your application, initialize tracing before creating the client:

```python
import os
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from openai import OpenAI
from traceai_openai import OpenAIInstrumentor

provider = register(
    project_name=os.environ.get("FI_PROJECT_NAME") or "perplexity-agent-api",
    project_type=ProjectType.OBSERVE,
    set_global_tracer_provider=False,
    verbose=False,
)
OpenAIInstrumentor().instrument(tracer_provider=provider)
try:
    with OpenAI(
        api_key=os.environ["PERPLEXITY_API_KEY"],
        base_url="https://api.perplexity.ai/v1",
    ) as client:
        response = client.responses.create(
            model=os.environ["PERPLEXITY_MODEL"],
            input="What is a solar eclipse?",
        )
        print(response.output_text)
finally:
    provider.force_flush()
    provider.shutdown()
```

See [src/app.py](src/app.py) for the CLI version.

## View your trace

Open the `perplexity-agent-api` project in Future AGI. Each request produces one `Response` LLM span with `gen_ai.provider.name=perplexity`, input/output, and token counts when supplied by Perplexity. Successful requests record the returned model in `gen_ai.request.model`; failed requests retain the requested model.

With the pinned `traceAI-openai` 0.1.11 release, search results and `usage.cost` remain in raw non-streamed output rather than dedicated retrieval or cost attributes. Streamed output contains answer text; if no answer text is present, it falls back to the raw response. Custom `extra_body` parameters are sent to Perplexity but are not captured in invocation parameters by this release.

## Protect trace content

Set these before initializing the instrumentor if prompt and response content must not reach Future AGI:

```bash
export FI_HIDE_INPUTS=true
export FI_HIDE_OUTPUTS=true
export FI_HIDE_LLM_INVOCATION_PARAMETERS=true
```

Raw response output can include search queries derived from your prompt, search-result URLs and snippets, and the response's `instructions`. Hiding inputs alone does not remove those output fields. Perplexity still receives your prompt; these settings control tracing, not Perplexity's own logging.

## Troubleshooting

- **Provider is `openai`:** upgrade `traceAI-openai` to 0.1.11 or later and use the documented Perplexity host. Custom proxy and loopback hosts are not inferred to be Perplexity.
- **Incorrect endpoint:** set the base URL to `https://api.perplexity.ai/v1`, not `/v1/agent` or `/v1/responses`. The SDK appends `/responses`, Perplexity's official alias for `/v1/agent`. Use HTTPS, no embedded credentials, and no query or fragment in the base URL.
- **Missing model:** set `PERPLEXITY_MODEL` or pass `--model`. The CLI uses explicit model selection, not effort-only or preset-only requests.
- **Using Sonar or the native SDK:** this recipe covers the Agent API through `client.responses.create`, not the native `perplexity` SDK, Search, Embeddings, Router, background runs or legacy Sonar endpoints. See Perplexity's [OpenAI compatibility guide](https://docs.perplexity.ai/docs/agent-api/openai-compatibility) and [Sonar migration guide](https://docs.perplexity.ai/docs/agent-api/migrate-from-sonar/overview).
