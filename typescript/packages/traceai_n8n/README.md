# @traceai/n8n

OpenTelemetry instrumentation for the self-hosted n8n execution engine. It adds a
workflow span around each engine run and child spans around node execution
attempts, including retries. Existing provider instrumentors can add LLM spans
under the active node span.

## Compatibility

- Node.js 22.16 or newer.
- `n8n-core >=2.16.1 <3`; integration tests use `n8n-core@2.16.1` and its
  `n8n-workflow@2.16.0` dependency. Earlier engine versions are not supported.
- Automatic instrumentation uses the CommonJS module loader. Both CommonJS and
  ESM package entry points are available, but native ESM loading of n8n itself is
  not covered by the automatic loader hook.

This package is a source contribution and has not been released to npm yet. The
commands below build it from this repository:

```sh
cd typescript
pnpm --filter '@traceai/n8n...' install --frozen-lockfile --ignore-scripts
pnpm run build:deps
pnpm --filter @traceai/n8n build
pnpm --filter @traceai/n8n pack --pack-destination /path/to/artifacts
```

Install the resulting tarball in the same application installation that resolves
`n8n-core`. The workspace dependencies must also be available there; when building
them from source, pack and install `@traceai/fi-semantic-conventions` and
`@traceai/fi-core` first. On Windows, use Git Bash for the existing dependency
build scripts.

## Self-hosted setup

Load instrumentation **before** n8n loads its engine, in every process that
executes workflows. For example, create `tracing.cjs` alongside your n8n
installation. It requires `@traceai/fi-core`, `@opentelemetry/api`,
`@opentelemetry/context-async-hooks` and `@opentelemetry/instrumentation` in addition
to this package.

```js
const { context } = require("@opentelemetry/api");
const {
  AsyncLocalStorageContextManager,
} = require("@opentelemetry/context-async-hooks");
const { registerInstrumentations } = require("@opentelemetry/instrumentation");
const { register, ProjectType } = require("@traceai/fi-core");
const { N8nInstrumentation } = require("@traceai/n8n");

// If the application already has an OTel context manager/provider, reuse them.
context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
const tracerProvider = register({
  projectName: "n8n-workflows",
  projectType: ProjectType.OBSERVE,
});
const instrumentation = new N8nInstrumentation();
registerInstrumentations({
  tracerProvider,
  instrumentations: [instrumentation],
});

// Flush on normal process exit; integrate shutdown with the host's lifecycle
// when it has its own shutdown hooks.
process.once("beforeExit", () => tracerProvider.shutdown());
```

Configure `FI_API_KEY` and `FI_SECRET_KEY` in the host environment, then preload
the file when starting the n8n process, for example on Linux:

```sh
NODE_OPTIONS="--require=/absolute/path/to/tracing.cjs" n8n start
```

Preserve any existing `NODE_OPTIONS` when adding the preload. In queue mode,
configure each worker as well as any process that runs manual executions. This
integration creates traces inside an engine process; it does **not** propagate
context through the queue or connect separate processes into one trace.

If the engine was already loaded, use the public manual entry point instead:

```js
instrumentation.manuallyInstrument(require("n8n-core"));
```

Call `instrumentation.disable()` to restore patched engine methods. Runs already
being observed still finish their open spans. OpenTelemetry tracing suppression
is respected.

## Captured data

| Span                  | Attributes                                                                                                                               |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Workflow (`CHAIN`)    | `n8n.workflow.id`, `n8n.workflow.name`, `n8n.execution.id` when available, `n8n.execution.mode`, `n8n.execution.status`                  |
| Node attempt (`TOOL`) | `n8n.node.id`, `n8n.node.name`, `n8n.node.type`, `n8n.node.type_version`, `n8n.node.run_index`, `n8n.node.status`, execution ID and mode |

Span duration records elapsed execution time. Workflow status comes from n8n's
final run result, including errors returned as resolved promises. Rejected node
attempts are marked as errors even if n8n subsequently retries or continues.
Cancellation ends open spans once and preserves n8n's cancellable promise.

The integration does not capture workflow items, node parameters, credentials,
input/output payloads, exception messages or stack traces. Exceptions retain only
their type. Names and identifiers are exported, so avoid putting secrets in them.
Provider instrumentors have their own payload settings; this package does not
change those settings.

This first integration observes engine runs and calls to `runNode`. Pinned nodes
that bypass that method have no node span. Nodes that handle errors internally
and return them as ordinary item data are not classified by inspecting that data.
Waiting and resumed runs are separate spans; there is no persisted trace context
across a restart. It does not install an n8n community node, configure n8n Cloud,
or replace provider-specific LLM instrumentation.

## Development

From `typescript/`, after building dependencies:

```sh
pnpm --filter @traceai/n8n build
pnpm --filter @traceai/n8n typecheck
pnpm --filter @traceai/n8n test:coverage
pnpm --filter @traceai/n8n lint
```

The tests use the actual n8n engine and custom local nodes, with an in-memory
OpenTelemetry exporter. They require no credentials, external services or mocked
n8n modules. A fresh Node process also tests automatic module patching. The
`lint` script checks formatting with Prettier.
