# Cognee example

Send Cognee's native add, cognify and search spans to Future AGI with the maintained `traceai-cognee` adapter. Do not add a second Cognee or LiteLLM instrumentor.

## Run

Use Python 3.10–3.13. Install the dependencies, then configure Cognee's model and embedding providers using the [Cognee documentation](https://docs.cognee.ai/).

```bash
pip install -r requirements.txt
export COGNEE_TRACING_ENABLED=true
export FI_API_KEY="YOUR_FI_API_KEY"
export FI_SECRET_KEY="YOUR_FI_SECRET_KEY"
export FI_PROJECT_NAME="my-cognee-app"
python src/app.py "Who works on Lighthouse?"
```

Open **Observe → Tracing** in Future AGI and select `my-cognee-app`. The application wraps its operations in one `remember_and_ask` parent span and flushes after the operations end.

## Content and lifecycle

Content capture is off by default. Pass `capture_content=True` to `configure` only for approved application data. Recognized credential fields are redacted; free text is not scanned for secrets. Error descriptions and stack traces stay excluded in both modes. Span names are retained, so keep sensitive data out of them. Other processors on the provider receive native, unmodified spans and need their own privacy controls.

Configure before the first Cognee operation. End spans and call `handle.force_flush()` before `handle.shutdown()`; a span still open at shutdown is not exported. The handle owns only the adapter's exporter, not other processors on the provider. Cognee's `cognee.disable_tracing()` and `cognee.shutdown_tracing()` shut down the shared provider too, so do not use them while other application tracing is active.

Search spans are mapped to `RETRIEVER` and structured-output model calls to `LLM`. Native error statuses, including missing-collection probes, are preserved. See the [adapter reference](../../frameworks/cognee/) for configuration options and lifecycle limits.
