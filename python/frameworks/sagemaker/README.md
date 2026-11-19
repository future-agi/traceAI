# traceAI-sagemaker

OpenTelemetry instrumentation for Amazon SageMaker real-time endpoints called through boto3's
`sagemaker-runtime` client.

## Install

```bash
pip install traceAI-sagemaker
```

## Use

```python
import boto3
from fi_instrumentation import register
from traceai_sagemaker import SageMakerInstrumentor

trace_provider = register(project_name="my-project")
SageMakerInstrumentor().instrument(tracer_provider=trace_provider)

# Create the client after instrumenting.
runtime = boto3.client("sagemaker-runtime", region_name="us-east-1")
response = runtime.invoke_endpoint(
    EndpointName="my-llm-endpoint",
    ContentType="application/json",
    Body='{"inputs": "What is the capital of France?", "parameters": {"max_new_tokens": 32}}',
)
print(response["Body"].read())
```

With `fi-instrumentation-otel` auto-instrumentation (`fi-instrument`, or `FI_INSTRUMENTATIONS`),
the `sagemaker` integration is enabled whenever `boto3` is installed and this package is present.

## What is traced

| Call | Span | Notes |
| --- | --- | --- |
| `invoke_endpoint` | `sagemaker.invoke_endpoint` | Request and response bodies, token usage. |
| `invoke_endpoint_async` | `sagemaker.invoke_endpoint_async` | Request span only: the payload is in S3 and there is no output. |
| `invoke_endpoint_with_response_stream` | `sagemaker.invoke_endpoint_with_response_stream` | Ends when the stream is exhausted, fails or is closed. |

Each span has kind `LLM`, `gen_ai.provider.name = aws.sagemaker`, and `gen_ai.request.model` set to
the endpoint name. `aws.sagemaker.inference_component` and `aws.sagemaker.target_variant` are set
when passed. A `ClientError` (or any other exception) is re-raised unchanged and recorded as
`ERROR` with `error.type`; the exception message is never exported.

### Content capture

* Bodies are captured as content only when their `ContentType` says JSON (`application/json`,
  `application/jsonlines`, `*+json`) or text (`text/*`). Any other type, including a missing
  `ContentType`, is noted as `[binary content: <type>, <n> bytes]` and the bytes are never recorded.
  Binary and file-like bodies are not read for tracing.
* The hide/capture settings of `TraceConfig` apply (`hide_inputs`, `hide_outputs`, and the text,
  message, image and invocation-parameter variants).
* Token usage and output text are read best-effort from common JSON shapes: Hugging Face TGI
  (`generated_text`, `details.generated_tokens`, `details.prefill`), OpenAI-compatible
  (`choices`, `usage`), Anthropic-style (`content`, `usage`) and Bedrock-style
  (`generation`, `prompt_token_count`, `generation_token_count`). An unrecognised shape records the
  raw decoded body and no counts; it never fails the call.
* Streaming responses are reassembled from `PayloadPart` chunks, including JSON lines split across
  chunks and `data:` server-sent events.

### Running with Bedrock

`traceAI-bedrock` and `traceAI-sagemaker` both hook boto3 client creation. Each only wraps its own
services (`bedrock-*` and `sagemaker-runtime`), so install and instrument both freely.

Only the Python SDK clients created after `instrument()` are traced.
