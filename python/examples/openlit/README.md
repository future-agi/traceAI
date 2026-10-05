# OpenLIT → Future AGI (recipe)

There is no traceAI package for OpenLIT and no OpenLIT adapter in Future
AGI. OpenLIT already exports OTLP/HTTP. This example points `openlit.init()`
at fi-collector, passes the Future AGI keys as OTLP headers, puts the
project on the resource with `OTEL_RESOURCE_ATTRIBUTES`, and turns off
metrics, events, OpenLIT's price download and its message capture. With
message capture off, the prompt and the answer are not exported, but
model-generated tool-call arguments, the `user` argument and error bodies
still are (see "Privacy"). The recipe adds no mapping or filtering of its
own: the key inventory below lists what OpenLIT sent and which of those keys
fi-collector reads.

Pinned: `openlit` 1.45.0. Its wheel METADATA declares `License: Apache-2.0`
and the OSI Apache classifier, ships the Apache 2.0 text, and declares
`Requires-Python: >=3.9.0,<4.0.0`. It requires `openai>=1.92.0,<3.0.0`, so
`requirements.txt` pins `openai` 2.54.0, not the 3.x line. Both are
dependencies of this example only, not of any traceAI package.

The contract test (see "Tests") passes on Python 3.10, 3.11, 3.12 and 3.13
with `openai` 2.54.0, `opentelemetry-sdk` and
`opentelemetry-exporter-otlp-proto-http` 1.45.0 and
`opentelemetry-instrumentation-httpx` 0.66b0, the versions uv resolved on
2026-10-05. Python 3.9 is inside openlit's range but was not tested here.

## Run

```bash
cd python/examples/openlit
pip install -r requirements.txt

export FI_API_KEY="YOUR_API_KEY"
export FI_SECRET_KEY="YOUR_SECRET_KEY"
export OTEL_EXPORTER_OTLP_ENDPOINT="https://YOUR_FI_COLLECTOR_ORIGIN"  # no path
export OTEL_RESOURCE_ATTRIBUTES="project_name=my-chatbot,project_type=observe"
export OPENAI_API_KEY="YOUR_OPENAI_KEY"  # optional: OPENAI_BASE_URL, OPENAI_MODEL

python src/app.py "What is the refund window?"
```

## The recipe

`init_tracing()` in `src/app.py` is the whole integration. It first checks
that `OTEL_EXPORTER_OTLP_ENDPOINT`, `FI_API_KEY`, `FI_SECRET_KEY` and the
project are set, and otherwise exits with a message saying what to set
(tested). Then it calls:

```python
openlit.init(
    service_name=APP_NAME,  # "openlit-recipe", exported as service.name
    otlp_endpoint=endpoint,  # OTEL_EXPORTER_OTLP_ENDPOINT
    otlp_headers={
        "x-api-key": quote(api_key, safe=""),  # FI_API_KEY
        "x-secret-key": quote(secret_key, safe=""),  # FI_SECRET_KEY
    },
    capture_message_content=False,
    disable_metrics=True,
    disable_events=True,
    pricing_json=str(NO_PRICING),  # src/no_pricing.json, an empty table
)
```

A test checks that this call has the same arguments as the one in
`src/app.py`, and that the "Run" block sets a `project_name`.

The 1.45.0 signature, quoted from the wheel's `openlit/__init__.py:185-209`:

```python
def init(
    environment="default",
    application_name="default",
    service_name="default",
    otlp_endpoint=None,
    otlp_headers=None,
    disable_batch=False,
    capture_message_content=True,
    disabled_instrumentors=None,
    disable_metrics=False,
    disable_events=False,
    pricing_json=None,
    controller_mode=None,
    collect_gpu_stats=False,
    collect_system_metrics=False,
    capture_db_parameters=False,
    max_content_length=None,
    custom_span_attributes=None,
    custom_metrics_attributes=None,
    openlit_api_key=None,
    openlit_url=None,
    *,
    guards=None,
    guard_fail_open=True,
):
```

It has no resource argument and no telemetry argument. `init()` logs and
swallows any error raised inside it (`__init__.py:487-488`), so a mistake
there shows up as a log line, not an exception.

### Endpoint

