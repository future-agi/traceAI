import { readFileSync, writeFileSync, existsSync, realpathSync, readdirSync, statSync } from "node:fs";
import { basename, dirname, resolve } from "node:path";
import { agentPath, esmBootstrap, payloadName, MARKER, PluginOptions, prepend, registration, serverMode } from "./common";
export type { PluginOptions } from "./common";
/** Inject only emitted Node entry points; dependencies and browser outputs stay untouched. */
export function traceAI(options: PluginOptions = {}): any {
  return { name: "traceai-auto", setup(build: any) {
    const settings = build.initialOptions;
    const platform = settings.platform === "node" ? true : settings.platform === "neutral" ? undefined : false;
    if (!serverMode(platform, options, "esbuild")) return;
    const key = Symbol.for("traceai.auto.esbuild");
    if (settings[key]) return; settings[key] = true;
    const file = agentPath(options);
    settings.metafile = true;
    const workingDir = settings.absWorkingDir || process.cwd();
    const real = (path: string): string => {
      try { return realpathSync(path); }
      catch { const parent = dirname(path); return parent === path ? path : resolve(real(parent), basename(path)); }
    };
    // Outputs need not exist with write:false. Resolve their existing ancestors too.
    const canonical = (file: string) => real(resolve(workingDir, file));
    const glob = (pattern: string): RegExp => {
      let source = "";
      for (let i = 0; i < pattern.length; i++) {
        if (pattern.slice(i, i + 3) === "**/") { source += "(?:.*/)?"; i += 2; }
        else if (pattern.slice(i, i + 2) === "**") { source += ".*"; i++; }
        else if (pattern[i] === "*") source += "[^/]*";
        else source += pattern[i].replace(/[\\^$+?.()|{}[\]]/g, "\\$&");
      }
      return new RegExp(`^${source}$`);
    };
    const expand = (entry: string): string[] => {
      // Bare entries may be aliases or installed packages, not filesystem paths.
      if (!entry.includes("*")) return [entry];
      const pattern = resolve(workingDir, entry);
      const prefix = pattern.slice(0, pattern.indexOf("*")), root = prefix.endsWith("/") ? prefix.slice(0, -1) : dirname(prefix);
      const matcher = glob(pattern), found: string[] = [], seen = new Set<string>();
      const maxDepth = pattern.includes("**") ? 64 : (pattern.slice(root.length + 1).match(/\//g) ?? []).length;
      let remaining = 10000;
      const walk = (dir: string, depth: number) => {
        if (depth > 64 || --remaining < 0 || seen.has(canonical(dir))) return;
        seen.add(canonical(dir));
        for (const name of readdirSync(dir)) {
          if (--remaining < 0) break;
          const path = resolve(dir, name);
          if (statSync(path).isDirectory()) { if (depth < maxDepth) walk(path, depth + 1); }
          else if (matcher.test(path)) found.push(canonical(path));
        }
        seen.delete(canonical(dir)); // Detect ancestor cycles, not sibling aliases.
      };
      walk(root, 0); return found;
    };
    const moduleOutput = (file: string): boolean => {
      if (file.endsWith(".mjs")) return true;
      if (file.endsWith(".cjs")) return false;
      for (let dir = dirname(canonical(file)); ; dir = dirname(dir)) {
        try { return JSON.parse(readFileSync(resolve(dir, "package.json"), "utf8")).type === "module"; } catch { /* Walk to package scope. */ }
        if (dirname(dir) === dir) return false;
      }
    };
    let entries = new Set<string>();
    build.onStart(async () => {
      const configured: string[] = Array.isArray(settings.entryPoints) ? settings.entryPoints.map((entry: any) => typeof entry === "string" ? entry : entry.in) : Object.values(settings.entryPoints ?? {});
      // Only globs are expanded as paths. Pass all other entries unchanged through
      // esbuild's entry-point resolver for aliases, packages, extensions and indexes.
      entries = new Set(await Promise.all(configured.flatMap(expand).map(async path => {
        const resolved = await build.resolve(path, { kind: "entry-point", resolveDir: workingDir });
        return canonical(resolved.path || path);
      })));
      if (settings.stdin) entries.add(canonical(settings.stdin.sourcefile || "<stdin>"));
    });
    build.onEnd((result: any) => {
      if (result.errors.length) return;
      for (const [name, output] of Object.entries<any>(result.metafile?.outputs ?? {})) {
        // esbuild also labels dynamic-import chunks entryPoint; these are not process entries.
        const extension = settings.outExtension?.[".js"] ?? ".js";
        if (!output.entryPoint || !(name.endsWith(extension) || /\.(?:[cm]?js)$/.test(name))) continue;
        const entry = canonical(output.entryPoint);
        if (!entries.has(entry) && !(settings.stdin && output.entryPoint === "<stdin>")) continue;
        // With no output destination esbuild calls the one output <stdout>, while
        // its metafile still names it after the entry. Never read that virtual path.
        const stdout = !settings.outfile && !settings.outdir && result.outputFiles?.find((f: any) => f.path === "<stdout>");
        const path = canonical(name), memory = stdout || result.outputFiles?.find((f: any) => canonical(f.path) === path);
        const code = memory ? memory.text : readFileSync(path, "utf8");
        if (code.includes(MARKER)) continue;
        const esm = settings.format === "esm" || (!settings.format && !settings.bundle && (result.metafile.inputs?.[output.entryPoint]?.format === "esm" || moduleOutput(name)));
        // There is no deployable sibling payload for an unnamed ESM output. Preserve
        // that output byte-for-byte; use an external preload or give it an output path.
        if (stdout && esm) continue;
        if (esm) {
          const payloadFor = (name: string) => name.endsWith(extension) ? name.slice(0, -extension.length) + ".traceai-app.mjs" : payloadName(name);
          const payload = payloadFor(path), payloadOutput = payloadFor(name), bootstrap = esmBootstrap(code, file, payload, output.exports);
          if (result.metafile.outputs[payloadOutput]) throw new Error(`[traceai:auto] Payload path already exists: ${payloadOutput}`);
          if (memory) {
            const contents = Buffer.from(code);
            result.outputFiles.push({ path: payload, contents, get text() { return Buffer.from(this.contents).toString(); } });
            memory.contents = Buffer.from(bootstrap);
          } else { writeFileSync(payload, code); writeFileSync(path, bootstrap); }
          result.metafile.outputs[payloadOutput] = { ...output, entryPoint: undefined };
          result.metafile.outputs[name] = { ...output, bytes: Buffer.byteLength(bootstrap), inputs: {}, exports: [], imports: [
            { path: file, kind: "import-statement", external: true }, { path: payloadOutput, kind: "dynamic-import" },
          ] };
          continue; // The unchanged payload keeps its original sourcemap, without a line shift.
        }
        let updated = prepend(code, registration(file, false));
        const shift = (text: string) => {
          const map = JSON.parse(text), lines = map.mappings.split(";");
          lines.splice(code.startsWith("#!") ? 1 : 0, 0, ""); map.mappings = lines.join(";");
          return JSON.stringify(map);
        };
        const mapPath = path + ".map", memoryMap = result.outputFiles?.find((f: any) => canonical(f.path) === mapPath);
        if (memoryMap) memoryMap.contents = Buffer.from(shift(memoryMap.text));
        else if (!result.outputFiles && existsSync(mapPath)) writeFileSync(mapPath, shift(readFileSync(mapPath, "utf8")));
        updated = updated.replace(/(sourceMappingURL=data:application\/json(?:;charset=utf-8)?;base64,)([^\s]+)/, (_all, prefix, data) => prefix + Buffer.from(shift(Buffer.from(data, "base64").toString())).toString("base64"));
        if (memory) memory.contents = Buffer.from(updated); else writeFileSync(path, updated);
      }
    });
  } };
}
export default traceAI;
