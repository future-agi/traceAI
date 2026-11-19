# Frameworks and deployment

[Back to quick start](../README.md) · [Configuration](CONFIGURATION.md) · [Architecture](ARCHITECTURE.md)

All Node recipes assume `@traceai/auto` is installed in your app and the FI keys/project are configured before startup. Set hide flags before initialization if prompts, responses or error details must not leave the process.

## ESM and CommonJS

Prefer the package preload shown in the quick start. It executes before application imports and registers both CJS and supported ESM hooks. A top-level `init()` next to static SDK imports is not equivalent: ESM can load those dependencies before the call runs. Put custom initialization in a separate `--import` file instead.

The package requires Node **20.6+**. Preload each child process/worker isolate separately. Keep the application working directory consistent so optional target libraries and instrumentors resolve correctly.

## Next.js

Use Next's `instrumentation.ts` at the project root (or inside `src` when that is where the app lives). Guard the runtime so the Node agent never enters an edge path:

```ts
export async function register() {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    await import("@traceai/auto/register");
  }
}
```

For Next versions supporting `serverExternalPackages`, merge these entries into `next.config.mjs`. Keep the SDKs you need to hook external too; this example uses OpenAI:

```js
export default {
  serverExternalPackages: ["@traceai/auto", "openai"],
};
```

Set FI variables on the **server**, never as `NEXT_PUBLIC_*` variables. Trigger a Node-runtime route that calls your model. Do not add this preload to browser code or assume it instruments edge handlers.

`withTraceAI(config)` from `@traceai/auto/next` is an optional **webpack-only** adapter. It preserves an existing webpack callback and skips client/edge configs, but does not cover Turbopack. Prefer the guarded register hook rather than combining both approaches.

## Node server bundlers

These opt-in plugins inject the packaged agent before Node **executable server entries**. They do not instrument browser or edge bundles. Keep AI SDKs external to preserve dedicated library import hooks; global-fetch wire capture can still observe recognized calls when application code is bundled.

Use only the recipe for your build tool. Each example takes the quick-start `app.mjs` and builds a server executable; install the selected bundler as your normal build dependency.

### esbuild

Save as `build.mjs` and run `node build.mjs`:

```js
import { build } from "esbuild";
import { traceAI } from "@traceai/auto/esbuild";

await build({
  entryPoints: ["app.mjs"],
  outfile: "dist/server.mjs",
  bundle: true,
  platform: "node",
  format: "esm",
  packages: "external",
  plugins: [traceAI()],
});
```

### Vite Node SSR

Save as `vite.config.mjs`. The plugin applies to production **Node SSR** builds, not development, client builds or `webworker` SSR:

```js
import { defineConfig } from "vite";
import { traceAI } from "@traceai/auto/vite";

export default defineConfig({
  plugins: [traceAI()],
  build: {
    ssr: "app.mjs",
    outDir: "dist",
    rollupOptions: { output: { entryFileNames: "server.mjs" } },
  },
  ssr: { target: "node", external: ["openai"] },
});
```

### Rollup

Save as `rollup.config.mjs`. Rollup cannot infer the server platform; `server: true` is required:

```js
import { traceAI } from "@traceai/auto/rollup";

export default {
  input: "app.mjs",
  external: ["openai"],
  output: { file: "dist/server.mjs", format: "es" },
  plugins: [traceAI({ server: true })],
};
```

### webpack 5

Save as `webpack.config.mjs`. This recipe emits a CommonJS server executable:

```js
import { resolve } from "node:path";
import { TraceAIPlugin } from "@traceai/auto/webpack";

export default {
  mode: "production",
  target: "node",
  entry: "./app.mjs",
  experiments: { topLevelAwait: true },
  externals: { openai: "commonjs openai" },
  output: { path: resolve("dist"), filename: "server.cjs" },
  plugins: [new TraceAIPlugin()],
};
```

### Deploy the build, not just its entry file

ESM output includes a small bootstrap at the original entry path and a sibling `*.traceai-app.mjs` payload. Ship **both**, plus generated chunks and sourcemaps. Run the built entry normally; the plugin already injects tracing.

The injected preload uses an **absolute filesystem path**. Either retain that path in your deployment or pass `agentPath` pointing to the deployment's `preload.cjs` (the file must also exist when the build resolves it). Copy the complete `dist/agent/` directory with its `runtime.cjs`, `hook.mjs`, `hook-register.cjs` and `.LEGAL.txt` files, plus `dist/THIRD-PARTY-NOTICES` and `dist/third-party-manifest.json` alongside the relocated payload. Do not copy only `preload.cjs`.

| Plugin option | Default | Meaning |
| --- | --- | --- |
| `agentPath` | Installed package's built preload | Absolute or resolvable path to a complete deployed agent payload |
| `server` | Tool-specific detection | Rollup requires `true`; `false` disables injection. Browser/edge detection still prevents injection |

ESM library/handler entries with exports are rejected by the executable bootstrap; use a runtime preload or a framework register hook instead. `import.meta.url` in the running ESM app identifies the sibling payload, not the bootstrap. Unnamed in-memory esbuild ESM output cannot get a deployable sibling; use an output path or external preload.

## AWS Lambda and short-lived processes

Do not rely on process exit in Lambda: the runtime can freeze before a batch exports. Initialize once outside the handler and flush completed spans at each invocation boundary. The fi-core wrapper handles a bounded invocation flush without shutting down a warm provider.

Install `@traceai/fi-core` directly when importing it in your own code. Save this as a Node ESM handler (for example `index.mjs`); initialize before dynamically importing AI SDKs:

```js
import { init } from "@traceai/auto";
import { traceLambdaHandler } from "@traceai/fi-core";

const provider = init();
const { default: OpenAI } = await import("openai");
const client = new OpenAI();

export const handler = traceLambdaHandler(async () => {
  const response = await client.chat.completions.create({
    model: process.env.OPENAI_MODEL,
    messages: [{ role: "user", content: "Say hello in one word." }],
  });
  return { statusCode: 200, body: response.choices[0].message.content };
}, { provider, maxFlushWaitMillis: 2000, remainingTimeReserveMillis: 250 });
```

Use this wrapper **instead of** an ADOT/upstream Lambda invocation wrapper, not on top of one. The invocation span is metadata-only; child model calls use your configured content policy. Do not call `shutdown()` after each warm invocation. Callback and response-streaming handlers have separate constraints in the [fi-core Lambda reference](../../fi-core/README.md#aws-lambda-invocation-lifecycle).

In a short-lived ordinary Node program, finish consuming responses, then `await forceFlush()` or `await shutdown()` from `@traceai/auto` before an explicit exit. A five-second bound prevents indefinite shutdown waits but cannot guarantee collector acceptance, cancel a stuck exporter or survive SIGKILL. For an application-owned provider, follow that provider's lifecycle too.

## Edge and browsers

Do **not** preload `@traceai/auto/register` in browsers, Cloudflare Workers, Deno isolates or Next edge routes. This package's zero-code agent is Node-only.

`@traceai/auto/edge` is a low-level, Node-free fetch observer for integration authors. It exports `patchFetch(maxBodyBytes, emit)`, returning a removable hook with pending response-decoding work. It does **not** configure an exporter, read FI keys, apply fi-core `TraceConfig`, propagate OTel context or supply the Node suppression/deduplication policy. It is not an end-to-end edge setup recipe; use a maintained native OTel integration for your runtime rather than copying a custom exporter into your app.