Set `otlp_endpoint` to the collector origin, with no path. OpenLIT copies
it into `OTEL_EXPORTER_OTLP_ENDPOINT` (`openlit/otel/tracing.py:72-73`) and
creates OpenTelemetry's OTLP/HTTP span exporter with no arguments
(`tracing.py:121-128`). That exporter (`opentelemetry-exporter-otlp-proto-http`
1.45.0, `_common/__init__.py:129-138`) removes one trailing `/` and always
appends `/v1/traces`. Tested results:

| `otlp_endpoint` | Request path | Reply from a fi-collector-like mux |
|---|---|---|
| `http://host:port` | `/v1/traces` | 200 |
| `http://host:port/` | `/v1/traces` | 200 |
| `http://host:port/v1/traces` | `/v1/traces/v1/traces` | 404 |
| `http://host:port/tracer` | `/tracer/v1/traces` | 200 |

fi-collector serves `/v1/traces` and `/tracer/v1/traces`
(`fi-collector/pkg/server/server.go:233-234` at future-agi `main` 4af5338).
These paths were tested against a loopback recorder that answers the way
that mux does; no deployed endpoint was called.

Read from source, not tested: with `otlp_endpoint` left out, OpenLIT uses
`OTEL_EXPORTER_OTLP_ENDPOINT` from the environment, and with neither it
prints every span to stdout (`tracing.py:121-131`). `src/app.py` refuses to
start without the variable instead (tested). `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`,
if set, is used as the full URL with nothing appended
(`_common/__init__.py:133-134`). `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` switches
OpenLIT to the gRPC exporter (`tracing.py:23-26`); this recipe does not test
gRPC.

### Auth headers

`otlp_headers` is a dict (`__init__.py:222`). OpenLIT joins it into
`name=value,name=value` and writes that to `OTEL_EXPORTER_OTLP_HEADERS`
(`tracing.py:75-83`). The exporter parses the variable with OpenTelemetry's
`parse_env_headers(..., liberal=True)` (`_common/__init__.py:141-153`),
which splits on commas and percent-decodes each value
(`opentelemetry/util/re.py:52-93`). So the recipe percent-encodes both
values. The contract test proves that they arrive as the `x-api-key` and
`x-secret-key` request headers fi-collector reads
(`fi-collector/pkg/auth/middleware.go:45-46`), using a placeholder secret
that contains `,` and `=`. Without the encoding, a comma cuts the value
short (tested).

Because OpenLIT writes the joined string to `os.environ`, the keys stay in
the process environment after `init()` and any child process the app starts
inherits them (read from source).

### Project

fi-collector rejects a batch whose resource has no `project_name` (HTTP 400,
`fi-collector/pkg/auth/stamp.go:31-45`) and creates the project on first
use. `openlit.init()` has no resource argument. It builds its resource with
`Resource.create()` (`tracing.py:62-68`), which merges the standard
`OTEL_RESOURCE_ATTRIBUTES` variable and percent-decodes its values
(`opentelemetry-sdk` 1.45.0, `resources/__init__.py:333-348`). The test sets
`project_name=...,project_type=observe` there and finds exactly these
resource attributes on every export: `project_name`, `project_type`,
`service.name` (`openlit-recipe`), `deployment.environment` (`default`),
`telemetry.sdk.name` (`openlit`), `telemetry.sdk.language`,
`telemetry.sdk.version` and `service.instance.id`. `src/app.py` refuses to
start when the variable has no `project_name` (tested).

### Metrics and events

By default OpenLIT also exports metrics and, once an endpoint is set, OTel
log records it calls events (`__init__.py:378-400`,
`otel/events.py:124-136`). Their exporters post to `/v1/metrics` and
`/v1/logs`, which fi-collector does not route, and get 404 (tested against
the recorder). The recipe passes `disable_metrics=True` and
`disable_events=True`; with both, the only request is to `/v1/traces`
(tested). With events off, `init()` still installs a global OTel logger
provider with an OTLP log exporter (`__init__.py:378-385`); nothing is
written to it, and no `/v1/logs` request was seen.

### Price download and telemetry

`init()` always loads a price table (`__init__.py:426`). Without
`pricing_json` it downloads
`https://raw.githubusercontent.com/openlit/openlit/main/assets/pricing.json`,
with a 20-second timeout, on every start (`__helpers.py:476-482`). The recipe
passes `src/no_pricing.json`, an empty table, so nothing is downloaded and
OpenLIT sends `gen_ai.usage.cost` as 0 (tested). With the argument left out,
the loopback guard refused exactly one attempt, a DNS lookup of
`raw.githubusercontent.com`, and nothing else (tested).

