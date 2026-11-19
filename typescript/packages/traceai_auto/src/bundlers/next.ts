import { TraceAIPlugin } from "./webpack";
import type { PluginOptions } from "./common";
export type { PluginOptions } from "./common";
const WRAPPED = Symbol.for("traceai.auto.next");
/** Webpack fallback. Prefer Next's instrumentation.ts register() for all bundlers. */
export function withTraceAI(nextConfig: any = {}, options: PluginOptions = {}): any {
  if (typeof nextConfig === "function") {
    return function(this: unknown, ...args: any[]) {
      const result = nextConfig.apply(this, args);
      return result?.then ? result.then((config: any) => withTraceAI(config, options)) : withTraceAI(result, options);
    };
  }
  if (!nextConfig || typeof nextConfig !== "object") throw new Error("[traceai:auto] withTraceAI expects a Next config object or config function.");
  if (nextConfig[WRAPPED]) return nextConfig;
  const original = nextConfig.webpack;
  const wrapped = { ...nextConfig, webpack(config: any, context: any) {
    const result = original ? original.call(nextConfig, config, context) : config;
    if (!result || result.then) throw new Error("[traceai:auto] nextConfig.webpack must return a webpack config synchronously.");
    if (context.isServer && context.nextRuntime !== "edge" && options.server !== false && !(result.plugins ?? []).some((p: any) => p instanceof TraceAIPlugin)) {
      result.plugins = [...(result.plugins ?? []), new TraceAIPlugin({ ...options, server: true })];
    }
    return result;
  } };
  Object.defineProperty(wrapped, WRAPPED, { value: true });
  return wrapped;
}
export default withTraceAI;
