import * as nodeModule from "node:module";
import { createRequire } from "node:module";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { context, diag, trace } from "@opentelemetry/api";
import { TraceIdRatioBasedSampler } from "@opentelemetry/sdk-trace-base";
import { FIContextBridge } from "./FIContextBridge.js";
import { createWithContext, liveSpanContext, observeNativeSpans, rememberExport } from "./span-context.js";
import { ownData } from "./snapshot.js";
import type { ObservabilityEntrypoint, ObservabilityInstance } from "@mastra/core/observability";
import type { FIMastraSpanExporter } from "./FIMastraSpanExporter.js";
import type { createFIMastraExporter } from "./FIObservability.js";
import type { SpanProcessor } from "@opentelemetry/sdk-trace-base";

const KEY = Symbol.for("traceai.mastra.auto.v1");
const AGENT_KEY = Symbol.for("traceai.auto.agent.v1");
const LOADED_KEY = Symbol.for("traceai.mastra.loaded-observability.v1");

/** @internal The root/manual entry may evaluate observability before enable. */
export function registerLoadedObservability(module: { BaseSpan: any; DefaultObservabilityInstance: any; Observability: any }): void {
  const globals = globalThis as any;
  const loaded: Set<typeof module> = globals[LOADED_KEY] ?? (globals[LOADED_KEY] = new Set());
  loaded.add(module);
  const state = globals[KEY] as State | undefined;
  if (state?.enabled) {
    try {
      state.wrapSpan(module.BaseSpan);
      state.wrapInstance(module.DefaultObservabilityInstance);
      state.wrapObservability(module.Observability);
    } catch { warn(state, "Could not patch previously loaded observability"); }
  }
}

interface Agent {
  processor?: SpanProcessor;
  config?: {
    apiKey?: string;
    secretKey?: string;
    projectName?: string;
    projectType?: "observe" | "experiment";
    sampleRate?: number;
    sdkDisabled?: boolean;
    instrumentations?: string[];
    disabledInstrumentations?: string[];
  };
}
interface State {
  enabled: boolean;
  warned: Set<string>;
  constructors: WeakMap<Function, Function>;
  wrapMastra: (target: any) => any;
  wrapObservability: (target: any) => any;
  wrapSpan: (target: any) => void;
  wrapInstance: (target: any) => void;
}

function agent(): Agent | undefined {
  return (globalThis as any)[AGENT_KEY];
}
function warn(state: State, key: string): void {
  if (state.warned.has(key)) return;
  state.warned.add(key);
  // Never include exception messages, paths, config or credentials in diagnostics.
  try { diag.warn(`[@traceai/mastra] ${key}; application execution is unchanged.`); } catch { /* Fail open even with a throwing logger. */ }
}

/**
 * Enable Mastra's exporter installation without changing application code.
 * Called after @traceai/auto initializes its processor and before Mastra is loaded.
 * No second transport/provider is created: converted spans go through the agent's
 * existing export-time privacy boundary, flush lifecycle and collector.
 * Returns false when the agent or synchronous Node loader API is unavailable.
 */
export function enableMastra(): boolean {
  try { return installMastra(); }
  catch {
    try { diag.warn("[@traceai/mastra] Could not enable instrumentation; application execution is unchanged."); } catch { /* Fail open. */ }
    return false;
  }
}

