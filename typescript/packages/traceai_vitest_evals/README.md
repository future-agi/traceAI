# @traceai/vitest-evals

Export normalized `vitest-evals` harness spans and scores as OTLP traces to Future AGI. This reporter uses the Vitest reporter API; it does not patch Vitest or the application under test.

## Install

Requires Node.js 20.19+, `vitest-evals` 0.17.x and Vitest 4.1.x or later in the 4.x series (`vitest-evals` 0.18 requires Vitest 5 and is not supported yet). Vitest 4.0.0 cannot run the `vitest-evals` 0.17.0 `describeEval` fixture (`evalTest.override is not a function`); upgrade Vitest to at least 4.1.0.

```sh
npm install -D @traceai/vitest-evals vitest-evals@0.17 vitest@4
```

The reporter only loads the OpenTelemetry packages at runtime; `vitest` and `vitest-evals` are optional peer dependencies that declare the supported versions. npm 10 can crash while installing `vitest-evals` 0.17 together with Vitest 4 (`Cannot read properties of null (reading 'edgesOut')`), with or without this package; use npm 11 or pnpm.

Set `FI_API_KEY` and `FI_SECRET_KEY` in the test process. In `vitest.config.ts`:

```ts
import { defineConfig } from 'vitest/config';
import { VitestEvalsReporter } from '@traceai/vitest-evals';

export default defineConfig({
  test: { reporters: ['default', new VitestEvalsReporter()] },
});
```

Run `npx vitest run`. Open your Observe project to inspect each evaluation's normalized harness traces. The reporter adds finite scores and token counts to the first span in each trace. Scores without a harness trace generate one evaluation span. Multiple normalized traces remain separate. The reporter sends one OTLP trace batch at run end; if the endpoint rejects the batch, the run reports an export error.

The default endpoint is `https://api.futureagi.com/tracer/v1/traces`. To use a collector, pass `new VitestEvalsReporter({ endpoint: 'http://localhost:4318/v1/traces' })`. An `exporter` option accepts an OpenTelemetry `SpanExporter` for applications already managing their own exporter; do not enable this reporter alongside a second exporter for the same harness traces.

Only operation names, coarse kinds, selected GenAI model and usage attributes, numeric scores with safe identifier names (or positional keys for other names), timing, status and trace hierarchy are exported. Raw prompts, outputs, tool arguments, arbitrary metadata and error messages are not exported. Avoid putting sensitive data in test names or operation names.
