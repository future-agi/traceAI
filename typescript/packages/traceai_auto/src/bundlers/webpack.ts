import { agentPath, esmBootstrap, payloadName, MARKER, PluginOptions, prepend, registration, serverMode } from "./common";
export type { PluginOptions } from "./common";
const INSTALLED = Symbol.for("traceai.auto.webpack");
export class TraceAIPlugin {
  constructor(private options: PluginOptions = {}) {}
  apply(compiler: any): void {
    const targets: string[] = Array.isArray(compiler.options.target) ? compiler.options.target : [compiler.options.target ?? "web"];
    const node = targets.some(t => typeof t === "string" && /^(?:async-)?node(?:\d|$)/.test(t));
    const browser = targets.some(t => typeof t === "string" && /^(?:web|webworker|electron-renderer)$/.test(t));
    if (!serverMode(browser ? false : node ? true : undefined, this.options, "webpack")) return;
    if (compiler[INSTALLED]) return; compiler[INSTALLED] = true;
    if (!compiler.webpack?.sources) throw new Error("[traceai:auto] TraceAIPlugin requires webpack 5.");
    const file = agentPath(this.options), esm = Boolean(compiler.options.output?.module);
    compiler.hooks.thisCompilation.tap("traceai-auto", (compilation: any) => {
      compilation.hooks.processAssets.tap({ name: "traceai-auto", stage: esm ? compiler.webpack.Compilation.PROCESS_ASSETS_STAGE_SUMMARIZE : compiler.webpack.Compilation.PROCESS_ASSETS_STAGE_ADDITIONS }, () => {
        for (const entry of compilation.entrypoints.values()) for (const name of entry.getEntrypointChunk().files) {
          if (!/\.[cm]?js$/.test(name)) continue;
          const asset = compilation.getAsset(name), code = asset.source.source().toString();
          if (code.includes(MARKER)) continue;
          if (!esm) {
            const end = code.startsWith("#!") ? code.indexOf("\n") : -1;
            const offset = code.startsWith("#!") ? end < 0 ? code.length : end + 1 : 0;
            const updated = prepend(code, registration(file, false));
            const injected = updated.slice(offset, offset + updated.length - code.length);
            const source = new compiler.webpack.sources.ReplaceSource(asset.source); source.insert(offset, injected);
            compilation.updateAsset(name, source); continue;
          }
          const payload = payloadName(name);
          if (compilation.getAsset(payload)) throw new Error(`[traceai:auto] Payload path already exists: ${payload}`);
          const bootstrap = esmBootstrap(code, file, payload, compiler.options.output.library ? ["library"] : []);
          compilation.emitAsset(payload, asset.source);
          compilation.updateAsset(name, new compiler.webpack.sources.RawSource(bootstrap));
        }
      });
    });
  }
}
export function traceAI(options: PluginOptions = {}): TraceAIPlugin { return new TraceAIPlugin(options); }
export default TraceAIPlugin;
