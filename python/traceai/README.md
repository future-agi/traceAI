# traceai

Initialize Future AGI OpenTelemetry tracing with one call. The `traceai.init`
helper can also enable instrumentors that you have installed separately.

[Documentation](https://docs.futureagi.com/docs/tracing) · [Issues](https://github.com/future-agi/traceAI/issues)

## Install from this repository

This helper is not published by Future AGI on PyPI. The `traceai` name on PyPI
belongs to another project; do **not** use `pip install traceai` for this helper.
From the root of a traceAI checkout, install the local package instead:

```sh
pip install ./python/traceai
```

Set `FI_API_KEY` and `FI_SECRET_KEY` in your environment for Future AGI export.
This example creates a manual span; it does not call an LLM.

## Quick start

```python
import traceai

provider = traceai.init(
    project_name="my-ai-app",
    project_type="observe",
)
tracer = provider.get_tracer(__name__)
with tracer.start_as_current_span("example"):
    pass  # Put your application work here.
provider.shutdown()
```

To opt in to OpenAI instrumentation, install `traceAI-openai` separately and pass
`auto_instrument=["openai"]` to `traceai.init` before constructing the OpenAI client.
The helper does not install instrumentors or supply provider credentials. See
[OpenAI setup](https://docs.futureagi.com/docs/integrations/traceai/openai) for a
provider-specific example.

## License

Apache-2.0.
