import { createRequire } from "node:module";
import * as nodeModule from "node:module";
import catalog from "./integrations.json";
declare const TRACEAI_AGENT_BUNDLE: boolean;
import { dirname, join } from "node:path";
import { satisfies } from "semver";
import { pathToFileURL } from "node:url";
import { readFileSync } from "node:fs";
import type { TracerProvider } from "@opentelemetry/api";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import type { SpanProcessor } from "@opentelemetry/sdk-trace-base";
import { Config, enabled, Options } from "./config";

export type Status = "instrumented" | "pending" | "not_installed" | "missing_instrumentor" | "disabled" | "manual" | "failed";
export interface Integration { name: string; package: string; targets: string[]; modules: string[]; className?: string; manual?: string; enable?: string }
export interface IntegrationStatus { name: string; package?: string; targets?: string[]; status: Status; reason?: string; media?: "unqualified" }
export const integrations: Integration[] = catalog as Integration[];
export function appRequire() { return createRequire(join(process.cwd(), "__traceai_auto__.cjs")); }
function resolvable(name: string): boolean {
  try { appRequire().resolve(name); return true; } catch {
    try { appRequire().resolve(`${name}/package.json`); return true; } catch { return false; }
  }
}
export function inventory(config: Config): IntegrationStatus[] {
  return integrations.map(i => ({ name: i.name, package: i.package, targets: i.targets, media: "unqualified",
    status: !enabled(i.name, config) ? "disabled" : ![...i.targets, ...i.modules].some(resolvable) ? "not_installed" : i.manual && !i.enable ? "manual" : !resolvable(i.package) && i.name !== "openai" ? "missing_instrumentor" : "pending",
    ...(i.manual ? { reason: i.manual } : {}) }));
}
function moduleVersion(name: string): string | undefined {
  const rootName = name.startsWith("@") ? name.split("/").slice(0, 2).join("/") : name.split("/")[0];
  let path: string;
  try { path = appRequire().resolve(name); } catch {
    try { path = appRequire().resolve(`${rootName}/package.json`); } catch { return undefined; }
  }
  for (let dir = dirname(path); ; dir = dirname(dir)) {
    try { const pkg = JSON.parse(readFileSync(join(dir, "package.json"), "utf8")); if (pkg.name === rootName) return pkg.version; } catch { /* Walk to package root. */ }
    if (dirname(dir) === dir) return undefined;
  }
}
let loaderInstalled = false;
const bridgedRegistries = new WeakSet<object>();
function registryFor(hook: string): any {
  const middle = createRequire(hook).resolve("import-in-the-middle");
  return require(join(dirname(middle), "lib/register.js"));
}
export function installLoader(targets: string[], packages: string[] = []): void {
  if (loaderInstalled || !targets.length) return;
  if (typeof nodeModule.register !== "function") throw new Error("module.register is unavailable");
  const bundled = typeof TRACEAI_AGENT_BUNDLE !== "undefined" && TRACEAI_AGENT_BUNDLE;
  const hook = bundled ? join(__dirname, "hook.mjs") : require.resolve("@opentelemetry/instrumentation/hook.mjs");
  const source = bundled ? require(join(__dirname, "hook-register.cjs")) : registryFor(hook);
  // IITM registries are module-local, but its loader may only initialize once.
  // Forward namespaces AND specifiers to each installed runtime, so embedded and
  // app-installed instrumentors share a single filtered loader without double wrapping.
  for (const packageName of packages) {
    let target: any;
    try { target = registryFor(createRequire(appRequire().resolve(packageName)).resolve("@opentelemetry/instrumentation/hook.mjs")); }
    catch { continue; } // Embedded packages already use source.
    if (target === source || bridgedRegistries.has(target)) continue;
    if (!Array.isArray(target.importHooks) || !Array.isArray(target.toHook) || !target.specifiers?.set) throw new Error("Unsupported ESM hook registry");
    const forward = (name: string, namespace: any) => {
      target.specifiers.set(name, source.specifiers.get(name));
      target.toHook.push([name, namespace]);
      for (const callback of [...target.importHooks]) callback(name, namespace);
    };
    source.importHooks.push(forward);
    for (const [name, namespace] of source.toHook) forward(name, namespace);
    bridgedRegistries.add(target);
  }
  // Init is once per process. The loader's target set is immutable; no rollback,
  // retry messages, shared-memory filter or shutdown rewiring is necessary.
  nodeModule.register(pathToFileURL(hook), { parentURL: pathToFileURL(__filename).href, data: { include: [...new Set(targets)] } });
  loaderInstalled = true;
}
/** Snapshot data descriptors at the module/class boundary, including non-shimmer patches. */
function patchSnapshot(root: object): () => boolean {
  const seen = new Set<object>(), snapshots: [object, PropertyKey, unknown][] = [];
  const visit = (value: any, depth: number) => {
    if (!value || !["object", "function"].includes(typeof value) || seen.has(value) || seen.size >= 128) return;
    seen.add(value);
    for (const key of Reflect.ownKeys(value).slice(0, 256)) {
      const descriptor = Object.getOwnPropertyDescriptor(value, key);
      if (!descriptor || !("value" in descriptor)) continue;
      snapshots.push([value, key, descriptor.value]);
      if (depth < 2 && key !== "constructor") visit(descriptor.value, depth + 1);
    }
    // Track markers added by processor-based instrumentors as well as replacements.
    snapshots.push([value, "_fiPatched", Object.getOwnPropertyDescriptor(value, "_fiPatched")?.value]);
  };
  visit(root, 0);
  return () => snapshots.some(([value, key, before]) => Object.getOwnPropertyDescriptor(value, key)?.value !== before);
}
export function enableLibraries(config: Config, options: Options, provider: TracerProvider, statuses: IntegrationStatus[], warn: (key: string, message: string) => void, spanProcessor?: SpanProcessor): () => void {
  const removers: (() => void)[] = [];
  // Register before loading any instrumentation package: its own imports can load targets.
  let esm = true;
  try {
    const pending = integrations.filter(i => statuses.find(s => s.name === i.name)?.status === "pending");
    const http = enabled("http", config) && resolvable("@opentelemetry/instrumentation-http");
    installLoader([...pending.flatMap(i => [...i.targets, ...i.modules]), ...(http ? ["http", "https", "node:http", "node:https"] : [])], [...pending.map(i => i.package), ...(http ? ["@opentelemetry/instrumentation-http"] : [])]);
  } catch { esm = false; warn("esm", "ESM loader could not be installed; CommonJS and fetch capture remain available."); }
  for (const entry of integrations) {
    const status = statuses.find(s => s.name === entry.name)!;
    if (status.status !== "pending") continue;
    let instance: any;
    try {
      // The launcher includes OpenAI so its default node-fetch transport works even in
      // an app containing only openai. A project-installed instrumentor always wins.
      // Keep this require inside the enabled branch: disabled integrations register no hooks.
      const path = entry.enable && resolvable(`${entry.package}/auto`) ? `${entry.package}/auto` : entry.package;
      const exports = resolvable(path) ? appRequire()(path) : entry.name === "openai" ? require("@traceai/openai") : appRequire()(path);
      if (entry.enable) {
        const enable = exports.enable ?? exports[entry.enable];
        if (typeof enable !== "function") { status.status = "manual"; status.reason = "Installed instrumentor has no zero-argument enable entry; wire capture remains available"; continue; }
        // Enable entries receive the selected neutral destination explicitly.
        // Never synthesize FI credentials to unlock a framework integration.
        const result = enable({ tracerProvider: provider, spanProcessor, traceConfig: options.traceConfig, attachmentManager: options.attachmentManager });
        if (result && typeof result.setTracerProvider === "function") result.setTracerProvider(provider);
        status.status = result === false || result === undefined ? "manual" : result?.status ?? "instrumented";
        status.reason = result?.reason;
        if (result && typeof result.disable === "function") removers.push(() => result.disable());
        continue;
      }
      const Instrumentor = exports[entry.className!];
      if (typeof Instrumentor !== "function") throw new Error("Missing exported Instrumentation class");
      instance = new Instrumentor({ instrumentationConfig: { enabled: false }, traceConfig: options.traceConfig, tracerProvider: provider, exclusiveProcessor: false, attachmentManager: options.attachmentManager });
      const known = (instance._modules ?? []).map((definition: any) => ({ definition, version: moduleVersion(definition.name) })).filter((item: any) => item.version);
      if (known.length && known.every(({ definition, version }: any) => !definition.supportedVersions.some((range: string) => satisfies(version, range, { includePrerelease: definition.includePrerelease })))) {
        instance.disable(); status.status = "failed"; status.reason = "Installed target version is outside the instrumentor's supported range";
        warn(entry.name, `Integration ${entry.name} does not support the installed library version; wire capture remains available.`);
        continue;
      }
      // Track wrappers, not registration callbacks: a duplicate instrumentor can
      // register hooks while another instance still owns every actual patch.
      const owned = new Map<object, Map<string, unknown>>();
      let changes = 0, active = true;
      const disableInstance = instance.disable;
      instance.disable = function(...args: unknown[]) {
        // An outer application wrapper may retain ours after unpatching. Make every
        // retained wrapper inert before cleanup, including rollback/error paths.
        active = false; return Reflect.apply(disableInstance, this, args);
      };
      const wrapMethod = instance._wrap, unwrapMethod = instance._unwrap;
      instance._wrap = function(target: any, name: string, wrapper: any) {
        const before = target[name];
        // Never replace an application-owned wrapper.
        if (before?.__wrapped) return before;
        const result = wrapMethod.call(this, target, name, (original: any) => {
          const instrumented = wrapper(original);
          return function(this: unknown, ...args: unknown[]) {
            if (!active) return Reflect.apply(original, this, args);
            return Reflect.apply(instrumented, this, args);
          };
        });
        if (target[name] !== before) {
          const methods = owned.get(target) ?? new Map(); methods.set(name, target[name]); owned.set(target, methods); changes++;
        }
        return result;
      };
      instance._unwrap = function(target: any, name: string) {
        const methods = owned.get(target);
        if (methods?.has(name) && methods.get(name) === target[name]) { methods.delete(name); return unwrapMethod.call(this, target, name); }
      };
      const guardedUndo = new WeakSet<Function>();
      const guardUndoMaps = (maps: (readonly [Map<any, any>, number])[]) => {
        for (const [map] of maps) for (const [target, undo] of map) {
          if (!target || !["object", "function"].includes(typeof target) || typeof undo !== "function" || guardedUndo.has(undo)) continue;
          const methods = Object.entries(Object.getOwnPropertyDescriptors(target)).filter(([, d]) => "value" in d && typeof d.value === "function");
          const wrapped = function(this: unknown, ...args: unknown[]) {
            // An outer application wrapper may retain an owned class patch. Leave
            // that outer wrapper in place; the disabled instance makes ours inert.
            const foreign = methods.flatMap(([name, descriptor]) => {
              const current = Object.getOwnPropertyDescriptor(target, name);
              return current?.value !== descriptor.value ? [[name, current] as const] : [];
            });
            try { return Reflect.apply(undo, this, args); }
            finally { for (const [name, descriptor] of foreign) { try { if (descriptor) Object.defineProperty(target, name, descriptor); else delete target[name]; } catch { /* Preserve frozen application objects. */ } } }
          };
          guardedUndo.add(wrapped); map.set(target, wrapped);
        }
      };
      // Processor-based integrations register callbacks rather than _wrap methods.
      // Their SDK may retain the processor after shutdown; make those callbacks inert.
      const guardProcessor = (processor: any) => {
        if (!processor || typeof processor !== "object") return;
        for (const name of ["onTraceStart", "onTraceEnd", "onSpanStart", "onSpanEnd"]) {
          const callback = processor[name];
          if (typeof callback === "function") processor[name] = function(this: unknown, ...args: unknown[]) { if (active) return Reflect.apply(callback, this, args); };
        }
      };
      // Observe actual patches rather than calling a registered hook 'instrumented'.
      const wrap = (definition: any) => {
        if (typeof definition.patch !== "function") return;
        const patch = definition.patch, unpatch = definition.unpatch, modules = new WeakSet<object>();
        let nativeOwned = false;
        definition.unpatch = function(this: unknown, ...args: any[]) {
          if (args[0] && (modules.has(args[0]) || nativeOwned)) { modules.delete(args[0]); return unpatch?.apply(this, args); }
          return args[0];
        };
        definition.patch = function(this: unknown, ...args: any[]) {
          const before = changes, changed = patchSnapshot(args[0]), processor = instance._processor;
          // Class patch registries (e.g. Cursor's undo map) and SDK processor
          // registrations are instance-owned even when module exports are getters.
          const maps = Object.values(Object.getOwnPropertyDescriptors(instance)).flatMap(d => "value" in d && d.value instanceof Map ? [[d.value, d.value.size] as const] : []);
          const nativeChanged = () => Boolean(instance._processor && instance._processor !== processor) || maps.some(([map, size]) => map.size !== size);
          const ownsPatch = () => changes > before || changed() || nativeChanged();
          try {
            const result = patch.apply(this, args);
            if (ownsPatch() || nativeOwned || (result && result !== args[0])) {
              if (nativeChanged()) { nativeOwned = true; guardUndoMaps(maps); }
              if (instance._processor !== processor) guardProcessor(instance._processor);
              modules.add(args[0]); status.status = "instrumented"; status.reason = esm ? undefined : "CommonJS only; ESM loader unavailable";
            } else if (!modules.has(args[0])) { status.status = "manual"; status.reason = "Application-owned instrumentation already patched this module"; }
            return result;
          } catch {
            if (ownsPatch() && args[0]) modules.add(args[0]);
            try { instance.disable(); } catch { /* Best-effort rollback of partial patching. */ }
            status.status = "failed"; status.reason = "Integration patch failed; wire capture is still available";
            warn(entry.name, `Integration ${entry.name} failed; the application will continue.`);
            return args[0];
          }
        };
      };
      for (const definition of instance._modules ?? []) { wrap(definition); for (const file of definition.files ?? []) wrap(file); }
      const disable = registerInstrumentations({ tracerProvider: provider, instrumentations: [instance] });
      removers.push(() => {
        try { disable(); }
        finally {
          // Some instrumentor releases omit optional API methods in unpatch().
          for (const [target, methods] of owned) for (const name of methods.keys()) { try { instance._unwrap(target, name); } catch { /* Only our wrappers are candidates. */ } }
          owned.clear();
        }
      });
    } catch {
      try { instance?.disable(); } catch { /* Fail open. */ }
      status.status = "failed"; status.reason = "Could not load or register instrumentation (check installed versions)";
      warn(entry.name, `Integration ${entry.name} failed; the application will continue.`);
    }
  }
  if (enabled("http", config)) {
    try {
      const path = appRequire().resolve("@opentelemetry/instrumentation-http");
      const { HttpInstrumentation } = appRequire()(path);
      // Outgoing propagation has its own privacy/policy boundary; it creates no
      // HTTP CLIENT spans and must not be enabled by this incoming-only package.
      const instance = new HttpInstrumentation({ enabled: false, disableOutgoingRequestInstrumentation: true });
      removers.push(registerInstrumentations({ tracerProvider: provider, instrumentations: [instance] }));
      statuses.push({ name: "http", status: "instrumented", reason: esm ? "Incoming SERVER spans only" : "Incoming SERVER spans, CommonJS only; ESM loader unavailable" });
    } catch { statuses.push({ name: "http", status: "not_installed", reason: "Optional @opentelemetry/instrumentation-http" }); }
  } else statuses.push({ name: "http", status: "disabled" });
  return () => {
    for (const remove of removers.reverse()) { try { remove(); } catch { /* Leave host running. */ } }
  };
}