function installMastra(): boolean {
  const globals = globalThis as any;
  const previous = globals[KEY] as State | undefined;
  if (previous?.enabled) return true;
  const state: State = previous ?? {
    enabled: false,
    warned: new Set(),
    constructors: new WeakMap(),
    wrapMastra: target => target,
    wrapObservability: target => target,
    wrapSpan: () => {},
    wrapInstance: () => {},
  };
  globals[KEY] = state;
  const current = agent();
  if (process.env.OTEL_SDK_DISABLED === "true" || current?.config?.sdkDisabled) return false;
  if (current?.config?.disabledInstrumentations?.includes("mastra") ||
    (current?.config?.instrumentations?.length && !current.config.instrumentations.includes("mastra"))) return false;
  if (!current?.processor || !current.config?.apiKey || !current.config?.secretKey) {
    warn(state, "An initialized @traceai/auto agent is required");
    return false;
  }
  // Synchronous hooks run in the application realm, unlike module.register's
  // loader worker. They cover both import and require, including Mastra's hashed
  // re-export chunks, without replacing the module namespace or unrelated exports.
  const registerHooks = (nodeModule as any).registerHooks;
  if (typeof registerHooks !== "function") {
    warn(state, "Node 22.15+ or 23.5+ is required for zero-code Mastra");
    return false;
  }

  // Resolve peers from the application, not from an unrelated global install.
  // Defer loading the manual exporter until after the source hook is installed.
  const requireFromApp = createRequire(join(process.cwd(), "__traceai_mastra__.cjs"));
  function exporter(serviceName?: string): FIMastraSpanExporter {
    const active = agent();
    if (!active?.processor || !active.config) throw new Error("Agent unavailable");
    const api = requireFromApp("@traceai/mastra") as {
      createFIMastraExporter: typeof createFIMastraExporter;
    };
    return api.createFIMastraExporter({
      apiKey: active.config.apiKey,
      secretKey: active.config.secretKey,
      serviceName: serviceName ?? process.env.OTEL_SERVICE_NAME ?? "mastra-app",
      projectName: active.config.projectName,
      projectType: active.config.projectType,
      spanProcessor: active.processor,
      sampleRate: active.config.sampleRate,
    });
  }
  const attempted = new WeakSet<ObservabilityInstance>();
  const patchedInstances = new WeakSet<object>();
  function install(instance: ObservabilityInstance): void {
    try {
      if (attempted.has(instance)) return;
      // Public APIs only. A custom instance without runtime registration stays
      // untouched; do not clone it into a plain config and lose its behavior.
      if (typeof instance.getExporters !== "function" || typeof instance.registerExporter !== "function") {
        warn(state, "Custom observability instance cannot register an exporter");
        return;
      }
      if (instance.getExporters().some(item => item?.name === "future-agi")) return;
      const additional = exporter(instance.getConfig().serviceName);
      // Cached constructors bypass the config proxy. For a known default
      // instance only, detach its SDK-owned exporter slot before registration:
      // Mastra otherwise appends into the application's (possibly frozen) array.
      for (let prototype = Object.getPrototypeOf(instance), depth = 0; prototype && depth < 16; prototype = Object.getPrototypeOf(prototype), depth++) {
        if (!patchedInstances.has(prototype)) continue;
        const config = Object.getOwnPropertyDescriptor(instance, "config")?.value;
        const field = config && Object.getOwnPropertyDescriptor(config, "exporters");
        if (field && "value" in field && Array.isArray(field.value)) {
          Object.defineProperty(config, "exporters", { ...field, value: [...field.value] });
        }
        break;
      }
      // A third-party registration may partly succeed before throwing. Do not
      // attach a second sink if the same instance is subsequently registered
      // under another name or supplied to another Mastra constructor.
      attempted.add(instance);
      instance.registerExporter(additional);
    } catch { warn(state, "Could not attach the Future AGI exporter"); }
  }
  function installAll(observability: ObservabilityEntrypoint): void {
    try {
      for (const instance of observability.listInstances().values()) install(instance);
    } catch { warn(state, "Unsupported Mastra observability entrypoint"); }
  }
  function wrap(target: any, construct: (target: any, args: any[], newTarget: any) => any): any {
    const cached = state.constructors.get(target);
    if (cached) return cached;
    const proxy = new Proxy(target, { construct });
    state.constructors.set(target, proxy);
    state.constructors.set(proxy, proxy);
    return proxy;
  }
  const sampler = new TraceIdRatioBasedSampler(current.config.sampleRate ?? 1);
  observeNativeSpans(current.processor);
  state.wrapInstance = target => {
    if (patchedInstances.has(target.prototype)) return;
    const descriptor = Object.getOwnPropertyDescriptor(target.prototype, "createSpan");
    if (typeof descriptor?.value !== "function") return;
    const original = descriptor.value;
    // A cached Observability constructor cannot be replaced through its import
    // binding. Its instances still call this public prototype boundary. Provide
    // a per-instance identity-only bridge here too, without replacing an app
    // bridge or mutating its config. This bridge has no event/lifecycle work.
    const getBridge = target.prototype.getBridge;
    const bridges = new WeakMap<object, FIContextBridge>();
    if (typeof getBridge === "function") {
      Object.defineProperty(target.prototype, "getBridge", { configurable: true, writable: true, value: function(this: object) {
        const configured = Reflect.apply(getBridge, this, []);
        if (configured !== undefined) return configured;
        let bridge = bridges.get(this);
        if (!bridge) { bridge = new FIContextBridge(); bridges.set(this, bridge); }
        return bridge;
      } });
    }
    Object.defineProperty(target.prototype, "createSpan", { ...descriptor, value: function(this: unknown, ...args: any[]) {
      return createWithContext(() => Reflect.apply(original, this, args), sampler);
    } });
    const getSpanForExport = target.prototype.getSpanForExport;
    if (typeof getSpanForExport === "function") {
      Object.defineProperty(target.prototype, "getSpanForExport", { configurable: true, writable: true, value: function(this: unknown, ...args: unknown[]) {
        const result = Reflect.apply(getSpanForExport, this, args);
        try {
          // Output processors may return a different live span, or implement a
          // custom exportSpan. Associate the final snapshot with the ORIGINAL
          // creation decision, without retaining IDs in a process-wide map or
          // changing what the bridge/application exporters receive. A processor
          // that changes identity must not suppress that new identity's export.
          const span = args[0];
          const id = ownData(span, "id");
          const traceId = ownData(span, "traceId");
          if (span && typeof span === "object" && result && typeof result === "object" &&
            typeof id === "string" && typeof traceId === "string" &&
            ownData(result, "id") === id && ownData(result, "traceId") === traceId) {
            rememberExport(span, result);
          }
        } catch { /* Preserve processor/filter return values and exceptions. */ }
        return result;
      } });
    }
    patchedInstances.add(target.prototype);
  };
  const patchedSpans = new WeakSet<object>();
  state.wrapSpan = target => {
    if (patchedSpans.has(target.prototype)) return;
    patchedSpans.add(target.prototype);
    const snapshot = Object.getOwnPropertyDescriptor(target.prototype, "exportSpan");
    if (typeof snapshot?.value === "function") {
      const original = snapshot.value;
      Object.defineProperty(target.prototype, "exportSpan", { ...snapshot, value: function(this: any, ...args: unknown[]) {
        const result = Reflect.apply(original, this, args);
        try { if (result && typeof result === "object") rememberExport(this, result); } catch { /* Preserve the original snapshot. */ }
        return result;
      } });
    }
    // Activate INSIDE the callback, after any preserved application bridge has
    // selected its context. Only model-step/chunk scopes inherit model coverage;
    // a tool can make a new request while its enclosing generation is in the tree.
    for (const name of ["executeInContext", "executeInContextSync"]) {
      const descriptor = Object.getOwnPropertyDescriptor(target.prototype, name);
      if (typeof descriptor?.value !== "function") continue;
      const original = descriptor.value;
      Object.defineProperty(target.prototype, name, { ...descriptor, value: function(this: any, ...args: unknown[]) {
        const fn = args[0];
        if (typeof fn !== "function") return Reflect.apply(original, this, args);
        const span = this;
        const callback = function(this: unknown, ...inner: unknown[]) {
          let active = context.active();
          try {
            // Internal/excluded spans have no exported identity; parent native
            // descendants to the nearest visible span, not an orphan carrier.
            const visible = span.isExcluded ? span.getParentSpan?.(false) : span;
            const identity = visible && liveSpanContext(visible, sampler);
            if (span.isValid !== false && identity) {
              let kind: string | undefined;
              let scope = span;
              const seen = new Set<object>();
              for (let depth = 0; scope && depth < 256 && !seen.has(scope); depth++) {
                seen.add(scope);
                if (!scope.isExcluded && ["model_generation", "model_inference", "rag_embedding"].includes(scope.type)) {
                  kind = scope.type === "rag_embedding" ? "EMBEDDING" : "LLM";
                  break;
                }
                if (!["model_step", "model_chunk"].includes(scope.type)) break;
                scope = scope.parent;
              }
              const selected = trace.getSpan(active);
              const selectedIdentity = selected?.spanContext();
              if (selected && selectedIdentity?.traceId === identity.traceId && selectedIdentity?.spanId === identity.spanId) {
                // The application bridge already selected the real SDK span.
                // Keep that exact recording object: replacing it discards the
                // application's setAttribute/addEvent/recordException calls.
                if (kind) selected.setAttribute("gen_ai.span.kind", kind);
              } else {
                const carrier = Object.assign(trace.wrapSpanContext(identity), {
                  attributes: kind ? { "gen_ai.span.kind": kind } : {},
                });
                active = trace.setSpan(active, carrier);
              }
            }
          } catch { /* Invalid/unsupported spans preserve upstream context and wire fallback. */ }
          return context.with(active, () => Reflect.apply(fn, this, inner));
        };
        return Reflect.apply(original, this, [callback, ...args.slice(1)]);
      } });
    }
  };
  const patchedRegistries = new WeakSet<object>();
  state.wrapObservability = target => {
    if (!patchedRegistries.has(target.prototype)) {
      patchedRegistries.add(target.prototype);
      const descriptor = Object.getOwnPropertyDescriptor(target.prototype, "registerInstance");
      if (typeof descriptor?.value === "function") {
        const original = descriptor.value;
        Object.defineProperty(target.prototype, "registerInstance", { ...descriptor, value: function(this: unknown, ...args: unknown[]) {
          const result = Reflect.apply(original, this, args);
          if (args[1]) install(args[1] as ObservabilityInstance);
          return result;
        } });
      }
    }
    return wrap(target, (original, args, newTarget) => {
      let next = args;
      try {
        // Mastra retains config.exporters by reference. Give it a private array
        // before registerExporter appends, preserving frozen application configs
        // and leaving the application's original exporter array untouched.
        const raw = args[0];
        if (raw && typeof raw === "object") {
          const descriptors = Object.getOwnPropertyDescriptors(raw);
          const configs = descriptors.configs;
          if (configs && "value" in configs && configs.value && typeof configs.value === "object" && !Array.isArray(configs.value)) {
            const entries = Object.getOwnPropertyDescriptors(configs.value);
            for (const entry of Object.values(entries)) {
              if (!("value" in entry) || !entry.value || Object.getPrototypeOf(entry.value) !== Object.prototype) continue;
              const fields = Object.getOwnPropertyDescriptors(entry.value);
              if (fields.exporters && "value" in fields.exporters && Array.isArray(fields.exporters.value)) {
                fields.exporters = { ...fields.exporters, value: [...fields.exporters.value] };
              }
              if (!fields.bridge || ("value" in fields.bridge && fields.bridge.value === undefined)) {
                fields.bridge = { value: new FIContextBridge(), enumerable: true, writable: true, configurable: true };
              }
              entry.value = Object.create(Object.prototype, fields);
            }
            descriptors.configs = { ...configs, value: Object.create(Object.getPrototypeOf(configs.value), entries) };
            next = [Object.create(Object.getPrototypeOf(raw), descriptors), ...args.slice(1)];
          }
        }
      } catch { warn(state, "Could not snapshot the Mastra exporter configuration"); }
      // The original constructor is called exactly once. Its exceptions propagate
      // unchanged; only our post-construction attachment is fail-open.
      const result = Reflect.construct(original, next, newTarget) as ObservabilityEntrypoint;
      installAll(result);
      return result;
    });
  };
  state.wrapMastra = target => wrap(target, (original, args, newTarget) => {
    let next = args;
    try {
      const config = args[0];
      if (config === undefined || (config !== null && typeof config === "object")) {
        const descriptors = config === undefined ? {} : Object.getOwnPropertyDescriptors(config);
        const descriptor = descriptors.observability;
        // Do not execute a user getter early or change Mastra's config validation.
        if (!descriptor || ("value" in descriptor && descriptor.value === undefined)) {
          const { Observability } = requireFromApp("@mastra/observability");
          const observability = new Observability({
            configs: { "future-agi": {
              serviceName: process.env.OTEL_SERVICE_NAME ?? "mastra-app",
              exporters: [exporter()],
              bridge: new FIContextBridge(),
              excludeSpanTypes: ["model_chunk"],
            } },
          });
          descriptors.observability = { value: observability, enumerable: true, writable: true, configurable: true };
          const copy = Object.create(config === undefined ? Object.prototype : Object.getPrototypeOf(config), descriptors);
          next = [copy, ...args.slice(1)];
        } else if ("value" in descriptor && descriptor.value) {
          installAll(descriptor.value);
        }
      }
    } catch { warn(state, "Could not initialize Mastra observability"); }
    return Reflect.construct(original, next, newTarget);
  });

  try {
    registerHooks({
      load(url: string, context: unknown, nextLoad: (url: string, context: unknown) => any) {
        const loaded = nextLoad(url, context);
        try {
          if (!url.startsWith("file:") || !["module", "commonjs"].includes(loaded.format)) return loaded;
          const path = fileURLToPath(url).replace(/\\/g, "/");
          const core = /\/node_modules\/@mastra\/core\/dist\/[^/]+\.(?:c?js)$/.test(path);
          const observability = /\/node_modules\/@mastra\/observability\/dist\/index\.(?:c?js)$/.test(path);
          if (!core && !observability) return loaded;
          if (loaded.source === undefined || loaded.source === null) return loaded;
          const source = typeof loaded.source === "string" ? loaded.source : Buffer.from(loaded.source).toString("utf8");
          const name = core ? "Mastra" : "Observability";
          // Touch only known, local class bindings. If Mastra changes its build
          // shape, leave the entire original source (and its exports) untouched.
          if (!new RegExp(`\\bvar ${name} = class\\b`).test(source)) return loaded;
          const method = core ? "wrapMastra" : "wrapObservability";
          const patch = `\n;try { ${name} = globalThis[Symbol.for("traceai.mastra.auto.v1")].${method}(${name});\n` +
            (observability && /\bvar BaseSpan = class\b/.test(source) ? 'globalThis[Symbol.for("traceai.mastra.auto.v1")].wrapSpan(BaseSpan);\n' : "") +
            (observability && /\bvar DefaultObservabilityInstance = class\b/.test(source) ? 'globalThis[Symbol.for("traceai.mastra.auto.v1")].wrapInstance(DefaultObservabilityInstance);\n' : "") +
            (loaded.format === "commonjs" ? `exports.${name} = ${name};\n` : "") +
            '} catch { /* Unsupported layout: retain the application module. */ }\n';
          return { ...loaded, source: source + patch };
        } catch { warn(state, "Unsupported Mastra module layout"); return loaded; }
      },
    });
    state.enabled = true;
    // A root import loads the manual factories (and observability) before init.
    // Patch those exact CJS/ESM classes too; a later source hook cannot revisit
    // an evaluated module. No application constructor or bridge is replaced.
    for (const loaded of globals[LOADED_KEY] ?? []) {
      try { registerLoadedObservability(loaded); }
      catch { warn(state, "Could not patch previously loaded observability"); }
    }
    return true;
  } catch {
    warn(state, "Could not register the Mastra loader hook");
    return false;
  }
}
