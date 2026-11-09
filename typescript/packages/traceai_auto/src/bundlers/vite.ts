import { traceAI as rollupPlugin } from "./rollup";
import type { PluginOptions } from "./common";
export type { PluginOptions } from "./common";
/** Vite client builds and edge/webworker SSR builds are intentionally untouched. */
export function traceAI(options: PluginOptions = {}): any {
  let config: any;
  const delegate = (environment: any) => {
    const server = Boolean(config?.build?.ssr) || environment?.config?.consumer === "server";
    const edge = (environment?.config?.ssr?.target ?? config?.ssr?.target) === "webworker";
    if (!server || edge || options.server === false) return;
    const plugin = rollupPlugin({ ...options, server: true }); plugin.buildStart(); return plugin;
  };
  return { name: "traceai-auto", apply: "build", enforce: "post",
    configResolved(value: any) { config = value; },
    renderChunk(this: any, code: string, chunk: any, output: any) {
      return delegate(this.environment)?.renderChunk(code, chunk, output) ?? null;
    },
    generateBundle(this: any, output: any, bundle: any) { delegate(this.environment)?.generateBundle(output, bundle); },
  };
}
export default traceAI;
