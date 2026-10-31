# OrcaRouter (OpenAI-compatible) with traceAI

Trace OrcaRouter Chat Completions in Future AGI with the official OpenAI Python SDK and the shared `traceAI-openai` instrumentor. No OrcaRouter-specific tracing package is needed.

## Install

From this directory:

```bash
pip install -r requirements.txt
```

Or install the same packages directly:

```bash
pip install 'openai==3.24.0' 'traceAI-openai==0.1.11' 'fi-instrumentation-otel==1.1.0'
```

Use `traceAI-openai` 0.1.11 or later for OrcaRouter provider attribution and model names on streamed and failed calls.

## Configure

Set your keys and a model available to your OrcaRouter workspace:

```bash
export FI_API_KEY="<your-futureagi-api-key>"
export FI_SECRET_KEY="<your-futureagi-secret-key>"
export ORCAROUTER_API_KEY="<your-orcarouter-api-key>"
export ORCAROUTER_MODEL="orcarouter/auto"
```

| Variable | Purpose | Sent to |
|---|---|---|
| `FI_API_KEY` | Future AGI API key | Tracer only |
| `FI_SECRET_KEY` | Future AGI secret key | Tracer only |
| `ORCAROUTER_API_KEY` | OrcaRouter API key; required | OpenAI client only |
| `ORCAROUTER_MODEL` | Requested model; required unless `--model` is given | OpenAI client |
| `ORCAROUTER_BASE_URL` | Optional override; defaults to `https://api.orcarouter.ai/v1` | OpenAI client |
| `FI_BASE_URL` | Optional Future AGI collector origin | Tracer only |
| `FI_PROJECT_NAME` | Optional Observe project name; defaults to `orcarouter-example` | Tracer only |

Use `https://api.orcarouter.ai/v1` for the [OpenAI-compatible endpoint](https://docs.orcarouter.ai/compatibility/openai-sdk), not the root URL or `/v1/chat/completions`. The CLI rejects URL credentials, malformed URLs, and invalid hosted endpoint paths before tracing. Use `ORCAROUTER_API_KEY` rather than embedding a key in a URL.

## Run

From this directory:

```bash
python src/app.py
python src/app.py --stream
```

Use `--model` to override `ORCAROUTER_MODEL`, or `--prompt` to supply a question. Set `FI_PROJECT_NAME` to send traces to another Observe project.

## Trace your application

Instrument once before creating the OpenAI client:

```python
import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from openai import OpenAI
from traceai_openai import OpenAIInstrumentor

provider = register(
    project_name=os.environ.get("FI_PROJECT_NAME") or "orcarouter-example",
    project_type=ProjectType.OBSERVE,
    set_global_tracer_provider=False,
    verbose=False,
)
OpenAIInstrumentor().instrument(tracer_provider=provider)
try:
    with OpenAI(
        base_url="https://api.orcarouter.ai/v1",
        api_key=os.environ["ORCAROUTER_API_KEY"],
    ) as client:
        response = client.chat.completions.create(
            model=os.environ["ORCAROUTER_MODEL"],
            messages=[{"role": "user", "content": "What is a coral reef?"}],
        )
        print(response.choices[0].message.content)
finally:
    provider.force_flush()
```

For streamed token counts, pass `stream=True, stream_options={"include_usage": True}` to `client.chat.completions.create(...)` and consume the stream completely. OrcaRouter sends usage in the final chunk before `[DONE]`; without it, token counts are omitted rather than reported as zero. The CLI's `--stream` option leaves `stream_options` unset. See [streaming and usage](https://docs.orcarouter.ai/advanced/streaming).

## View your traces

Open **Observe** in Future AGI and select **orcarouter-example**, or your `FI_PROJECT_NAME`. Each Chat Completions call produces one `ChatCompletion` LLM span with `gen_ai.provider.name=orcarouter` when using `api.orcarouter.ai`.

For successful non-streamed calls, `gen_ai.request.model` contains the model returned by OrcaRouter. With `orcarouter/auto`, this can differ from the requested alias. Streamed and failed calls retain the requested model in `gen_ai.request.model`. Request options are recorded in `gen_ai.request.parameters` unless hidden. Token usage appears when OrcaRouter includes it; assistant text appears in `output.value`.

This route traces calls made through the instrumented OpenAI SDK. It does not import OrcaRouter's metrics or console logs, or trace its separate protocol endpoints.

## Privacy

Set `FI_HIDE_INPUTS=true` to mask input attributes and `FI_HIDE_OUTPUTS=true` to mask output attributes. Set `FI_HIDE_LLM_INVOCATION_PARAMETERS=true` to omit request parameters while retaining the model name, including on streamed and failed calls. Set these variables before tracing is initialized.

These settings control trace content sent to Future AGI. They do not remove prompts from requests sent to OrcaRouter or change OrcaRouter's own logging.

## Troubleshooting

- **No traces:** Confirm both Future AGI keys are set in the same shell as the app, instrument before making calls, and flush the provider before exiting.
- **Provider shows `openai` at the hosted endpoint, or streamed/error model names are missing:** Reinstall the requirements above to use `traceAI-openai==0.1.11`. Custom proxy URLs may use a different provider label.
- **OrcaRouter returns 401:** Use your OrcaRouter key, not an OpenAI or Future AGI key.
- **OrcaRouter returns `403 insufficient_user_quota`:** `orcarouter/auto` requires a funded OrcaRouter wallet. Add credit or choose a model available to your workspace. Provider errors are recorded as ERROR spans; they are not Future AGI ingest failures. See [OrcaRouter errors](https://docs.orcarouter.ai/operations/errors).
