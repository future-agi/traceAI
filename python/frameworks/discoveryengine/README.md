# traceAI-discoveryengine

OpenTelemetry instrumentation for the search and answer methods of the
[`google-cloud-discoveryengine`](https://pypi.org/project/google-cloud-discoveryengine/)
client (Google Discovery Engine, also sold as Vertex AI Search).

It traces the v1 `search`, `search_lite` and `answer_query` calls, sync and
async. Conversation and session management (`create_conversation`,
`converse_conversation` and the rest), `stream_answer_query`, document
ingestion, data store and engine administration produce no spans from this
package. It does not instrument `google.genai`; for the Gemini client use
`traceAI-google-genai`.

Status: alpha. Verified against the live `discoveryengine.googleapis.com`
on 2026-10-18 (see [Live vendor verification](#live-vendor-verification-2026-10-18)):
`search` succeeded on the gRPC, REST and gRPC asyncio transports, and the
error paths of `search`, `search_lite` and `answer_query` were recorded from
real server errors. The success paths of `search_lite` and `answer_query`
were not available on that setup and are covered by the loopback tests only.

## Why a wrapper

The client does not trace itself. With `google-cloud-discoveryengine` 0.20.5
an uninstrumented `search`, `search_lite` or `answer_query` emits no span,
including with `GOOGLE_SDK_EXPERIMENTAL_PYTHON_TRACING_ENABLED=true`
(`google-api-core` traces only methods whose transport passes `method_name`
to `wrap_method`, and this client does not).
`tests/test_discoveryengine_stock_spans.py` measures this before any wrapper
is installed. A gRPC client instrumentation you add yourself creates
transport spans, which carry no query, result count or retriever kind.

## Installation

```bash
pip install traceAI-discoveryengine
```

Until the first release reaches PyPI, install it from a checkout of this
repository:

```bash
pip install "fi-instrumentation-otel>=1.1.0" "google-cloud-discoveryengine>=0.20.5,<1" \
  ./python/frameworks/discoveryengine
```

It accepts `google-cloud-discoveryengine>=0.20.5,<1` and
`fi-instrumentation-otel>=1.1.0`. The suite runs with
`google-cloud-discoveryengine` 0.20.5 on Python 3.10, 3.11, 3.12 and 3.13.

## Usage

```python
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from google.cloud import discoveryengine_v1 as discoveryengine
from traceai_discoveryengine import DiscoveryEngineInstrumentor

tracer_provider = register(project_type=ProjectType.OBSERVE, project_name="my-search-app")
DiscoveryEngineInstrumentor().instrument(tracer_provider=tracer_provider)

serving_config = (
    "projects/my-gcp-project/locations/global/collections/default_collection/"
    "engines/my-engine/servingConfigs/default_search"
)
client = discoveryengine.SearchServiceClient()  # Application Default Credentials
pager = client.search(request={"serving_config": serving_config, "query": "open telemetry"})

answers = discoveryengine.ConversationalSearchServiceClient()
answers.answer_query(request={"serving_config": serving_config, "query": {"text": "What is OTLP?"}})
```

Import the clients from `google.cloud.discoveryengine_v1`, as above.
**`from google.cloud import discoveryengine` produces no spans**: at 0.20.5
that unversioned module re-exports the v1beta clients, which are not traced
(nor are v1alpha clients).

The methods are wrapped on the client classes, so clients built before or
after `instrument()` are both traced. See
[`examples/search_and_answer.py`](examples/search_and_answer.py).

### Google credentials and projects

The Google clients keep reading Application Default Credentials (ADC) or the
credentials you pass them, exactly as without this package; it never reads
ADC or adds a credential of its own. `register()` reads `FI_API_KEY` and
`FI_SECRET_KEY` for Future AGI.

The Future AGI project (`project_name` above) is not your GCP project. The
GCP project appears only inside resource names such as the serving config.

An engine in a multi-region location needs its regional endpoint:

```python
client = discoveryengine.SearchServiceClient(
    client_options={"api_endpoint": "eu-discoveryengine.googleapis.com"}
)
```

## Instrumented calls

| Call | Span name |
|---|---|
| `SearchServiceClient.search`, `SearchServiceAsyncClient.search` | `discoveryengine.search` |
| `SearchServiceClient.search_lite`, `SearchServiceAsyncClient.search_lite` | `discoveryengine.search_lite` |
| `ConversationalSearchServiceClient.answer_query`, `ConversationalSearchServiceAsyncClient.answer_query` | `discoveryengine.answer_query` |

What the server allows depends on your app, not on this package:

- `search_lite` is meant for public website search with an API key
  (`client_options={"api_key": ...}`). Against any other data store the
  server answers `FAILED_PRECONDITION` "API key is only supported for public
  website search." (seen live with OAuth credentials and an unstructured
  data store).
- `answer_query` needs an app with the Large Language Model add-on
  (Enterprise edition). On a Standard edition app the server answers
  `FAILED_PRECONDITION` "This feature is only available when Large Language
  Model add-on is enabled." (seen live).

Both errors are traced like any other error (status ERROR,
`discoveryengine.error.status` = `FAILED_PRECONDITION`, code `400`).

Each call produces one span with `gen_ai.span.kind` = `RETRIEVER`. Retries made
by `google-api-core` inside one call (when you pass `retry=`; at 0.20.5 these
methods have no default retry) stay in that one span. The span is a child of the active span
when there is one, and a root span otherwise. It is current while the
transport sends the request, so a span from a gRPC client instrumentation
nests under it.

`search` and `search_lite` return a pager. Iterating past the first page
makes one more RPC of the same method per page, directly through the
transport and not through `search()` or `search_lite()`, so later pages add
no span: the span covers the call that created the pager, and
`discoveryengine.result_count` counts the first page only.

## Span attributes

| Attribute | Calls | Value |
|---|---|---|
| `gen_ai.span.kind` | all | `RETRIEVER` |
| `discoveryengine.serving_config` | requests with `serving_config` | The serving config resource name from the request, cut to 1 KB. |
| `discoveryengine.result_count` | successful calls | `search`, `search_lite`: number of results in the returned (first) page. `answer_query`: number of `references` on the answer (the grounding sources). Omitted when the response has no answer. |
| `discoveryengine.answer.length` | successful `answer_query` with an answer | Length of `answer_text` in characters. The text itself is never recorded. |
| `discoveryengine.answer.state` | successful `answer_query` with an answer | The answer state name, for example `SUCCEEDED` or `FAILED`. |
| `discoveryengine.session` | `answer_query` with a session | The session resource name from the request and then from the response. A request for a new session (`.../sessions/-`) records the name the response returns. |
| `gen_ai.retrieval.query`, `input.value` | requests with a query | `SearchRequest.query` or `AnswerQueryRequest.query.text`, recorded by default. See [Privacy](#privacy). |
| `discoveryengine.error.status`, `discoveryengine.error.code` | failed calls | The gRPC status name (for example `PERMISSION_DENIED`) and the HTTP-style code `google-api-core` maps it to (for example `403`). On the REST transport, where `google-api-core` raises by HTTP code (403 is `Forbidden`, without a gRPC status), the status name is the canonical `status` of the JSON error body. For a `RetryError`, the last attempt's. |
| `discoveryengine.cancelled` | cancelled async calls | `true`; see [Errors and cancellation](#errors-and-cancellation). |

No model name, token count or cost is recorded. At 0.20.5
`AnswerQueryResponse` and `Answer` carry no model field, and the model
version a request asks for (`answer_generation_spec.model_spec`) is not the
model that answered, so none is reported. Discovery Engine is billed in GCP,
not per span.

Spans come from an `fi_instrumentation.FITracer`, so the context attributes
of `using_session`, `using_user`, `using_metadata`, `using_tags` and
`using_attributes` are stamped on every Discovery Engine span started inside
them (`session.id`, `user.id`, `metadata`, `tag.tags`).
`discoveryengine.session` is Discovery Engine's own session and is a
separate attribute.

## Privacy

The query follows `TraceConfig`, like the input of every traceAI
instrumentor: it is recorded by default (`gen_ai.retrieval.query` and
`input.value`, with credentials removed and cut to 1 KB of UTF-8 on a
character boundary), and `FI_HIDE_INPUTS=true` or
`TraceConfig(hide_inputs=True)` hides it everywhere, including server error
text. There is no package-specific switch.

Also recorded: counts, the answer state, the serving config and session
resource names, and error text (status name, code and message).

Never recorded: Google credentials, search results (titles, snippets,
documents, summaries), answer text, citations and reference content,
request metadata, stack traces and local file paths. There is no setting
that adds them.

To keep the query off the trace:

```python
from fi_instrumentation import TraceConfig

DiscoveryEngineInstrumentor().instrument(
    tracer_provider=tracer_provider,
    config=TraceConfig(hide_inputs=True),   # or FI_HIDE_INPUTS=true
)
```

While the query is hidden, each occurrence of it in server-written text
(the error status description and the `exception` event's message) becomes
`__REDACTED__`. That
covers the verbatim text and the escaped copies the client libraries write:
protobuf text format (a status detail that quotes the query, which
`google-api-core` appends to the error message with `'`, `"` and `\`
backslash-escaped, `\n`, `\r` and `\t` as short escapes, and other ASCII
control characters and DEL as 3-digit octal such as `\013`), the hex
escapes of older gRPC cores (the chained gRPC
error's `debug_error_string`, with non-ASCII bytes as `\xHH`; seen with
grpcio 1.62, not with 1.82 or 1.84), and the Python repr and JSON string
forms. A short query is removed wherever it occurs, including inside other
words. A copy escaped or reformatted any other way is not matched.
If the query cannot be read, the error message is recorded as
`__REDACTED__`. Other request fields that a server error may quote (a
`filter`, `user_pseudo_id`) are not removed.

Credentials are removed from every recorded value, including error
messages, and replaced with `[redacted]`. The package looks for them
where the client keeps them: the transport's google.auth credentials
(`token`, `refresh_token`, `client_secret`; an API key credential's
`token`), `client_options.api_key`, and per-call `metadata` entries named
`authorization`, `proxy-authorization`, `x-goog-api-key` or
`x-goog-iam-authorization-token` (with and without the `Bearer` prefix).
It reads them before the call and again when it records an error, so a
token set on those credentials during the call (on first use or by a
refresh) is removed too.
The package reads `metadata` once and passes the client a tuple of the same
pairs, so metadata given as a generator still reaches the server; if
reading it raises, the client raises the same error, as without the package.
Text shaped like a Google credential is replaced too, even if the client
never held it: `ya29.` access tokens, `AIza` API keys, `1//` refresh tokens,
JWTs (`eyJ...`: three base64url parts, such as a service account's
self-signed token), and, in server-written text only, the value after
`Bearer`. If the place the client keeps credentials (or the call's
`metadata`) cannot be read, before or after the call, the call is traced
without the query and with
`[not recorded: the client credentials could not be read]` in place of the
error message.

`TraceConfig` settings apply. Pass `config=TraceConfig(...)` or set the
environment variables before calling `instrument()`:

| Setting | Effect |
|---|---|
| `hide_inputs` / `FI_HIDE_INPUTS=true` | Records `input.value` as `__REDACTED__` and drops `gen_ai.retrieval.query`. The query is also removed from error text as described above. Counts and resource names stay. |
| `hide_outputs` / `FI_HIDE_OUTPUTS=true` | No effect: no output text is recorded. |
| `pii_redaction` / `FI_PII_REDACTION=true` | Replaces emails, phone numbers, SSNs, card numbers, IPv4 addresses and `sk-`/`pk-` style keys with tokens such as `<EMAIL_ADDRESS>` in every recorded text: attributes, the error status and the `exception` event. It runs after credentials (and the hidden query) are removed and before the size caps, so a cut cannot leave part of an email. The patterns also match ids: in a resource name with a GCP project number, the last ten digits are replaced (`projects/123456789012/...` is recorded as `projects/12<PHONE_NUMBER>/...`). |

`config` must be a `fi_instrumentation.TraceConfig`; anything else raises
`TypeError` and nothing is wrapped.

## Errors and cancellation

An error raised by the client (a `google.api_core.exceptions` subclass such
as `PermissionDenied`, `InvalidArgument` or `ServiceUnavailable`, or a
`RetryError` when retries run out) sets the span status to ERROR, records
`discoveryengine.error.status` and `discoveryengine.error.code`, and records
one `exception` event. Credentials are removed from both, and the query when
it is not recorded (see [Privacy](#privacy)). The exception is re-raised
unchanged, so your code still sees the server's original message.

The error text is cleaned in this order: credentials, then the hidden query,
then PII with `pii_redaction`. It is then cut on a UTF-8 character boundary:
the status description's message and `exception.message` to 1 KB. The
`exception` event carries `exception.type` and `exception.message` only:
`exception.stacktrace` is never written, because traceback frames contain
local file paths (home directory, user name, virtualenv).

An `answer_query` that returns an answer with state `FAILED` is not an
exception: the span gets status ERROR with description `answer state FAILED`
and no `exception` event.

Cancelling an async call (`asyncio.CancelledError`) ends the span with
status ERROR, description `cancelled`, and `discoveryengine.cancelled` =
`true`, without an exception event.

If the instrumentation itself fails (reading a request, a response or the
credentials, starting a span, recording an error), it logs at debug level and
your call proceeds with its own result or exception.
`fi_instrumentation.suppress_tracing()` skips the span.

## Limits

- Recorded text is cut on a UTF-8 character boundary after it is cleaned:
  1 KB for the query and each resource name, 1 KB for `exception.message`
  and the message in the error status. No stack trace is recorded.
  With `pii_redaction`, `FITracer` runs its own PII pass on attributes after
  the cut, and a new match can make a value a few bytes longer; that pass
  only redacts more.
- A token carried by a gRPC channel you build yourself (call credentials on
  a `channel=` you pass to the transport) is not visible to the package;
  only the credential shapes above are removed from text.
- When it builds the gRPC channel, `google-api-core` gives it a scoped copy
  of credentials that require scopes, such as service-account credentials
  without scopes. A token minted on that copy during the call (a self-signed
  JWT or an access token) is never on the credentials the client holds, so
  only its shape (`eyJ...` or `ya29.`) removes it.
- Only v1 is traced. `google.cloud.discoveryengine` (v1beta at 0.20.5),
  `discoveryengine_v1beta` and `discoveryengine_v1alpha` clients produce no
  spans.
- `stream_answer_query` and `converse_conversation` are not traced.
- The tests cover the gRPC, gRPC asyncio and REST (`transport="rest"`)
  transports. The async REST transport is not exercised.
- `instrument()` checks the installed `google-cloud-discoveryengine`
  version. Outside `>=0.20.5,<1` it logs an error and wraps nothing.
- Do not combine this package with another instrumentor that wraps the same
  client methods, or calls will produce duplicate spans. A gRPC
  instrumentation adds transport spans under these spans; it does not
  duplicate them.

## Live vendor verification (2026-10-18)

Run against the real `discoveryengine.googleapis.com` (v1, `global`) in a
staging GCP project, with the smallest app: one unstructured data store with
three inline `text/plain` documents and one Standard edition generic search
engine (no LLM add-on). The engine and data store were deleted after the run.
The code followed [Usage](#usage); the only change was
`credentials=google.oauth2.credentials.Credentials(token=..., quota_project_id=...)`
on each client, because the machine had no ADC.

Versions: `google-cloud-discoveryengine` 0.20.5 (latest on PyPI),
`fi-instrumentation-otel` 1.1.0, google-api-core 2.42.0, google-auth
2.62.0, grpcio 1.84.0, protobuf 7.36.2, Python 3.12. Spans were exported
over OTLP/HTTP to a local Future AGI and checked in its store.

| Call | Transport | Live result | Span |
|---|---|---|---|
| `search` | gRPC, REST, gRPC asyncio | 2 results | OK, `discoveryengine.result_count` = 2, query, serving config |
| `search` on a serving config that does not exist | gRPC, REST | `NotFound` 404 | ERROR, `NOT_FOUND` / 404 |
| `search_lite` (OAuth, not a website data store) | gRPC | `FailedPrecondition` 400 | ERROR, `FAILED_PRECONDITION` / 400 |
| `answer_query` (Standard edition app) | gRPC | `FailedPrecondition` 400 | ERROR, `FAILED_PRECONDITION` / 400 |
| `answer_query` (Standard edition app) | REST | `BadRequest` 400 | ERROR, `FAILED_PRECONDITION` (from the JSON error body) / 400 |

With the defaults the query was stored on every Discovery Engine span; with
`FI_HIDE_INPUTS=true` it was in none of the stored columns (`input.value` =
`__REDACTED__`, no `gen_ai.retrieval.query`). In both modes the Discovery
Engine spans held no result content (titles, snippets), and no stored column
held a credential, the access token, a local path, the user name, the host
name, the account's e-mail address or a stack trace. The real error messages
seen did not quote the query.

Not verified live: a successful `search_lite` (needs a public website data
store and an API key), a successful `answer_query` and its answer
attributes (`discoveryengine.answer.*`, `discoveryengine.session`; needs the
LLM add-on), pagination, a regional endpoint, the async REST transport, and
removal of a hidden query from a server error that quotes it. The loopback
tests cover these.

## Tests

The three error bodies the live API returned on 2026-10-18 are replayed in
`tests/test_discoveryengine_rest.py` (`test_live_rest_error_bodies_record_status_and_code`).

The tests drive the real `google-cloud-discoveryengine` clients over their
own gRPC transports against a gRPC fake on 127.0.0.1
(`tests/_discoveryengine_support.py`), and over the REST transport against a
loopback HTTP server that answers the documented v1 REST routes
(`tests/test_discoveryengine_rest.py`). They use no Google credentials, no
ADC and no GCP project: credentials are placeholders, and the serving config
is a placeholder resource name. The contract tests export through the real
`register()` OTLP exporter into the shared `harness.Receiver`, and post
captured spans with `harness.post_otlp()`.

From the repository root:

```bash
env -u PYTHONPATH PYTHONPATH="python/frameworks/discoveryengine:python:python/tests" \
  uv run --no-project --python 3.11 \
  --with pytest --with pytest-asyncio --with opentelemetry-api --with opentelemetry-sdk \
  --with opentelemetry-instrumentation --with opentelemetry-exporter-otlp-proto-http \
  --with wrapt --with requests --with jsonschema --with protobuf --with opentelemetry-proto \
  --with 'google-cloud-discoveryengine==0.20.5' \
  pytest python/frameworks/discoveryengine/tests -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```

To run against the published `fi-instrumentation-otel` wheel instead of the
in-repo source, drop `python:` from `PYTHONPATH` and add
`--with 'fi-instrumentation-otel==1.1.0'`.

Replace `3.11` with `3.10`, `3.12` or `3.13` for the other supported
Pythons.

The stock-span measurement (`tests/test_discoveryengine_stock_spans.py`)
also runs with `opentelemetry-instrumentation-grpc` installed. Then
`GOOGLE_SDK_EXPERIMENTAL_PYTHON_TRACING_ENABLED=true` turns
`google-api-core`'s experimental tracing on, and the file checks that the
unwrapped client still emits no span:

```bash
env -u PYTHONPATH PYTHONPATH="python/frameworks/discoveryengine:python:python/tests" \
  uv run --no-project --python 3.11 \
  --with pytest --with pytest-asyncio --with opentelemetry-api --with opentelemetry-sdk \
  --with opentelemetry-instrumentation --with opentelemetry-exporter-otlp-proto-http \
  --with wrapt --with requests --with jsonschema --with protobuf --with opentelemetry-proto \
  --with 'google-cloud-discoveryengine==0.20.5' --with opentelemetry-instrumentation-grpc \
  pytest python/frameworks/discoveryengine/tests/test_discoveryengine_stock_spans.py \
  -q -p no:cacheprovider --noconftest -o addopts= -rfEs
```
