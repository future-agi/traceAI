# vLLM client tracing

Trace requests from the OpenAI Python client to a vLLM server with the shared
`traceAI-openai` integration. For inference-engine and server spans, use
[vLLM's native OpenTelemetry setup](https://docs.vllm.ai/en/latest/examples/observability/opentelemetry/)
separately; this client integration does not instrument the server.

## Install and configure

Use Python 3.10–3.13 and OpenAI >= 1.69.0 for the shared route. Install a
`traceAI-openai` release that exports `set_client_provider`:

```bash
pip install traceAI-openai
```

Set `FI_API_KEY` and `FI_SECRET_KEY` to your Future AGI credentials. Set
`VLLM_MODEL` to the served model name and `VLLM_API_KEY` to your server's API key
(or a non-empty placeholder only if your server has authentication disabled).
`VLLM_BASE_URL` defaults to `http://localhost:8000/v1` in this example.

```python
import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from openai import OpenAI
from traceai_openai import OpenAIInstrumentor, set_client_provider

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="vllm-app",
)
OpenAIInstrumentor().instrument(tracer_provider=trace_provider)

try:
    with OpenAI(
        base_url=os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1"),
        api_key=os.environ["VLLM_API_KEY"],
    ) as client:
        set_client_provider(client, provider="vllm")
        response = client.chat.completions.create(
            model=os.environ["VLLM_MODEL"],
            messages=[{"role": "user", "content": "Explain continuous batching briefly."}],
        )
        print(response.choices[0].message.content or "")
finally:
    trace_provider.shutdown()
```

Open the `vllm-app` project in Future AGI to inspect the client request span.
The explicit provider setting labels it `vllm`; a localhost address alone does
not identify a provider. Set it on each `OpenAI` or `AsyncOpenAI` client before
requests, including clients created by `copy()` or `with_options()`. It changes
trace metadata only and is not sent to the server.

The shared instrumentor captures sync/async chat, consumed streams, embeddings,
Responses and request failures. Use only APIs supported by your deployed vLLM
version and model. Embeddings require an embedding-capable model. Streaming
spans finish when the stream finishes. Configure `TraceConfig` on the shared
instrumentor to control input, output, embedding and error-detail capture.

## Migrate from `VLLMInstrumentor`

1. Upgrade OpenAI to >= 1.69.0 and install the shared package above.
2. Replace the `VLLMInstrumentor(...).instrument(...)` call with
   `OpenAIInstrumentor().instrument(...)`, preserving your tracer provider and
   privacy configuration.
3. Replace `vllm_base_urls` configuration with
   `set_client_provider(client, provider="vllm")` on each vLLM client.
4. Remove the legacy instrumentor from your application. You can then uninstall
   `traceAI-vllm` if no other application in the environment uses it.

During migration, if both instrumentors are enabled, the shared instrumentor
owns client spans and its tracer provider and privacy configuration apply.
The legacy wrapper does not emit a second span. This works in either setup
order; disabling the shared instrumentor restores legacy chat tracing if the
legacy instrumentor is still enabled. Legacy URL settings do not configure the
shared instrumentor: keep the explicit per-client provider setting.

## Legacy compatibility

`traceAI-vllm` retains its public `VLLMInstrumentor` entry point and its
OpenAI >= 1.0.0 dependency floor for existing applications. It emits a
`DeprecationWarning` when enabled; the package has not been removed. Applications
that must stay on OpenAI 1.0–1.68 can continue using their existing setup.

The legacy instrumentor is **chat-completions only**, including sync/async chat
and consumed text streams for configured URLs (`localhost:8000` by default).
It does not trace embeddings, Responses, the native vLLM engine, or server-side
scheduling. Migrate to the shared route for the additional client APIs; enable
native server telemetry separately when you need server spans. Do not interpret
a client span as an inference-engine span or assume automatic correlation with
native server spans.

## Troubleshooting

- **`set_client_provider` cannot be imported:** your installed `traceAI-openai`
  release predates this API. Upgrade to a release containing it before switching
  instrumentors; do not remove a working legacy setup first.
- **Provider is `openai`:** set the override on the exact client making the
  request, including any copied client.
- **No embedding or Responses span with the legacy package:** those APIs are
  outside its chat-only scope. Use the shared route and confirm that your server
  supports the requested API.

## License

Apache-2.0
