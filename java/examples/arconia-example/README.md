# Arconia to Future AGI (recipe example)

[Arconia](https://docs.arconia.io) is a Spring Boot OpenTelemetry starter. It already exports OTLP traces, so Future AGI
needs **no `traceai-arconia` module**: you point Arconia's trace exporter at Future AGI with two headers and a
`project_name` resource attribute. This example is a plain Spring Boot 4 app that does exactly that, plus tests.

Tested with Arconia `0.31.0` (BOM, latest on 2026-10-16) and `0.30.0`, Spring Boot `4.1.1`, Java 21, OpenTelemetry SDK `1.66.0`.

| File | Purpose |
| --- | --- |
| `src/main/resources/application.yaml` | The recipe. Keys come from `FI_API_KEY` / `FI_SECRET_KEY`. |
| `src/main/java/.../ArconiaExampleApplication.java` | `/hello` and `/fail` endpoints. No Future AGI code. |
| `src/main/java/.../ErrorDetailRedaction.java` | Optional. Removes raw exception text and stack traces before export (off with `FI_EXPORT_ERROR_DETAILS=true`). |
| `src/test/java/...` | Boots the app against a stand-in OTLP collector and asserts the wire format. No keys needed. |
| `run_e2e.sh` | Tests, then a live export to a real Future AGI, then the env-var-precedence check. |

## Run

```bash
export JAVA_HOME=/path/to/jdk-21          # Java 21+ and Maven 3.9+
./run_e2e.sh                              # tests only, no keys

export FI_API_KEY=... FI_SECRET_KEY=...
export FI_BASE_URL=http://localhost:14338 # self-hosted collector; leave unset for https://api.futureagi.com
export FI_PROJECT_NAME=arconia-test
./run_e2e.sh                              # also sends 3 traces live and checks OTEL_EXPORTER_OTLP_TRACES_ENDPOINT precedence
ARCONIA_VERSION=0.30.0 ./run_e2e.sh       # another BOM version
```

## What the tests prove

- HTTP/protobuf POST to `/tracer/v1/traces`, both `X-Api-Key` and `X-Secret-Key` sent, no `Content-Encoding` (gzip off).
- Resource carries `project_name` and `project_type=observe`; one request is one trace with one root server span.
- `/fail` exports an ERROR span; context close flushes (the batch delay is 5 s, the test closes sooner).
- `arconia.otel.traces.enabled=false` and `arconia.otel.traces.exporter.type=none` export nothing.
- Arconia's default compression is gzip. The recipe sets `compression: none` so it also works with self-hosted collectors older than v1.56.0; Future AGI Cloud and self-hosted v1.56.0 and later accept gzip, so you can drop the setting there.
- The documented protocol value `http-protobuf` silently disables the exporter on 0.30.0 and 0.31.0; leave `protocol` unset.
- With defaults, query strings, exception messages and stack traces are not on the wire; no host name, user name or local path either.
