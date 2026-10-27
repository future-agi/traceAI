# Quarkus LangChain4j to Future AGI

A Quarkus command-line app with one `@RegisterAiService` and one `@Tool`. Quarkus LangChain4j already creates the
spans; `quarkus-opentelemetry` exports them; `application.properties` points the exporter at Future AGI. There is no
traceAI module here and no `traceai-langchain4j` (that one wraps plain LangChain4j, not Quarkus' build-time AI
services).

Docs page: `docs/integrations/traceai/java/quarkus-langchain4j` in future-agi/docs.

## Tested

| | |
| --- | --- |
| `io.quarkiverse.langchain4j:quarkus-langchain4j-bom` | **1.14.2** (latest stable on Maven Central, 2026-10-15) with Quarkus platform **3.33.3.1** (the version 1.14.2 is built against), OpenTelemetry SDK 1.57.0 |
| Also run | 1.8.2 + Quarkus 3.27.2 (the PRD pin), live export only, see "Older versions" |
| JDK / Maven | OpenJDK 21.0.12, Maven 3.10.0 |
| Model | `quarkus-langchain4j-openai` against an OpenAI-compatible endpoint (tests: a scripted local server) |
| Mode | JVM. Native image not built or tested. |

## Files

| File | What |
| --- | --- |
| `src/main/resources/application.properties` | The recipe: exporter, headers, resource, privacy defaults |
| `Assistant.java`, `WeatherTools.java`, `AskCommand.java` | The app: one AI service, one tool, a command-mode `main` |
| `FutureAgiTracingDefaults.java` | Application code to copy: `ERROR` status on failed model calls and AI service calls, no error text unless opted in, removes `host.name` |
| `ToolErrorStatus.java` | Application code to copy: `ERROR` status on a failed tool span (needs quarkus-langchain4j 1.12.0+) |
| `src/test/...` | 14 tests against a scripted OpenAI-compatible server and a stand-in OTLP collector |
| `run_e2e.sh` | Tests, negative check, live run against a real model and a real Future AGI |

## Run

```bash
export JAVA_HOME=<jdk 17+>   # 21 tested
./run_e2e.sh                 # tests + "no quarkus-opentelemetry exports nothing", no keys needed

# live: a real OpenAI-compatible model and a real Future AGI
FI_API_KEY=... FI_SECRET_KEY=... FI_BASE_URL=https://api.futureagi.com FI_PROJECT_NAME=quarkus-demo \
OPENAI_API_KEY=... OPENAI_BASE_URL=https://api.openai.com/v1/ OPENAI_MODEL=gpt-4o-mini ./run_e2e.sh

# the app alone
mvn package -DskipTests
java -jar target/quarkus-app/quarkus-run.jar "What is the weather in Paris?"

# the older pair
QLC4J_VERSION=1.8.2 QUARKUS_VERSION=3.27.2 ./run_e2e.sh
```

## What one question exports

```
langchain4j.aiservices.Assistant.chat            root (AI service span)
├── completion <model>                           gen_ai.operation.name=chat, gen_ai.provider.name=open_ai,
│   └── POST /chat/completions                   gen_ai.request/response.model, gen_ai.usage.input/output_tokens
├── langchain4j.tools.currentWeather             gen_ai.operation.name=execute_tool, gen_ai.tool.name, gen_ai.tool.call.id
└── completion <model>
    └── POST /chat/completions                   Quarkus REST client span: method, url.full, status code
```

In Future AGI the model spans are type `llm` with model and tokens, the tool span is type `tool`, the AI service and
REST client spans are `unknown`. Resource: `project_name`, `project_type=observe`, `service.name`, `service.version`,
`telemetry.sdk.*`, `webengine.*`.

## Privacy defaults and opt-ins

| What | Default | Opt in |
| --- | --- | --- |
| Prompt, completion, tool arguments, tool result | off (upstream default, written out) | `quarkus.langchain4j.tracing.include-prompt`, `-completion`, `-tool-arguments`, `-tool-result` = `true` (or `QUARKUS_LANGCHAIN4J_TRACING_INCLUDE_PROMPT=true` ...). Stored as span attributes `gen_ai.prompt`, `gen_ai.completion`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`; Future AGI's Input/Output columns stay empty. |
| Error message in the span status | off | `FI_EXPORT_ERROR_DETAILS=true` (`fi.export-error-details`) |
| Exception events (`exception.message`, full `exception.stacktrace`) | off: `quarkus.otel.span.event.count.limit=0` | raise the limit. Future AGI stores only the event name and time. |
| `host.name` resource attribute | removed by `FutureAgiTracingDefaults.DropHostName` | none |

## Upstream behaviour the recipe works around (asserted in `UpstreamDefaultsTest`)

- Quarkus appends `/v1/traces` to `quarkus.otel.exporter.otlp.traces.endpoint`. Set it to `{base}/tracer`; a value
  ending in `/tracer/v1/traces` posts to `/tracer/v1/traces/v1/traces`.
- Quarkus always adds `host.name` to the resource, after `quarkus.otel.resource.attributes` and after the
  `disabled-keys` filter, so configuration cannot remove it.
- Quarkus LangChain4j calls `recordException` on failures but never sets the span status, so failures arrive as
  `UNSET` with the exception message and stack trace in an event.
- `quarkus.otel.span.event.count.limit=0` needs OpenTelemetry SDK 1.51+. Quarkus 3.27.2 (SDK 1.44.1) refuses to start
  with it.

## Older versions

1.8.2 (the PRD pin) was run live on Quarkus 3.27.2 with `-Dpre112` (leaves `ToolErrorStatus` out, skips the tests):
the trace shape is the same, but the model spans carry no `gen_ai.operation.name` and use
`gen_ai.usage.prompt_tokens`/`completion_tokens`, so Future AGI shows them as `unknown` without tokens. The source shows
the current names from quarkus-langchain4j 1.11.0 and `ToolSpanContributor` from 1.12.0.
