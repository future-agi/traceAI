# traceAI-discoveryengine

Trace Google Discovery Engine (Vertex AI Search) retrieval, answers,
conversation turns, ranking and grounding checks in Future AGI with the
[official Python client](https://pypi.org/project/google-cloud-discoveryengine/).

## Install

```bash
pip install traceAI-discoveryengine
```

Supports Python 3.10, 3.11, 3.12 and 3.13. Requires
`google-cloud-discoveryengine>=0.20.5,<1` and
`fi-instrumentation-otel>=1.1.0`. Use the **v1** clients explicitly:
`google.cloud.discoveryengine` re-exports v1beta clients at
`google-cloud-discoveryengine` 0.20.5 and is not instrumented.

## Configure and search

Set `FI_API_KEY` and `FI_SECRET_KEY` in your environment for Future AGI.
Configure [Google Application Default Credentials](https://cloud.google.com/docs/authentication/provide-credentials-adc)
for your Google client, or pass it credentials as usual. The integration
does not change how the client authenticates.

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from google.cloud import discoveryengine_v1 as discoveryengine
from traceai_discoveryengine import DiscoveryEngineInstrumentor

provider = register(project_type=ProjectType.OBSERVE, project_name="support-search")
DiscoveryEngineInstrumentor().instrument(tracer_provider=provider)

serving_config = (
    "projects/my-gcp-project/locations/global/collections/default_collection/"
    "engines/my-engine/servingConfigs/default_search"
)
search = discoveryengine.SearchServiceClient()
pager = search.search(request={"serving_config": serving_config, "query": "rotate an API key"})
print(len(pager.results))
provider.force_flush()
```

Replace the serving config with your engine's resource name. The Future AGI
project (`support-search`) is separate from your GCP project. For a regional
engine, pass the appropriate endpoint to the Google client, for example
`client_options={"api_endpoint": "eu-discoveryengine.googleapis.com"}`.

Open **Observe** in Future AGI and select your project. The call appears as a
search span with its query, duration, status and first-page result count.
Inside an active application span, Discovery Engine calls appear as children.
Existing clients and clients created after `instrument()` are both traced.

## Supported operations

All rows support the synchronous `Client` and asynchronous `AsyncClient`.
Span names use the `discoveryengine.` prefix followed by the method name.

| Service | Method | Span kind |
|---|---|---|
| `SearchService` | `search`, `search_lite` | `RETRIEVER` |
| `ConversationalSearchService` | `answer_query`, `stream_answer_query`, `converse_conversation` | `CHAIN` |
| `RankService` | `rank` | `RERANKER` |
| `GroundedGenerationService` | `check_grounding` | `EVALUATOR` |

Answers are retrieval-and-generation operations, not raw model calls. The
integration does not invent a model name, token usage or cost. Google API
retries remain inside one logical span.

Search spans end when the initial pager is returned. Later pages are not
consumed by the instrumentor and do not add spans; the result count covers
only the first page. Administrative operations (including conversation/session
creation, ingestion and engine management) and v1alpha/v1beta clients are not
instrumented. Use `traceAI-google-genai` for the separate Gemini client.

## Stream an answer

```python
answers = discoveryengine.ConversationalSearchServiceClient()
stream = answers.stream_answer_query(
    request={"serving_config": serving_config, "query": {"text": "How do I rotate an API key?"}}
)
try:
    for response in stream:
        print(response.answer.state.name)
finally:
    stream.close()
provider.force_flush()
```

For an async client, obtain the stream with
`stream = await answers.stream_answer_query(request=...)`, consume it with
`async for`, and call `await stream.aclose()` when stopping early.

The span stays open until the stream is exhausted, closed or cancelled. The
wrapper does not prefetch or retain response chunks. Only the final complete
answer supplies answer text, length and reference counts; Google's terminal
snapshot is not added to its preceding text deltas. A stream that ends without
a `SUCCEEDED` or `FAILED` answer is marked incomplete. Closing early or
cancelling marks it cancelled. Exhaust or explicitly close streams to finish
traces promptly; a plain `break` does not close an iterator.

## Control captured content

By default, queries are captured but response content is not. To include a
bounded projection of answer text, citations, reference chunks, search titles
and snippets, conversation summaries, ranked records and grounded claims:

```python
DiscoveryEngineInstrumentor().instrument(
    tracer_provider=provider,
    capture_content=True,
)
```

Choose these options in your initial `instrument()` call, not a second call
on an already instrumented process. Content appears as JSON in `output.value`.
Only named content fields are projected; the full vendor response is never
dumped. Each text field is capped at 1 KB, each collection at 20 items and the
complete JSON value at 16 KB. Larger outputs are partial and are not a copy of
the complete response. Streaming text deltas are never captured individually.

Use `TraceConfig` or the corresponding environment variables to restrict data:

```python
from fi_instrumentation import TraceConfig

DiscoveryEngineInstrumentor().instrument(
    tracer_provider=provider,
    config=TraceConfig(hide_inputs=True, hide_outputs=True),
)
```

- `hide_inputs=True` / `FI_HIDE_INPUTS=true` hides the query and disables
  response-content capture, since an output can repeat an input.
- `hide_outputs=True` / `FI_HIDE_OUTPUTS=true` disables response-content
  capture even when `capture_content=True`.
- `pii_redaction=True` / `FI_PII_REDACTION=true` applies the core PII patterns
  before text is truncated. Counts and resource names remain; PII patterns
  can also mask numeric identifiers in resource names.

Credentials visible on the client's transport, client options or auth metadata
are removed before text is capped, including credentials refreshed during a
call. Google token shapes and bearer values in server text are also removed.
If credential lookup fails, no response content is recorded. Tokens attached
to a custom gRPC channel or a private credential copy are not readable by the
instrumentor; only recognized token shapes can be removed in those cases.
Avoid putting credentials in application content.

Queries and resource names are capped at 1 KB. Error text is capped at 1 KB;
`exception.stacktrace` is never written. On search/answer errors, hidden query
text and its common escaped forms are removed. Other fields echoed by a
server, such as filters, are not matched by that query-redaction step. For
streaming, conversation, ranking and grounding errors, server-written messages are
omitted/redacted entirely because they can contain records, facts or history.
Core `TraceConfig` error-detail controls continue to apply.

## Attributes and errors

| Attribute | Meaning |
|---|---|
| `input.value` | Query text, or the grounding answer candidate; hidden with `hide_inputs`. |
| `gen_ai.retrieval.query` | Search, answer and conversation query. |
| `gen_ai.reranker.query` | Ranking query. |
| `discoveryengine.serving_config` | Serving config resource name. |
| `discoveryengine.ranking_config`, `discoveryengine.grounding_config` | Operation resource names. |
| `discoveryengine.result_count` | First-page search results, answer references, conversation search results, ranked records or cited grounding facts. |
| `discoveryengine.answer.length`, `discoveryengine.answer.state` | Answer character count and state; final snapshot for a stream. |
| `discoveryengine.session` | Answer session resource name, when present. |
| `discoveryengine.grounding.support_score` | Grounding support score returned by Google. |
| `discoveryengine.stream.chunk_count` | Number of response chunks consumed. |
| `discoveryengine.error.status`, `discoveryengine.error.code` | Google error status and HTTP-style code. |
| `discoveryengine.cancelled` | `true` for async cancellation or early stream closure/cancellation. |

Raised exceptions are re-raised unchanged and produce an ERROR span. A returned
answer in state `FAILED` also produces ERROR, without inventing an exception.
Cancellation ends the span without an exception event. Instrumentation failures
do not replace your SDK result or exception. `suppress_tracing()` skips tracing,
and `DiscoveryEngineInstrumentor().uninstrument()` restores the client methods.
Context from `using_session`, `using_user`, `using_metadata`, `using_tags` and
`using_attributes` is preserved; the Google session is separate from `session.id`.

## Troubleshooting

- **No spans:** import `discoveryengine_v1`, call `register()` before
  `instrument()`, and flush the provider before a short-lived process exits.
- **`FAILED_PRECONDITION`:** `search_lite` requires public website search with
  an API key. Answer generation requires an app with the appropriate LLM
  add-on; check your engine's configuration.
- **Duplicate spans:** do not install another wrapper for the same Google
  client methods. Generic gRPC tracing supplies transport spans, not these
  semantic spans; it can run underneath this integration.
- **Missing streamed answer:** consume the stream through its terminal answer.
  Closing early records cancellation rather than a complete answer.
