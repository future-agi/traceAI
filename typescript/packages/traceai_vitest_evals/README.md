# @traceai/vitest-evals

Export normalized `vitest-evals` harness spans and scores as OTLP traces to Future AGI. This reporter uses the Vitest reporter API; it does not patch Vitest or the application under test.

## Install

Requires Node.js 20.19+, `vitest-evals` 0.17.x and Vitest 4.1.x or later in the 4.x series (`vitest-evals` 0.18 requires Vitest 5 and is not supported yet). Vitest 4.0.0 cannot run the `vitest-evals` 0.17.0 `describeEval` fixture (`evalTest.override is not a function`); upgrade Vitest to at least 4.1.0.

```sh
npm install -D @traceai/vitest-evals vitest-evals@0.17 vitest@4
```

The reporter only loads the OpenTelemetry packages at runtime; `vitest` and `vitest-evals` are optional peer dependencies that declare the supported versions. npm 10 can crash while installing `vitest-evals` 0.17 together with Vitest 4 (`Cannot read properties of null (reading 'edgesOut')`), with or without this package; use npm 11 or pnpm.

Set `FI_API_KEY` and `FI_SECRET_KEY` in the test process, and `FI_PROJECT_NAME` for the Observe project the eval traces go to (default `vitest-evals`; or pass `new VitestEvalsReporter({ projectName: '...' })`). In `vitest.config.ts`:

```ts
import { defineConfig } from 'vitest/config';
import { VitestEvalsReporter } from '@traceai/vitest-evals';

export default defineConfig({
  test: { reporters: ['default', new VitestEvalsReporter()] },
});
```

Run `npx vitest run`. Open your Observe project to inspect each evaluation's normalized harness traces. Each trace's root span is named after its test, using Vitest's full test name with the enclosing `describeEval` or `describe` names (for example `capital answers > answers the capital of France`), so tests that share one harness can be told apart. When the harness named that root span differently, its own name is kept in the `vitest.evals.original_name` attribute; other spans keep their names. The reporter adds finite scores and token counts to the first span of each test's first trace. Scores without a harness trace generate one evaluation span, also named after the test. Multiple normalized traces remain separate; when one test produces several, their roots are numbered, such as `(1/2)` and `(2/2)`. The reporter sends one OTLP trace batch at run end; if the endpoint rejects the batch, the run reports an export error.

The default endpoint is `https://api.futureagi.com/tracer/v1/traces`. To send to another Future AGI deployment, set `FI_BASE_URL` to its base URL, as for the other traceAI packages: the reporter appends `/tracer/v1/traces` (for example `FI_BASE_URL=http://localhost:8000` sends to `http://localhost:8000/tracer/v1/traces`). To send to an OpenTelemetry collector or any other OTLP/HTTP endpoint, pass its full URL, which is used as given and takes precedence over `FI_BASE_URL`: `new VitestEvalsReporter({ endpoint: 'http://localhost:4318/v1/traces' })`. An `exporter` option accepts an OpenTelemetry `SpanExporter` for applications already managing their own exporter; do not enable this reporter alongside a second exporter for the same harness traces.

Only test names, operation names, coarse kinds, selected GenAI model and usage attributes, numeric scores with safe identifier names (or positional keys for other names), timing, status and trace hierarchy are exported. Raw prompts, outputs, tool arguments, arbitrary metadata and error messages are not exported. Avoid putting sensitive data in test, describe or operation names.
