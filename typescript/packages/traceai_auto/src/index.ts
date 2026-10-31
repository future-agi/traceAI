import { context, propagation, trace, SpanKind, SpanStatusCode, Context, TracerProvider } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { ExportResultCode, W3CTraceContextPropagator, W3CBaggagePropagator, CompositePropagator } from "@opentelemetry/core";
import { ParentBasedSampler, TraceIdRatioBasedSampler, SpanProcessor, SpanExporter, ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { getAttributesFromContext, ProjectType, register, createSpanExporter, registerSecret, seedSecrets, markSecretDiscoveryFailed, BatchSpanProcessor, hasExportPrivacyBoundary } from "@traceai/fi-core";
import { Config, credentialsForConfig, enabled, maskConfig, Options, resolveConfig } from "./config";
import { enableLibraries, IntegrationStatus, inventory } from "./libraries";
import { FetchHook, patchFetch, trackCoverage } from "./wire/fetch";
import { captureSecrets, SpanResult } from "./wire/core";
import { withDeadline } from "./deadline";
import { privacyFilter } from "./export-privacy";
import { chainSigterm } from "./signals";
import { observeTransports } from "./wire/observe-node";
import { projectAttributes, tracerAttributeLimit } from "./attribute-projection";
export { registerSecret } from "@traceai/fi-core";

export type { Options } from "./config";
interface AgentState {
  config: Config; statuses: IntegrationStatus[]; active: boolean; provider?: any; ownsProvider?: boolean;
  processor?: SpanProcessor; detach?: () => void; removeLibraries?: () => void; fetch?: FetchHook;
  collector: string; shutdown?: Promise<void>; beforeExit?: () => void; removeSignals?: () => void;
  removeObservation?: () => void; ownsGlobalProvider?: boolean; contextManager?: AsyncLocalStorageContextManager; propagator?: CompositePropagator;
}
const STATE_KEY = Symbol.for("traceai.auto.agent.v1");
let state: AgentState | undefined;
function sharedState(): AgentState | undefined { return state ??= (globalThis as any)[STATE_KEY]; }
const warned = new Set<string>();
function warn(key: string, message: string): void {
  if (warned.has(key)) return; warned.add(key);
  try { console.warn(`[traceai:auto] ${message}`); } catch { /* Host logger may throw. */ }
}
function guardedExporter(exporter: SpanExporter, agent: AgentState, options: Options, failedCaptures: WeakSet<object>): SpanExporter {
  const resource = { project_name: agent.config.projectName, project_type: agent.config.projectType };
  // Default FI constructors own S1 themselves. Unknown/supplied destinations
  // use the same boundary here. Explicit init hiding remains authoritative even
  // if a supplied FI exporter has its own, less restrictive constructor policy.
  const privacy = options.exporter || !hasExportPrivacyBoundary(exporter) ? privacyFilter(options.traceConfig, [agent.config.apiKey, agent.config.secretKey].filter((v): v is string => !!v)) : undefined;
  return {
    export(spans, callback) {
      let called = false;
      const done = (result: { code: ExportResultCode; error?: Error }) => {
        if (called) return; called = true;
        agent.collector = result.code === ExportResultCode.SUCCESS ? "accepted" : "failed";
        if (result.code !== ExportResultCode.SUCCESS) warn("collector", "Collector export failed; the application will continue.");
        callback(result);
      };
      try {
        // Enrich only the copy sent to FI: leave the app's spans, resources and other
        // exporters untouched, including when attaching to an existing global SDK.
        const copies: ReadableSpan[] = privacy ? spans.map(span => privacy.span(span, failedCaptures.has(span) ? null : [], resource)) : spans;
        if (!privacy && spans.some(span => failedCaptures.has(span))) markSecretDiscoveryFailed();
        exporter.export(copies, done);
      } catch { done({ code: ExportResultCode.FAILED }); }
    },
    async shutdown() { try { await exporter.shutdown(); } catch { warn("collector", "Collector export failed; the application will continue."); } },
    async forceFlush() { try { await exporter.forceFlush?.(); } catch { warn("collector", "Collector export failed; the application will continue."); } },
  };
}
function existingProvider(): any {
  const proxy: any = trace.getTracerProvider(), delegate = proxy.getDelegate?.() ?? proxy;
  return delegate.constructor?.name === "NoopTracerProvider" ? undefined : delegate;
}
function attach(provider: any, processor: SpanProcessor): (() => void) | undefined {
  // OTel JS 2.x removed addSpanProcessor. Use its live MultiSpanProcessor list,
  // not a replacement provider or replacement list (which would strand old tracers).
  const processors = provider._activeSpanProcessor?._spanProcessors;
  if (Array.isArray(processors)) {
    processors.push(processor);
    return () => { const index = processors.indexOf(processor); if (index >= 0) processors.splice(index, 1); };
  }
  if (typeof provider.addSpanProcessor === "function") { provider.addSpanProcessor(processor); return () => {}; }
  return undefined;
}
function removeHooks(agent: AgentState): void {
  for (const remove of [
    () => { if (agent.beforeExit) process.removeListener("beforeExit", agent.beforeExit); },
    () => agent.removeSignals?.(),
    () => agent.fetch?.remove(), () => agent.removeObservation?.(), () => agent.removeLibraries?.(),
  ]) { try { remove(); } catch { /* A broken cleanup must not skip the others. */ } }
}
export interface Report { active: boolean; config: Record<string, unknown>; integrations: IntegrationStatus[]; collector: string }
/** Initialize once. No network calls or thrown startup errors. */
export function init(options: Options = {}): TracerProvider | undefined {
  const existing = sharedState();
  if (existing) { warn("initialized", "Already initialized; keeping the existing provider and configuration."); return existing.active ? existing.provider : undefined; }
  try {
    const config = resolveConfig(options), agent: AgentState = { config, statuses: inventory(config), active: false, collector: "not_tested (no startup network I/O)" };
    options = { ...options, traceConfig: config.traceConfig };
    state = agent; (globalThis as any)[STATE_KEY] = agent;
    const hostProvider = options.provider ?? existingProvider();
    seedSecrets(process.env, credentialsForConfig(config));
    const fiCredentials = !!config.apiKey && !!config.secretKey;
    if (config.sdkDisabled || !fiCredentials && !options.exporter && !options.spanProcessor && !hostProvider) {
      for (const status of agent.statuses) { status.status = "disabled"; status.reason = config.sdkDisabled ? "OTEL_SDK_DISABLED" : "Missing credentials"; }
      agent.statuses.push({ name: "wire", status: "disabled" }, { name: "http", status: "disabled" });
      if (!config.sdkDisabled) warn("credentials", "FI_API_KEY and FI_SECRET_KEY are required; auto-instrumentation is disabled.");
      if (config.debug) console.info(JSON.stringify(report(), null, 2));
      return undefined;
    }
    const failedCaptures = new WeakSet<object>();
    let processor: SpanProcessor;
    if (options.spanProcessor) {
      const supplied = options.spanProcessor;
      processor = { onStart: (span, parent) => supplied.onStart(span, parent), onEnd: span => supplied.onEnd(span),
        forceFlush: () => supplied.forceFlush(), shutdown: () => supplied.shutdown() };
    }
    else if (options.exporter || fiCredentials) {
      const exporter = guardedExporter(options.exporter ?? createSpanExporter({ endpoint: config.baseUrl,
        headers: { "x-api-key": config.apiKey!, "x-secret-key": config.secretKey! }, verbose: false, traceConfig: config.traceConfig,
        resourceAttributes: { project_name: config.projectName, project_type: config.projectType } }), agent, options, failedCaptures);
      processor = new BatchSpanProcessor(exporter);
    } else {
      // Host-only mode observes coverage without creating a credentialed FI route.
      processor = { onStart() {}, onEnd() {}, async forceFlush() {}, async shutdown() {} };
      agent.collector = "not_tested (application-owned destination)";
    }
    agent.processor = processor;
    const onStart = processor.onStart.bind(processor);
    processor.onStart = (span, parent) => { trackCoverage(span, parent); onStart(span, parent); };
    const existing = hostProvider;
    if (existing) {
      agent.detach = attach(existing, processor);
      if (agent.detach || options.provider && !options.exporter && !options.spanProcessor && !fiCredentials) agent.provider = existing;
      else warn("provider", "Existing provider cannot accept processors; using an isolated AI provider without replacing it.");
    }
    if (!agent.provider) {
      agent.provider = register({ projectName: config.projectName, projectType: config.projectType === "observe" ? ProjectType.OBSERVE : ProjectType.EXPERIMENT,
        setGlobalTracerProvider: false, verbose: false, endpoint: config.baseUrl, spanProcessors: [processor],
        sampler: new ParentBasedSampler({ root: new TraceIdRatioBasedSampler(config.sampleRate) }) });
      agent.ownsProvider = true;
      if (!existing) agent.ownsGlobalProvider = trace.setGlobalTracerProvider(agent.provider);
    }
    // Avoid duplicate-global diagnostics when the application owns context already.
    const currentManager = (context as any)._getContextManager?.();
    if (!currentManager || currentManager.constructor?.name === "NoopContextManager") {
      const manager = new AsyncLocalStorageContextManager().enable();
      if (context.setGlobalContextManager(manager)) agent.contextManager = manager;
      else manager.disable();
    }
    const currentPropagator = (propagation as any)._getGlobalPropagator?.();
    if (!currentPropagator || currentPropagator.constructor?.name === "NoopTextMapPropagator") {
      const propagator = new CompositePropagator({ propagators: [new W3CTraceContextPropagator(), new W3CBaggagePropagator()] });
      if (propagation.setGlobalPropagator(propagator)) agent.propagator = propagator;
    }
    const tracer = agent.provider.getTracer("@traceai/auto", "0.1.0");
    const admissionPrivacy = privacyFilter(options.traceConfig, [config.apiKey, config.secretKey].filter((v): v is string => !!v));
    const emit = (result: SpanResult, parent: Context, start: number, end: number) => {
      try {
        const secrets = captureSecrets(result);
        if (secrets) for (const secret of secrets) registerSecret(secret);
        let admitted = {};
        try { if (secrets !== null) admitted = admissionPrivacy.attributes({ ...getAttributesFromContext(parent), ...result.attributes }, undefined, result.attributes["gen_ai.operation.name"]); }
        catch { /* Fail closed before SDK value truncation, retaining identity/timing. */ }
        const projected = projectAttributes(admitted, tracerAttributeLimit(tracer));
        const span = tracer.startSpan(result.name, { kind: SpanKind.CLIENT, startTime: start, attributes: projected.attributes }, parent);
        // Agent-created SDK spans only: preserve pre-admission losses alongside
        // any native SDK losses. Other application spans are never changed.
        if (projected.dropped && typeof (span as any)._droppedAttributesCount === "number") (span as any)._droppedAttributesCount += projected.dropped;
        if (secrets === null) failedCaptures.add(span);
        span.setStatus({ code: result.ok ? SpanStatusCode.OK : SpanStatusCode.ERROR, message: result.description });
        span.end(end);
      } catch { /* Internal failures are not application failures. */ }
    };
    if (config.wireCapture && enabled("wire", config)) agent.removeObservation = observeTransports();
    agent.removeLibraries = enableLibraries(config, options, agent.provider, agent.statuses, warn, processor);
    if (config.wireCapture && enabled("wire", config) && typeof globalThis.fetch === "function") {
      agent.fetch = patchFetch(config.wireMaxBodyBytes, emit, [config.apiKey, config.secretKey].filter((v): v is string => !!v)); agent.statuses.push({ name: "wire", status: "instrumented" });
    } else agent.statuses.push({ name: "wire", status: "disabled" });
    agent.active = true;
    agent.beforeExit = () => { void forceFlush(); };
    process.once("beforeExit", agent.beforeExit);
    agent.removeSignals = chainSigterm(forceFlush);
    if (config.debug) console.info(JSON.stringify(report(), null, 2));
  } catch {
    warn("init", "Auto-instrumentation could not initialize; the application will continue.");
    const failed = state;
    if (failed) {
      failed.active = false; removeHooks(failed);
      try { failed.detach?.(); } catch { /* Roll back only our processor. */ }
      try { if (failed.propagator && (propagation as any)._getGlobalPropagator?.() === failed.propagator) propagation.disable(); } catch { /* Leave application globals alone. */ }
      try { if (failed.ownsGlobalProvider && existingProvider() === failed.provider) trace.disable(); } catch { /* Leave app globals alone. */ }
      try { if (failed.contextManager && (context as any)._getContextManager?.() === failed.contextManager) context.disable(); } catch { /* Fail open. */ }
      void withDeadline(async () => { await failed.processor?.shutdown(); }).catch(() => {});
      // Configuration is terminal for this process, even after failed startup.
      // Hooks are rolled back, but init never installs a second provider or loader.
    }
  }
  return state?.active ? state.provider : undefined;
}
export function report(): Report {
  sharedState();
  if (!state) {
    const config = resolveConfig();
    return { active: false, config: maskConfig(config), integrations: inventory(config), collector: "not_tested (agent not initialized)" };
  }
  return { active: state.active, config: maskConfig(state.config), integrations: state.statuses.map(s => ({ ...s })), collector: state.collector };
}
export async function forceFlush(): Promise<void> {
  sharedState();
  try {
    await withDeadline(async () => {
      await Promise.all([...(state?.fetch?.pending ?? [])]);
      await state?.processor?.forceFlush();
    });
  } catch { if (state?.collector !== "failed") warn("flush", "Trace flush failed; the application will continue."); }
}
export async function shutdown(): Promise<void> {
  const agent = sharedState();
  if (!agent) return;
  if (agent.shutdown) return agent.shutdown;
  agent.shutdown = (async () => {
    agent.active = false; removeHooks(agent);
    // Reserve half the total budget for resource cleanup. An outer race alone would
    // return on time but never invoke shutdown() when forceFlush() stalls forever.
    try {
      await withDeadline(async () => {
        await Promise.all([...(agent.fetch?.pending ?? [])]);
        await agent.processor?.forceFlush();
      }, 2500);
    } catch { warn("shutdown", "Trace shutdown failed; the application will continue."); }
    finally {
      // Completed bodies can still be decoding. Keep our processor attached until
      // those spans have ended and flushed (or the drain budget expires).
      try { agent.detach?.(); } catch { /* Leave the application's processors intact. */ }
      try { await withDeadline(async () => { await agent.processor?.shutdown(); }, 2500); }
      catch { warn("shutdown", "Trace shutdown failed; the application will continue."); }
    }
  })();
  return agent.shutdown;
}