`init()` has no telemetry switch. The 1.45.0 wheel has no telemetry client,
and the price download is the only URL the `init()` path calls;
`cloud.openlit.io` appears only in the CLI's help text (read from source).
No recipe run in the contract test made any non-loopback connection
attempt.

## What is traced

With content off, `python src/app.py` exports two spans in one trace:

| Span | Parent | Kind | Instrumentor |
|---|---|---|---|
| `chat gpt-4o-mini` | none | CLIENT | OpenLIT's OpenAI instrumentor |
| `POST` | `chat gpt-4o-mini` | CLIENT | OpenTelemetry's httpx instrumentor, which `openlit.init()` also enables |

The model-call span is named `chat <request model>`. `init()` also enables
OpenTelemetry's `requests`, `urllib`, `urllib3` and `aiohttp` client
instrumentors when those libraries are installed (`_instrumentors.py:78-82`,
`__init__.py:141-145`), so other HTTP calls in your app become spans too
(read from source). `disabled_instrumentors` turns any of them off (not
tested).

## Key inventory

Every attribute key OpenLIT 1.45.0 put on the spans of one non-streaming
chat call against the fake, with the value the test checked. "Stored" means
fi-collector keeps the key in the span's raw attribute maps
(`attrs_string`, `attrs_number`, `attrs_bool`, or the `attributes_extra`
JSON for arrays; `fi-collector/pkg/adapter/adapter.go:62-107`) without
putting it in a column. Whether the Future AGI UI shows raw attributes was
not checked.

Model-call span (`chat gpt-4o-mini`), content off:

