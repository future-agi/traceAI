# traceAI-typesafe

OpenTelemetry instrumentation for the [TypeSafe AI](https://typesafe.ai) Python SDK
(`typesafe-sdk`), the client for Jev's System One decision API (`POST /v1/systemone`).

Every `TypeSafeClient.system_one(...)` and `AsyncTypeSafeClient.system_one(...)` call becomes
one `EVALUATOR` span in Future AGI, however many questions it asks. Each question becomes
`typesafe.question.<name>.*` attributes:

| Answer type | Attributes |
| --- | --- |
| `noul` | `type`, `noul` (probability of yes, a number, never Pass/Fail) |
| `choice` | `type`, `choice` (the label), `confidence` |
| `score` | `type`, `score` (expected score, a number), `confidence` |
| `box` | `type`, `box.top`, `box.bottom`, `box.left`, `box.right`, `confidence` when not null |

The span also carries `gen_ai.provider.name=typesafe`, `gen_ai.request.model` (as you asked
for it), `gen_ai.response.model` plus `typesafe.response.model` when the API resolved an
alias, `gen_ai.usage.input_tokens` / `output_tokens`, `typesafe.request_id`, `server.address`
and `typesafe.question_count`. Probability maps, score legends and image bytes are never
recorded. The API returns no explanation, so the span has none.

This package traces calls your code makes to TypeSafe. It does not add Jev as a judge model
for Future AGI evals, and it does not call the API itself.

## Install

```bash
pip install traceAI-typesafe typesafe-sdk
```

## Use

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_typesafe import TypeSafeInstrumentor
from typesafe_sdk import Choice, Noul, TypeSafeClient

trace_provider = register(project_type=ProjectType.OBSERVE, project_name="support-triage")
TypeSafeInstrumentor().instrument(tracer_provider=trace_provider)

with TypeSafeClient() as client:  # reads TYPESAFE_API_KEY
    result = client.system_one(
        state="I was charged twice. Please help.",
        questions={
            "billing": Noul(instructions="Is this about billing?"),
            "tone": Choice(instructions="What is the tone?", criteria={"calm": None, "angry": None}),
        },
    )
trace_provider.force_flush()
```

The instrumentor never sets `base_url` or a model. Point the client wherever you already do
(`base_url=` or `TYPESAFE_BASE_URL`); `server.address` records the host.

When you supply `extra_body`, captured state, questions and request model follow the SDK's
shallow, last-write-wins overrides. Replaced inputs and question names are not retained;
privacy settings apply to the effective values. Other `extra_body` fields are not captured.

## Privacy

Like every traceAI instrumentor, content is captured by default:

| Setting | Effect |
| --- | --- |
| default | `input.value` = the `state` and the questions (instructions, criteria); `output.value` and `typesafe.question.<name>.choice` = the decisions |
| `FI_HIDE_INPUTS=true` (or `TraceConfig(hide_inputs=True)`, `FI_HIDE_INPUT_TEXT`) | no `input.value`, no `typesafe.idempotency_key` |
| `FI_HIDE_OUTPUTS=true` (or `TraceConfig(hide_outputs=True)`, `FI_HIDE_OUTPUT_TEXT`) | no `output.value`, no `choice` labels; numeric answers stay |
| `FI_PII_REDACTION=true` | regex PII redaction on every string attribute and the error message |

Always recorded: question names and types (they are attribute keys, so do not put secrets in
question names), numeric answers, model, usage, request id. Never recorded: the API key,
stack traces, the text of client-side errors (they can name a local file), image bytes or
paths. The API's own error message is added to the span status only while neither inputs nor
outputs are hidden.

## Errors and retries

An HTTP error (401, 403, 422, 429, 5xx) or a connection error ends the span with `ERROR`,
`error.type`, `http.response.status_code`, the server's `typesafe.error_type` and the request
id. The instrumentor adds no retries. The SDK's own `RetryPolicy` (by default up to 2 retries
for 408, 429 and 5xx) runs inside the one span, so a 429 followed by a success is one `OK`
span. A call you repeat yourself is a second span.

## Tests

```bash
pip install -e . pytest pytest-asyncio
pytest
```

The tests run the real `typesafe-sdk` clients against an in-process fake shaped like
TypeSafe's published OpenAPI document. They never contact `api.typesafe.ai`.
