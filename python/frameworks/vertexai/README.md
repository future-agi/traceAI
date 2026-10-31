# Trace Vertex AI requests

Trace Gemini requests on Vertex AI in Future AGI using the Google Gen AI SDK.

## Choose the SDK

For new applications, use [`traceAI-google-genai`](../google-genai/README.md) with `google-genai`. Its `GoogleGenAIInstrumentor` wraps the SDK's synchronous and asynchronous generation and streaming methods, including clients configured with `vertexai=True`.

`traceAI-vertexai` is the **legacy** integration for `google-cloud-aiplatform`. Google deprecated its generative AI modules, including `vertexai.generative_models`, and [announced their removal after June 24, 2026](https://cloud.google.com/vertex-ai/generative-ai/docs/deprecations/genai-vertexai-sdk). This package supports `google-cloud-aiplatform>=1.49.0,<=1.101.0`; the upper bound retains a release containing those modules. It does not instrument `google-genai`. Do not install the separate `vertexai` distribution to set up either route.

## Set up Google Gen AI (recommended)

Install the maintained instrumentation package and SDK:

```bash
pip install traceAI-google-genai google-genai
```

You need a Google Cloud project with Vertex AI enabled, access to your chosen model in the selected location, and Application Default Credentials. For local development:

```bash
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT="your-project-id"
export GOOGLE_CLOUD_LOCATION="global"
export GOOGLE_GENAI_MODEL="your-enabled-model-id"
export FI_API_KEY="your-future-agi-api-key"
export FI_SECRET_KEY="your-future-agi-secret-key"
```

Use a model ID available to your project and location. In a deployed application, use its service account or another supported Application Default Credentials configuration instead of an interactive login.

Register the tracer and instrument the SDK before making requests:

```python
import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from google import genai
from traceai_google_genai import GoogleGenAIInstrumentor


def main():
    trace_provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="vertexai_app",
    )
    GoogleGenAIInstrumentor().instrument(tracer_provider=trace_provider)
    try:
        client = genai.Client(
            vertexai=True,
            project=os.environ["GOOGLE_CLOUD_PROJECT"],
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
        )
        response = client.models.generate_content(
            model=os.environ["GOOGLE_GENAI_MODEL"],
            contents="Write a haiku about a mountain trail.",
        )
        print(response.text)
    finally:
        trace_provider.force_flush()


if __name__ == "__main__":
    main()
```

This example is also available as [`examples/google_genai_vertex.py`](examples/google_genai_vertex.py). Open the `vertexai_app` project in Future AGI Observe to inspect the generation span, input, output and token usage returned by the SDK.

## Keep an existing legacy application running

For applications that still import `vertexai.generative_models`, install the bounded legacy dependency rather than upgrading it past the supported range:

```bash
pip install traceAI-vertexai 'google-cloud-aiplatform==1.101.0'
```

Use the same Google Cloud and Future AGI environment variables above. Instrument **before importing `vertexai`** so the legacy SDK's GAPIC clients are wrapped when they are created:

```python
import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_vertexai import VertexAIInstrumentor

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="vertexai_legacy_app",
)
VertexAIInstrumentor().instrument(tracer_provider=trace_provider)

import vertexai
from vertexai.generative_models import GenerativeModel

vertexai.init(
    project=os.environ["GOOGLE_CLOUD_PROJECT"],
    location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
)
try:
    response = GenerativeModel(os.environ["GOOGLE_GENAI_MODEL"]).generate_content(
        "Write a haiku about a mountain trail."
    )
    print(response.text)
finally:
    trace_provider.force_flush()
```

The pin preserves the SDK modules, not retired models or service endpoints. Migrate the application's imports, client construction and calls using [Google's migration guide](https://cloud.google.com/vertex-ai/generative-ai/docs/deprecations/genai-vertexai-sdk), then replace `VertexAIInstrumentor` with `GoogleGenAIInstrumentor`. Do not enable both instrumentors for the same SDK call.

## Troubleshooting

- **`ModuleNotFoundError: vertexai.generative_models`**: migrate to the recommended Google Gen AI route, or install the legacy version shown above. Installing `vertexai` separately is not the migration.
- **No generation spans after switching to `google-genai`**: replace `VertexAIInstrumentor` with `GoogleGenAIInstrumentor`; they target different SDKs.
- **Authentication, model-not-found or location errors**: check Application Default Credentials, Vertex AI permissions, and the model's availability in your project and location. Changing the tracing package does not restore retired models.
