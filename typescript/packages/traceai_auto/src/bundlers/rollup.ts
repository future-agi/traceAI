import { basename } from "node:path";
import { agentPath, esmBootstrap, payloadName, MARKER, PluginOptions, prepend, registration, serverMode } from "./common";
export type { PluginOptions } from "./common";
/** Rollup cannot infer a platform: pass server:true only for Node server outputs. */
export function traceAI(options: PluginOptions = {}): any {
  const active = serverMode(undefined, options, "rollup");
  let file: string;
  return { name: "traceai-auto", buildStart() { if (active) file = agentPath(options); },
    renderChunk(code: string, chunk: any, output: any) {
      if (!active || !chunk.isEntry || code.includes(MARKER)) return null;
      if (!["es", "esm", "cjs", "commonjs"].includes(output.format)) throw new Error("[traceai:auto] Rollup server output must use format 'es' or 'cjs'.");
      if (["es", "esm"].includes(output.format)) return null; // Wrap the final chunk, not its static imports.
      const inserted = code.startsWith("#!") ? 1 : 0, lines: string[] = code.split("\n").map((_line, index) => index ? "AACA" : "AAAA");
      lines.splice(inserted, 0, "");
      return { code: prepend(code, registration(file, ["es", "esm"].includes(output.format))),
        map: { version: 3, names: [], sources: [chunk.fileName], sourcesContent: [code], mappings: lines.join(";") } };
    },
    generateBundle(output: any, bundle: any) {
      if (!active || !["es", "esm"].includes(output.format)) return;
      for (const chunk of Object.values<any>(bundle)) {
        if (chunk.type !== "chunk" || !chunk.isEntry || chunk.code.includes(MARKER)) continue;
        const payload = payloadName(chunk.fileName), bootstrap = esmBootstrap(chunk.code, file, payload, chunk.exports);
        if (bundle[payload]) throw new Error(`[traceai:auto] Payload path already exists: ${payload}`);
        let source = chunk.code.replace(/\n?\/\/# sourceMappingURL=[^\n]*/g, "");
        if (chunk.map && output.sourcemap) {
          const map = JSON.parse(chunk.map.toString()); map.file = basename(payload);
          if (output.sourcemap === "inline") source += `\n//# sourceMappingURL=data:application/json;charset=utf-8;base64,${Buffer.from(JSON.stringify(map)).toString("base64")}\n`;
          else {
            bundle[payload + ".map"] = { type: "asset", fileName: payload + ".map", source: JSON.stringify(map) };
            if (output.sourcemap !== "hidden") source += `\n//# sourceMappingURL=${basename(payload)}.map\n`;
          }
        }
        bundle[payload] = { type: "asset", fileName: payload, source };
        chunk.code = bootstrap; chunk.map = null;
        chunk.imports = [file]; chunk.dynamicImports = [payload];
      }
    },
  };
}
export default traceAI;