| Key | Value in the test | fi-collector |
|---|---|---|
| `gen_ai.operation.name` | `chat` | `observation_type` (see "What Future AGI shows") |
| `gen_ai.provider.name` | `openai` | `provider` column |
| `gen_ai.request.model` | `gpt-4o-mini` | `model` column |
| `gen_ai.response.model` | `gpt-4o-mini-2024-07-18` | stored; `model` reads the request model first |
| `gen_ai.usage.input_tokens` | `11` | `prompt_tokens` column |
| `gen_ai.usage.output_tokens` | `7` | `completion_tokens` column |
| `gen_ai.client.token.usage` | `18` | stored; not a total-tokens alias |
| `gen_ai.usage.cost` | `0` (empty price table) | stored; not a cost alias |
| `gen_ai.usage.cache_read.input_tokens` | `0` | stored |
| `gen_ai.usage.cache_creation.input_tokens` | `0` | stored |
| `gen_ai.request.temperature` | `1.0` (OpenLIT's default; the call set none) | stored |
| `gen_ai.request.top_p` | `1.0` (same) | stored |
| `gen_ai.request.frequency_penalty` | `0.0` (same) | stored |
| `gen_ai.request.presence_penalty` | `0.0` (same) | stored |
| `gen_ai.request.seed` | `0` (same) | stored |
| `gen_ai.request.user` | empty string | stored |
| `gen_ai.request.stream` | `false` | stored |
| `gen_ai.response.id` | `chatcmpl-fake` | stored |
| `gen_ai.response.finish_reasons` | `["stop"]` | stored (array) |
| `gen_ai.output.type` | `text` | stored |
| `gen_ai.sdk.version` | `2.54.0`, the `openai` package version | stored |
| `gen_ai.server.time_to_first_token` | seconds, a float | stored |
| `gen_ai.server.time_per_output_token` | `0` | stored |
| `openai.api.type` | `chat_completions` | stored |
| `openlit.agent.version_hash` | 16 hex digits | stored |
| `server.address` | `127.0.0.1` | stored |
| `server.port` | the fake's port | stored |
| `service.name` | `openlit-recipe` | stored (also on the resource) |
| `deployment.environment` | `default` | stored (also on the resource) |
| `telemetry.sdk.name` | `openlit` | stored (also on the resource) |

Added to the model-call span only with content on, OpenLIT's default:

| Key | Value in the test | fi-collector |
|---|---|---|
| `gen_ai.input.messages` | JSON string: the system and user messages | stored; not read as the span's input |
| `gen_ai.output.messages` | JSON string: the assistant message | stored; not read as the span's output |
| `gen_ai.system_instructions` | JSON string: the system prompt | stored |

HTTP span (`POST`):

| Key | Value in the test | fi-collector |
|---|---|---|
| `http.method` | `POST` | stored |
| `http.url` | the full OpenAI request URL | stored |
| `http.status_code` | `200` | stored |

Not sent (tested): `gen_ai.usage.total_tokens`, `gen_ai.system`, and every
key fi-collector reads a span kind from: `fi.span.kind`, `gen_ai.span.kind`,
`llm.request.type`, `openinference.span.kind`
(`exporter/clickhouse25exporter/converter.go:79-84`). No span events were
sent. A tool call adds `gen_ai.tool.name`, `gen_ai.tool.call.id` and
`gen_ai.tool.args`, and a failed call adds `error.type`, two `exception`
events and an error status; "Privacy" covers them because they are sent
with content off (tested). Streaming and embeddings were not exercised.

## What Future AGI shows

fi-collector promotes a few keys to columns. This table is everything this
recipe calls "displayed". It was read from fi-collector at future-agi `main`
4af5338 (`pkg/adapter/adapter.go`,
`exporter/clickhouse25exporter/converter.go`); the opt-in test (see
"Tests") re-reads the alias lists from that source and checks this table
against them and against the keys the run emitted. No fi-collector was run.

| Column | Filled from | Source |
|---|---|---|
| `model` | `gen_ai.request.model`, which wins over `gen_ai.response.model` | `adapter.go:279-284` |
| `provider` | `gen_ai.provider.name` | `adapter.go:289-295` |
| `prompt_tokens` | `gen_ai.usage.input_tokens` | `adapter.go:296-300` |
| `completion_tokens` | `gen_ai.usage.output_tokens` | `adapter.go:301-305` |
| `total_tokens` | derived as input plus output, because no total alias arrived | `adapter.go:230-235`, `:306-310` |
| `cost` | none: `gen_ai.usage.cost` is not one of `gen_ai.cost.*`, `llm.cost.*` | `adapter.go:311-313` |
| `observation_type` | `gen_ai.operation.name`: `chat` maps to `llm` | `converter.go:79-131` |
| `input` | none: only `input.value` is read | `converter.go:343` |
| `output` | none: only `output.value` is read | `converter.go:344` |

Because OpenLIT's cost key is not read, fi-collector treats the span as
having no user cost and prices it from `model` and the token columns with
its own price table, when one is configured (`converter.go:285-294`). That
pricing was not exercised here. This recipe computes no price, and the 0
OpenLIT sends with the empty table is not used.

The `POST` span carries none of these keys: its `observation_type` resolves
to `unknown` and its model and token columns stay empty (read from source).

Gaps for the shared processor work (SF-1), from this inventory:

- `gen_ai.client.token.usage` is not a total-tokens alias. The column is
  derived instead, and for this call the derived value equals it.
- `gen_ai.usage.cost` is not a cost alias.
- `gen_ai.input.messages`, `gen_ai.output.messages` and
  `gen_ai.system_instructions` are not read as the span's input or output.
- A `@openlit.trace` span (see "Privacy") has no span-kind key, so it is
  `unknown`.

## Privacy

Content capture is on unless you turn it off: `capture_message_content`
defaults to `True` (`__init__.py:192`). With it on, the model-call span
carries `gen_ai.input.messages` (system prompt and question),
`gen_ai.output.messages` (the answer) and `gen_ai.system_instructions`
(tested). The recipe passes `False`, and the test finds none of those keys,
no span events, and none of the question, answer or system-prompt text
anywhere in the export. The switch covers those messages only. The list
below is what it does not cover; the recipe filters nothing.

`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false` turns content off
even when `init()` is called with `True` (tested; `__init__.py:410-414`).
The variable cannot turn content on when the argument is `False`: OpenLIT
applies it only while the argument is still `True` (`__init__.py:292-293`,
read from source).

Not covered by the content switch:

- Tool calls. When the request passes `tools` and the model answers with
  tool calls, the model-call span carries `gen_ai.tool.name`,
  `gen_ai.tool.call.id` and `gen_ai.tool.args`, the arguments the model
  generated, verbatim (`instrumentation/openai/utils.py:1524-1551`, before
  the content check at `:1616`). Those arguments often repeat what the user
  typed: an email address, an order number. The test plants an email address
  in the fake model's tool-call arguments and finds it in `gen_ai.tool.args`
  (`tests/tool_call.py`). The Responses API sets the same keys
  (`utils.py:991-1022`; not tested).
- `user`. The `user` argument of `chat.completions.create()` is sent as
  `gen_ai.request.user` (`utils.py:1484-1487`). The test passes an email
  address and finds it there. Without the argument the key is an empty
  string.
- Errors. When the OpenAI call raises, OpenLIT records the exception on the
  model-call span (`instrumentation/openai/openai.py:186-209`,
  `utils.py:77-84`) and re-raises inside `start_as_current_span`, whose
  context manager records it again and sets the span status
  (`opentelemetry-api` 1.45.0 `trace/__init__.py:609-622`, called from
  `opentelemetry-sdk` `trace/__init__.py:1130-1136`). So a failed call
  carries two `exception` events, each with `exception.message` and
  `exception.stacktrace`, and the same text as the status description,
  `status.message`. The `openai` SDK puts the whole error response body in
  that text (`openai/_base_client.py:430-435`). Tested with a fake 500 and
  `max_retries=0` (`tests/failed_call.py`): the fake's error body appeared
  in all three places, and no prompt or answer text did. An
  OpenAI-compatible server or proxy behind `OPENAI_BASE_URL` whose error
  bodies echo the request would put prompt text there (not tested).
- `@openlit.trace` records the decorated function's arguments
  (`function.args`, `function.kwargs`) and its return value
  (`gen_ai.output.messages`) whatever the switch says
  (`__init__.py:953-965`). With content off, the test finds the question in
  `function.args` and the answer in `gen_ai.output.messages`. `src/app.py`
  does not use the decorator; `tests/traced_function.py` is the fixture.
- The `POST` span's `http.url` is the full request URL (tested).
- OpenLIT's Firecrawl instrumentor ignores the switch (see "Firecrawl").
- With traceai-openai also enabled, its span carries the prompt and the
  answer whatever OpenLIT's switch says (tested; see "traceai-openai").

If your tool arguments or `user` values can carry personal data, choose one:

- Turn OpenLIT's OpenAI instrumentor off with
  `disabled_instrumentors=["openai"]`. OpenLIT then makes no model-call span,
  so none of the tool, `user` or error keys above is recorded by it (tested
  with traceai-openai enabled; see "traceai-openai"). The httpx `POST` span
  and its `http.url` stay.
- Or accept that these keys reach Future AGI.

No Future AGI key, secret or OpenAI key appears in any exported span or
resource, or in the app's output (tested).

## Using OpenLIT with traceAI

### traceai-openai

OpenLIT wraps `Completions.create`
(`instrumentation/openai/__init__.py:185-189`) and traceai-openai wraps
`OpenAI.request` (`python/frameworks/openai/traceai_openai/__init__.py:55-59`),
so with both enabled one call is traced twice. The test runs the recipe and
then enables traceai-openai 0.1.10's `OpenAIInstrumentor` on the tracer
provider OpenLIT created. One call to the fake gave three spans in one
trace:

| Span | Parent | From |
|---|---|---|
| `chat gpt-4o-mini` | none | OpenLIT |
| `ChatCompletion` | `chat gpt-4o-mini` | traceai-openai (`gen_ai.span.kind` = `LLM`) |
| `POST` | `ChatCompletion` | OpenTelemetry httpx, enabled by OpenLIT |

Both model-call spans carry the same token counts (tested), so fi-collector
stores two LLM spans with tokens for one call (read from source) and a sum
over spans counts the call twice. OpenLIT's content switch does not reach
traceai-openai's span: with the recipe's content off, that span still
carried the system prompt in `input.value`, the question in
`gen_ai.input.messages.1.message.content` and the answer in `output.value`
(tested).

Enable one of the two. Neither package disables the other, and this recipe
does not do it for you. To keep only traceai-openai's span, pass
`disabled_instrumentors=["openai"]` to `openlit.init()`; the test then sees
only `ChatCompletion` and `POST`. To keep only OpenLIT's, do not enable
traceai-openai.

If traceAI's `register()`, or anything else, has already set a global tracer
provider, `openlit.init()` reuses it and adds no exporter, so
`otlp_endpoint` and `otlp_headers` do nothing for spans
(`tracing.py:56-59`; read from source, not tested).

### Firecrawl

OpenLIT 1.45.0's `FireCrawlInstrumentor` wraps `FirecrawlApp` in
`firecrawl.firecrawl` (`scrape_url`, `crawl_url`, `map_url`, `search`,
`extract`, `batch_scrape_urls`, `check_crawl_status`) and the matching
`AsyncFirecrawlApp` methods (`instrumentation/firecrawl/__init__.py:40-273`):
the Firecrawl v1 client. The architecture note (D-8338) confirms the same at
OpenLIT commit `897838a`. Per that note, the traceAI Firecrawl package
(TH-8325) wraps the v2 `Firecrawl` client instead. If you use both, pick
one. This recipe does not disable either and does not change the Firecrawl
package. `disabled_instrumentors=["firecrawl"]` turns OpenLIT's off
(`_instrumentors.py:50`, `__init__.py:131-133`; not tested).

OpenLIT's content switch does not reach its Firecrawl instrumentor. `init()`
passes `capture_message_content` (`__init__.py:148-156`), but the
instrumentor reads `trace_content`, which defaults to `True`
(`instrumentation/firecrawl/__init__.py:33`), and uses it to decide whether
to record scraped content (`instrumentation/firecrawl/utils.py:264`, `:431`).
Read from source; no Firecrawl call was made.

## Notes

- OpenLIT uses a `BatchSpanProcessor`, and the SDK tracer provider flushes
  it at exit, so a short script exports on exit (tested) and a server
  exports in batches.
- When fi-collector rejects an export (401 without valid keys,
  `middleware.go:20`), the exporter logs
  `Failed to export spans batch code: 401, reason: Unauthorized` to stderr
  and the app carries on and exits 0; nothing raises in your code (tested
  against a recorder that answers 401).
- No fi-collector, OpenLIT host, GitHub or OpenAI was called. Auth, project
  stamping and storage are fi-collector behaviour the harness receiver does
  not reproduce.

## Tests

`tests/test_openlit_recipe.py` runs `src/app.py` as written, in a
subprocess, with the real `openlit` 1.45.0 instrumenting the real `openai`
client. `tests/_fake_openai.py` is a loopback fake of the Chat Completions
API; the spans go to the shared harness `Receiver` (`python/tests/harness`),
which serves `/v1/traces` and `/tracer/v1/traces` on 127.0.0.1.
`tests/_guarded_run.py` blocks and logs every non-loopback connection; a
positive control (`tests/_guard_probe.py`) proves it refuses and logs IPv4,
IPv6 and DNS attempts. All keys are placeholders.

`tests/recipe_variant.py` runs `src/app.py` with one `openlit.init()`
argument changed or removed, or with traceai-openai also enabled.
`tests/traced_function.py` wraps the recipe's model call in
`@openlit.trace`. Both are fixtures, not part of the recipe.

The tests check the request path for each endpoint form, the auth headers,
the resource, the span tree, the exact key inventory and its values,
content off and on, the environment override, the metrics and events paths,
the price download, the decorator, a 401 reply, the app's start-up checks,
both instrumentors together, and that no key reaches the export.

From the repository root, Python 3.11:

```bash
env -u PYTHONPATH PYTHONPATH="python/examples/openlit:python:python/tests" \
  uv run --no-project --python 3.11 \
  --with pytest --with jsonschema --with protobuf --with opentelemetry-proto \
  --with 'openlit==1.45.0' --with 'openai==2.54.0' \
  pytest python/examples/openlit/tests -q -p no:cacheprovider \
  --noconftest -o addopts= -rfEs
```

For Python 3.13, replace `--python 3.11`. `python` is on the path and
`jsonschema` is installed only because pytest imports `python/__init__.py`,
which imports `fi_instrumentation`; the recipe needs neither.

The two traceai-openai tests skip in that environment. To run them, add
`--with 'traceai-openai==0.1.10'`. That package requires `wrapt<2` (through
`fi-instrumentation-otel` 1.1.0), so uv resolves `wrapt` 1.17.3 there and
2.5.0 without it. The whole suite passes in both environments.

One test is opt-in. It reads the alias lists from fi-collector's
`adapter.go` and `converter.go` and checks the "What Future AGI shows" table
against them and against the emitted keys. Point `FI_COLLECTOR_SRC` at the
`fi-collector` directory of a future-agi checkout (tested with `main`
4af5338):

```bash
export FI_COLLECTOR_SRC=<future-agi checkout>/fi-collector   # then run the command above
```

Without it the test is skipped. No CI job runs this example, so run it
whenever fi-collector's alias lists or this recipe change; otherwise the
table can drift unnoticed. A full run starts the app in a new process about
20 times and takes under a minute.
