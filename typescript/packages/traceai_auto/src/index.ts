import { trace, SpanKind, SpanStatusCode, TraceFlags, Context, TracerProvider } from "@opentelemetry/api";
import { ExportResultCode } from "@opentelemetry/core";
import { AlwaysOffSampler, AlwaysOnSampler, ParentBasedSampler, TraceIdRatioBasedSampler, Sampler, SamplingDecision, SpanProcessor, SpanExporter, ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { getAttributesFromContext, ProjectType, register, registerSecret, seedSecrets, markSecretDiscoveryFailed, BatchSpanProcessor, hasExportPrivacyBoundary, FITracer, rememberSpanValueLimit } from "@traceai/fi-core";
// The Future AGI route factory is provided by @traceai/fi-core through a process-wide symbol, not a package export.
const FI_ROUTE_FACTORY = Symbol.for("traceai.fi-core.fi-route-factory");
function createFiRouteSpanExporter(options: Record<string, unknown>): SpanExporter {
  const factory = (globalThis as any)[FI_ROUTE_FACTORY];
  if (typeof factory !== "function") throw new Error("The installed @traceai/fi-core does not provide the Future AGI route. Install a matching @traceai/fi-core version.");
  return factory(options);
}
import { autoModelResponsibility, autoModelWireAllowed, createAutoSpanDelivery, hasAutoSpanSink, rememberAutoSpanSink, type AutoSpanDelivery } from "@traceai/fi-core";
import { Config, credentialsForConfig, enabled, explicitlyEnabled, incompleteCredentialSources, maskConfig, Options, resolveConfig, outgoingPolicyForConfig } from "./config";
import { createOutgoingPrivacy, noteOutgoingSpanStart } from "./outgoing-privacy";
import { installOutgoingPropagation } from "./outgoing-node";
import { enableLibraries, IntegrationStatus, inventory } from "./libraries";
import { endCoverage, FetchHook, patchFetch, trackCoverage } from "./wire/fetch";
import { removePromiseObservation } from "./wire/promise-node";
import { captureSecrets, SpanResult } from "./wire/core";
import { withDeadline } from "./deadline";
import { privacyFilter } from "./export-privacy";
import { contextBoundProvider, isAgentSpan, markAgentSpan } from "./agent-context";
import { HTTP_SCOPE } from "./incoming-parent";
import { forwardedHiding, hideForwardedContent } from "./forwarded-content";
import { samplingRatioAdmission, type SamplerSelection } from "./sampling-ratio";

const VERSION = "0.4.2"; // x-release-please-version
import { chainSigterm } from "./signals";
import { observeTransports } from "./wire/observe-node";
import { borrowConvertedSpanDestination, ConvertedSpanDestination } from "./converted-spans";
import { idempotentExporter } from "./idempotent-export";
export { registerSecret } from "@traceai/fi-core";
// Context for AI spans, also in applications without their own context manager.
export { runInContext as withContext, runWithSession as withSession, runWithUser as withUser,
  runWithMetadata as withMetadata, runWithTracingSuppressed as withTracingSuppressed } from "./agent-context";

export type { Options } from "./config";
interface AgentState {
  config: Config; statuses: IntegrationStatus[]; active: boolean; provider?: any; ownsProvider?: boolean;
  spanDelivery?: AutoSpanDelivery;
  ownsSpanExporter?: boolean;
  processor?: SpanProcessor; processorIsObserver?: boolean; convertedSpanDestination?: ConvertedSpanDestination;
  detach?: () => void; removeLibraries?: () => void; fetch?: FetchHook;
  collector: string; shutdown?: Promise<void>; beforeExit?: () => void; removeSignals?: () => void;
  removeOutgoing?: () => void; removeObservation?: () => void;
  /** Spans handed to the FI route and not yet exported. */
  unsent: number; collectorFailed?: boolean;
  /** Forwarding switch only: attach to an application provider registered after init. */
  tryForward?: () => void; cancelForward?: () => void;
  /** Outgoing propagation opt-in only: observe the application's span starts. */
  tryObserve?: () => void; stopObserving?: () => void;
}
const STATE_KEY = Symbol.for("traceai.auto.agent.v1");
let state: AgentState | undefined;
function sharedState(): AgentState | undefined { return state ??= (globalThis as any)[STATE_KEY]; }
const warned = new Set<string>();
function warn(key: string, message: string): void {
  if (warned.has(key)) return; warned.add(key);
  try { console.warn(`[traceai:auto] ${message}`); } catch { /* Host logger may throw. */ }
}
/** Only called for a provider the agent creates; an application's sampler is never replaced. */
function environmentSampler(): SamplerSelection {
  const fallback = (): SamplerSelection => ({
    sampler: new ParentBasedSampler({ root: new TraceIdRatioBasedSampler(1) }),
    ratio: { rate: 1, parentBased: true },
  });
  // Filtered installs can resolve OTel 1.x. Use public sampler constructors
  // shared by both majors and parse the environment once for decision and metadata.
  const name = process.env.OTEL_TRACES_SAMPLER;
  if (!name?.trim()) return fallback();
  switch (name) {
    case "always_on": return { sampler: new AlwaysOnSampler() };
    case "always_off": return { sampler: new AlwaysOffSampler() };
    case "parentbased_always_on": case "parentbased_always_off": {
      const on = name === "parentbased_always_on";
      return { sampler: new ParentBasedSampler({ root: on ? new AlwaysOnSampler() : new AlwaysOffSampler() }),
        ratio: { rate: on ? 1 : 0, parentBased: true } };
    }
    case "traceidratio": case "parentbased_traceidratio": {
      const raw = process.env.OTEL_TRACES_SAMPLER_ARG;
      const rate = raw?.trim() ? Number(raw) : 1;
      if (!Number.isFinite(rate) || rate < 0 || rate > 1) {
        warn("sampler", "Invalid OTEL_TRACES_SAMPLER_ARG; using ParentBased(TraceIdRatio(1)).");
        return fallback();
      }
      const root = new TraceIdRatioBasedSampler(rate), parentBased = name === "parentbased_traceidratio";
      return { sampler: parentBased ? new ParentBasedSampler({ root }) : root, ratio: { rate, parentBased } };
    }
    default:
      warn("sampler", "Invalid or unsupported OTEL_TRACES_SAMPLER; using ParentBased(TraceIdRatio(1)).");
      return fallback();
  }
}
// AI spans that the agent's sampler recorded with the sampled flag off (see aiSampler).
const recordOnly = new WeakSet<object>();
// The trace id of the sampler's last such decision. The SDK starts that span on the
// agent's provider, and runs the processor's onStart, right after the sampler returns.
let recordOnlyDecision: string | undefined;
/**
 * AI spans are sampled by trace id at FI_SAMPLE_RATE, and the application's sampling
 * decision never drops them. Under a parent the application did not sample, an
 * admitted AI span is recorded with the sampled flag off, so the spans the application
 * starts inside the AI call, and their outgoing trace headers, keep its decision.
 */
function aiSampler(rate: number): Sampler {
  const ratio: Sampler = new TraceIdRatioBasedSampler(rate);
  return {
    shouldSample(ctx, traceId, spanName, spanKind, attributes, links) {
      recordOnlyDecision = undefined;
      const result = ratio.shouldSample(ctx, traceId, spanName, spanKind, attributes, links);
      if (result.decision !== SamplingDecision.RECORD_AND_SAMPLED) return result;
      const parent = trace.getSpanContext(ctx);
      if (!parent || !trace.isSpanContextValid(parent) || (parent.traceFlags & TraceFlags.SAMPLED) === TraceFlags.SAMPLED) return result;
      recordOnlyDecision = traceId;
      return { decision: SamplingDecision.RECORD };
    },
    toString: () => `TraceAIAutoSampler{${ratio.toString()}}`,
  };
}
/** First in the processor's onStart: marks the span the sampler has just recorded with the flag off. */
function noteRecordOnly(span: ReadableSpan): void {
  const decided = recordOnlyDecision;
  recordOnlyDecision = undefined;
  if (decided === undefined) return;
  try {
    const { traceId, traceFlags } = span.spanContext();
    if (traceId === decided && (traceFlags & TraceFlags.SAMPLED) === 0) recordOnly.add(span);
  } catch { /* Not marked; the batch processor then drops it. */ }
}
// The batch processor exports only sampled spans, so a record-only AI span reaches it
// as a view whose span context has the sampled flag set. The exporter turns the view
// back into the span before anything reads it, so lookups by span still match.
const sampledViews = new WeakMap<object, ReadableSpan>();
function sampledView(span: ReadableSpan): ReadableSpan {
  const view = Object.create(span, { spanContext: { value: () => {
    const ctx = span.spanContext();
    return { ...ctx, traceFlags: ctx.traceFlags | TraceFlags.SAMPLED };
  } } }) as ReadableSpan;
  sampledViews.set(view, span);
  return view;
}
function guardedExporter(exporter: SpanExporter, agent: AgentState, options: Options, failedCaptures: WeakSet<object>): SpanExporter {
  const resource = { project_name: agent.config.projectName, project_type: agent.config.projectType };
  // Default FI constructors own S1 themselves. Unknown/supplied destinations
  // use the same boundary here. Explicit init hiding remains authoritative even
  // if a supplied FI exporter has its own, less restrictive constructor policy.
  const privacy = options.exporter || !hasExportPrivacyBoundary(exporter) ? privacyFilter(options.traceConfig, [agent.config.apiKey, agent.config.secretKey].filter((v): v is string => !!v)) : undefined;
  // Only with the forwarding switch do application spans reach this exporter.
  // FI_HIDE_* then also hides other AI tracers' prompt and response keys on them.
  const hiding = agent.config.forwardApplicationSpans ? forwardedHiding(options.traceConfig) : undefined;
  return idempotentExporter({
    export(batch, callback) {
      const spans = batch.map(span => sampledViews.get(span) ?? span);
      let called = false;
      const done = (result: { code: ExportResultCode; error?: Error }) => {
        if (called) return; called = true;
        agent.unsent = Math.max(0, agent.unsent - spans.length);
        agent.collector = result.code === ExportResultCode.SUCCESS ? "accepted" : "failed";
        if (result.code !== ExportResultCode.SUCCESS) { agent.collectorFailed = true; warn("collector", "Collector export failed; the application will continue."); }
        callback(result);
      };
      try {
        // Enrich only the copy sent to FI: leave the app's spans, resources and other
        // exporters untouched, including when attaching to an existing global SDK.
        const views = hiding ? spans.map(span => isAgentSpan(span) ? span : hideForwardedContent(span, hiding)) : spans;
        const copies: ReadableSpan[] = privacy ? views.map((view, i) => privacy.span(view, failedCaptures.has(spans[i]) ? null : [], resource)) : views;
        if (!privacy && spans.some(span => failedCaptures.has(span))) markSecretDiscoveryFailed();
        exporter.export(copies, done);
      } catch { done({ code: ExportResultCode.FAILED }); }
    },
    async shutdown() { try { await exporter.shutdown(); } catch { warn("collector", "Collector export failed; the application will continue."); } },
    async forceFlush() { try { await exporter.forceFlush?.(); } catch { warn("collector", "Collector export failed; the application will continue."); } },
  });
}
function existingProvider(): any {
  const proxy: any = trace.getTracerProvider(), delegate = proxy.getDelegate?.() ?? proxy;
  return delegate.constructor?.name === "NoopTracerProvider" ? undefined : delegate;
}
function attach(provider: any, processor: SpanProcessor): (() => void) | undefined {
  // OTel JS 2.x removed addSpanProcessor. Use its live MultiSpanProcessor list,
  // not a replacement provider or replacement list (which would strand old tracers).
  // An explicitly supplied API proxy must observe the same delegate that its
  // getTracer() uses. Keep the caller-visible provider object unchanged.
  const target = provider.getDelegate?.() ?? provider;
  const processors = target._activeSpanProcessor?._spanProcessors;
  if (Array.isArray(processors)) {
    processors.push(processor);
    return () => { const index = processors.indexOf(processor); if (index >= 0) processors.splice(index, 1); };
  }
  if (typeof target.addSpanProcessor === "function") { target.addSpanProcessor(processor); return () => {}; }
  return undefined;
}
/** Forwarding switch only: the preload runs before the application starts its SDK. */
function forwardLater(agent: AgentState, processor: SpanProcessor): void {
  const attempt = () => {
    if (!agent.active || agent.detach) { agent.tryForward = undefined; return; }
    const app = existingProvider();
    if (!app || app === agent.provider) return;
    agent.tryForward = undefined;
    agent.detach = attach(app, processor);
    if (!agent.detach) warn("provider", "The application's provider cannot accept processors; its spans are not forwarded.");
  };
  agent.tryForward = attempt;
  const immediate = setImmediate(attempt); immediate.unref?.();
  agent.cancelForward = () => { agent.tryForward = undefined; clearImmediate(immediate); };
}
/**
 * Outgoing propagation opt-in only. Trace headers are added only under spans whose
 * start the agent observed, so it notes the kind and redirect lineage of the
 * application's spans. This observer never exports, ends or changes a span.
 */
function observeApplicationSpans(agent: AgentState, supplied: unknown): void {
  const observer: SpanProcessor = { onStart: (span, parent) => noteOutgoingSpanStart(span, parent), onEnd() {}, async forceFlush() {}, async shutdown() {} };
  let detach: (() => void) | undefined, immediate: ReturnType<typeof setImmediate> | undefined;
  const attempt = () => {
    if (detach) return;
    const app = supplied ?? existingProvider();
    if (!app || app === agent.provider) return;
    agent.tryObserve = undefined;
    detach = attach(app, observer);
    if (!detach) { detach = () => {}; warn("outgoing-observer", "The application's provider cannot accept processors; outgoing requests get no trace headers."); }
  };
  agent.stopObserving = () => {
    agent.tryObserve = undefined;
    if (immediate) clearImmediate(immediate);
    const remove = detach; detach = () => {};
    remove?.();
  };
  attempt();
  // The application may start its SDK after the preload.
  if (!detach) { agent.tryObserve = attempt; immediate = setImmediate(attempt); immediate.unref?.(); }
}
// Incoming HTTP SERVER spans reach Future AGI only for traces with AI work.
interface ServerGate { note(traceId: unknown): void; start(span: any): void; admit(span: any): boolean }
function serverGate(): ServerGate {
  const traces = new Set<string>();
  const traceId = (span: any): unknown => { try { return span.spanContext().traceId; } catch { return undefined; } };
  const scope = (span: any): unknown => span?.instrumentationScope?.name ?? span?.instrumentationLibrary?.name;
  const note = (id: unknown) => {
    if (typeof id !== "string" || traces.has(id)) return;
    traces.add(id);
    if (traces.size > 1024) traces.delete(traces.values().next().value as string);
  };
  return {
    note,
    start(span) { if (scope(span) !== HTTP_SCOPE) note(traceId(span)); },
    admit(span) { if (scope(span) !== HTTP_SCOPE) return true; const id = traceId(span); return typeof id === "string" && traces.has(id); },
  };
}
function credentialsWarning(config: Config): string {
  const incomplete = incompleteCredentialSources(config);
  return incomplete.length
    ? `Ignoring incomplete credentials from ${incomplete.join("; ")}. FI_API_KEY and FI_SECRET_KEY are required in the same source; auto-instrumentation is disabled and nothing is sent to Future AGI.`
    : "FI_API_KEY and FI_SECRET_KEY are required; auto-instrumentation is disabled and nothing is sent to Future AGI.";
}
function removeHooks(agent: AgentState): void {
  for (const remove of [
    () => { if (agent.beforeExit) process.removeListener("beforeExit", agent.beforeExit); },
    () => agent.removeSignals?.(), () => agent.cancelForward?.(), () => agent.stopObserving?.(),
    () => agent.removeOutgoing?.(), () => agent.fetch?.remove(), () => removePromiseObservation(), () => agent.removeObservation?.(), () => agent.removeLibraries?.(),
  ]) { try { remove(); } catch { /* A broken cleanup must not skip the others. */ } }
}
export interface Report { active: boolean; config: Record<string, unknown>; integrations: IntegrationStatus[]; collector: string }
/** Initialize once. No network calls or thrown startup errors. */
export function init(options: Options = {}): TracerProvider | undefined {
  const existing = sharedState();
  if (existing) { warn("initialized", "Already initialized; keeping the existing provider and configuration."); return existing.active ? existing.provider : undefined; }
  try {
    const config = resolveConfig(options), agent: AgentState = { config, statuses: inventory(config), active: false, collector: "not_tested (no startup network I/O)", unsent: 0 };
    options = { ...options, traceConfig: config.traceConfig };
    state = agent; (globalThis as any)[STATE_KEY] = agent;
    seedSecrets(process.env, credentialsForConfig(config));
    const fiCredentials = !!config.apiKey && !!config.secretKey;
    // The FI route: the Future AGI collector, or an exporter/processor given to init().
    const route = fiCredentials || !!options.exporter || !!options.spanProcessor;
    // Without a route or a provider given to init(), install nothing: no provider,
    // hooks or globals. An application provider found on the API is not a destination.
    if (config.sdkDisabled || !route && !options.provider) {
      for (const status of agent.statuses) { status.status = "disabled"; status.reason = config.sdkDisabled ? "OTEL_SDK_DISABLED" : "Missing credentials"; }
      agent.statuses.push({ name: "wire", status: "disabled" }, { name: "http", status: "disabled" }, { name: "outgoing-http", status: "disabled" });
      if (!config.sdkDisabled) warn("credentials", credentialsWarning(config));
      if (config.debug) console.info(JSON.stringify(report(), null, 2));
      return undefined;
    }
    const forwarding = config.forwardApplicationSpans;
    const failedCaptures = new WeakSet<object>();
    let processor: SpanProcessor;
    if (options.spanProcessor) {
      const supplied = options.spanProcessor;
      processor = { onStart: (span, parent) => supplied.onStart(span, parent), onEnd: span => supplied.onEnd(span),
        forceFlush: () => supplied.forceFlush(), shutdown: () => supplied.shutdown() };
      rememberAutoSpanSink(processor, supplied);
    }
    else if (options.exporter || fiCredentials) {
      // The FI keys go to the resolved base URL. When a file or profile chose it, say which host, once.
      if (!options.exporter && (config.baseUrlSource === "config file" || config.baseUrlSource === "profile")) {
        let host = "<invalid URL>";
        try { host = new URL(config.baseUrl).host || host; } catch { /* Reported as invalid. */ }
        warn("base-host", `Sending traces to ${host} (base URL from the ${config.baseUrlSource}).`);
      }
      const exporter = guardedExporter(options.exporter ?? createFiRouteSpanExporter({ endpoint: config.baseUrl,
        headers: { "x-api-key": config.apiKey!, "x-secret-key": config.secretKey! }, verbose: false, traceConfig: config.traceConfig,
        resourceAttributes: { project_name: config.projectName, project_type: config.projectType } }), agent, options, failedCaptures);
      processor = new BatchSpanProcessor(exporter);
      agent.ownsSpanExporter = true;
    } else {
      // Host-only mode observes coverage without creating a credentialed FI route.
      processor = { onStart() {}, onEnd() {}, async forceFlush() {}, async shutdown() {} };
      rememberAutoSpanSink(processor);
      agent.processorIsObserver = true;
      agent.collector = "not_tested (application-owned destination)";
    }
    agent.processor = processor;
    // Innermost, before any integration wraps onEnd: a record-only AI span reaches the
    // batch processor, or a supplied processor, as a view with the sampled flag set.
    const deliverEnd = processor.onEnd.bind(processor);
    processor.onEnd = span => deliverEnd(recordOnly.has(span) ? sampledView(span) : span);
    // AI-only route: an incoming SERVER span passes only when its trace has AI work.
    const gate = !forwarding && !agent.processorIsObserver && explicitlyEnabled("http", config) ? serverGate() : undefined;
    const onStart = processor.onStart.bind(processor);
    processor.onStart = (span, parent) => { noteRecordOnly(span); agent.tryForward?.(); agent.tryObserve?.(); gate?.start(span); noteOutgoingSpanStart(span, parent); rememberSpanValueLimit(span); trackCoverage(span, parent); onStart(span, parent); };
    // Where AI spans are created. By default this is the agent's own provider,
    // never registered globally, whose only processor is the FI route. The
    // application's provider is used only when init() is given one without an FI
    // route, or, with the forwarding switch, to send its spans as before.
    const host = !route ? options.provider : forwarding ? options.provider ?? existingProvider() : undefined;
    if (route && !forwarding && options.provider) warn("provider", "init() was given a provider, but AI spans go to the Future AGI route on the agent's own provider. forwardApplicationSpans: true would create them on yours, but it also sends your application's own spans to Future AGI; to keep AI spans on your provider without Future AGI, pass it to init() without FI keys.");
    if (host) {
      agent.detach = attach(host, processor);
      if (agent.detach || !route) agent.provider = host;
      else warn("provider", "Existing provider cannot accept processors; using an isolated AI provider without replacing it.");
    }
    if (!agent.provider) {
      // Resolve against the application's environment before constructing the SDK.
      // Explicit rates retain aiSampler's decisions, including record-only handling
      // of unsampled parents, but sampled decisions inherit the parent's metadata.
      const admission = samplingRatioAdmission(config.sampleRateExplicit
        ? { sampler: aiSampler(config.sampleRate), ratio: { rate: config.sampleRate, parentBased: true } }
        : environmentSampler());
      const { sampler } = admission;
      const ownedStart = processor.onStart.bind(processor);
      processor.onStart = (span, parent) => { admission.onStart(span); ownedStart(span, parent); };
      const samplerName = process.env.OTEL_TRACES_SAMPLER, samplerArg = process.env.OTEL_TRACES_SAMPLER_ARG;
      try {
        // BasicTracerProvider evaluates its env defaults even with a sampler supplied.
        // Hide only these already-resolved inputs during synchronous construction to
        // avoid duplicate diagnostics for rejected/overridden values. Do not change
        // the application's diagnostic logger or suppress unrelated SDK diagnostics.
        delete process.env.OTEL_TRACES_SAMPLER;
        delete process.env.OTEL_TRACES_SAMPLER_ARG;
        agent.provider = register({ projectName: config.projectName, projectType: config.projectType === "observe" ? ProjectType.OBSERVE : ProjectType.EXPERIMENT,
          setGlobalTracerProvider: false, verbose: false, endpoint: config.baseUrl, batch: true, spanProcessors: [processor], sampler });
      } finally {
        if (samplerName === undefined) delete process.env.OTEL_TRACES_SAMPLER;
        else process.env.OTEL_TRACES_SAMPLER = samplerName;
        if (samplerArg === undefined) delete process.env.OTEL_TRACES_SAMPLER_ARG;
        else process.env.OTEL_TRACES_SAMPLER_ARG = samplerArg;
      }
      agent.ownsProvider = true;
      if (forwarding && !host) forwardLater(agent, processor);
    }
    if (agent.processorIsObserver) {
      agent.convertedSpanDestination = borrowConvertedSpanDestination(agent.provider, processor);
      const destination = agent.convertedSpanDestination;
      // Flush the borrowed sinks, never their shutdown. The adapter excludes this
      // observer from the live list, so neither flush nor conversion can recurse.
      if (destination) processor.forceFlush = () => destination.processor.forceFlush();
    }
    // Membership is necessary, not sufficient: census follows our wrapper to
    // the actual supplied target on EVERY admission/forwarding check. Host wire
    // sinks remain independent when that target is empty, no-op or shut down.
    const deliveryPipeline = (agent.provider.getTracer("@traceai/mastra") as any)._spanProcessor;
    agent.spanDelivery = agent.convertedSpanDestination?.delivery ?? createAutoSpanDelivery(() =>
      !agent.processorIsObserver && hasAutoSpanSink(processor) && (deliveryPipeline === processor ||
        Array.isArray(deliveryPipeline?._spanProcessors) && deliveryPipeline._spanProcessors.includes(processor)));
    // No global tracer provider, context manager or propagator is registered.
    // Only instrumentors get this facade; the returned provider remains native.
    // Explicit standalone FITracer users on it are unaffected. Its tracers parent
    // spans under the application's active span, or keep the agent's own parent
    // links when the application has no context manager.
    const instrumentationProvider = FITracer.autoProvider(contextBoundProvider(agent.provider));
    const tracer = agent.provider.getTracer("@traceai/auto", VERSION);
    // Wire spans always pass their request's parent context explicitly.
    const wireTracer = new FITracer({ tracer: FITracer.autoProvider(agent.provider).getTracer("@traceai/auto", VERSION), traceConfig: options.traceConfig });
    const admissionPrivacy = privacyFilter(options.traceConfig, [config.apiKey, config.secretKey].filter((v): v is string => !!v));
    const wirePipeline = (tracer as any)._spanProcessor;
    const wireDestinationReady = () => hasAutoSpanSink(wirePipeline, agent.processorIsObserver ? [processor] : []);
    const emit = (result: SpanResult, parent: Context, start: number, end: number) => {
      const canDeliver = () => agent.spanDelivery?.canForward(wireDestinationReady) === true;
      // Credential discovery is independent of which semantic copy wins delivery.
      let secrets: ReturnType<typeof captureSecrets>;
      try {
        secrets = captureSecrets(result);
        if (secrets) for (const secret of secrets) registerSecret(secret);
      } catch { markSecretDiscoveryFailed(); return; }
      // Body consumption may occur much later, in an unrelated active Context.
      // Re-read selection's live authority from the retained REQUEST Context at
      // this first irreversible handoff, never from context.active() or a cache.
      const responsibility = autoModelResponsibility(parent);
      const operation = responsibility?.operation;
      if (!autoModelWireAllowed(operation, responsibility?.independentCoverage === true, "complete")) return;
      // A recovered closer native owner leaves the outer aggregate/earlier route
      // untouched. No claim is made about native delivery after this decision.
      // Unsupported URLs, unread bodies and empty/no-op sinks never claim wire.
      if (!canDeliver()) { operation?.finishWire(false); return; }
      if (operation && !operation.claimWire(canDeliver)) return;
      let submitted = false;
      try {
        let admitted = {};
        try { if (secrets !== null) admitted = admissionPrivacy.attributes({ ...getAttributesFromContext(parent), ...result.attributes }, undefined, result.attributes["gen_ai.operation.name"]); }
        catch { /* Fail closed before SDK value truncation, retaining identity/timing. */ }
        // Host SDK limits admit this already-protected FI-authored data. Only
        // our own provider reserves sampler metadata; borrowed storage is untouched.
        const span = wireTracer.startSpan(result.name, { kind: SpanKind.CLIENT, startTime: start, attributes: admitted }, parent);
        markAgentSpan(span.getContextSpan());
        if (secrets === null) failedCaptures.add(span.getContextSpan());
        span.setStatus({ code: result.ok ? SpanStatusCode.OK : SpanStatusCode.ERROR, message: result.description });
        const recording = span.isRecording();
        span.end(end);
        submitted = recording;
      } catch { /* Internal failures are not application failures. */ }
      finally { operation?.finishWire(submitted); }
    };
    // HTTP/HTTPS propagation is independent of wire/body capture and HTTP spans.
    // Adapters stay off unless explicitly selected; destination policy is not activation.
    const outgoingPolicy = outgoingPolicyForConfig(config);
    agent.removeOutgoing = installOutgoingPropagation(outgoingPolicy, createOutgoingPrivacy(config.traceConfig, credentialsForConfig(config)), agent.statuses, warn, config.propagateOutgoingAdapters);
    // With the FI processor on the application's provider (no FI route, or the
    // forwarding switch), its start hook already observes the application's spans.
    if (route && !forwarding && config.propagateOutgoingAdapters === true && outgoingPolicy.valid && outgoingPolicy.mode !== "none") observeApplicationSpans(agent, options.provider);
    if (config.wireCapture && enabled("wire", config)) agent.removeObservation = observeTransports();
    // On the AI-only route the agent does not install its HTTP instrumentation beside
    // an application provider, which normally has its own. With the forwarding switch
    // (or no FI route) AI spans are on that provider, and the agent's HTTP spans too.
    const httpBesideApplication = route && !forwarding && !!(options.provider ?? existingProvider());
    agent.removeLibraries = enableLibraries(config, options, instrumentationProvider, agent.statuses, warn, processor, httpBesideApplication);
    // Genkit creates its spans with the global tracer provider. On the AI-only route
    // the agent's provider is never global, so those spans stay with the application.
    const genkit = route && !forwarding ? agent.statuses.find(status => status.name === "genkit") : undefined;
    if (genkit?.status === "instrumented") {
      genkit.status = "manual";
      genkit.reason = "Genkit spans use the global tracer provider; set FI_FORWARD_APP_SPANS=true, or pass your provider to init() without an FI route, to send them";
    }
    // Stay outside the installed mapping adapters: Genkit can shorten an SDK-
    // truncated value when it scrubs a local path or identity. Inspect the native
    // span first so that its detached export copy cannot erase that loss signal.
    const mappedEnd = processor.onEnd.bind(processor);
    processor.onEnd = span => {
      endCoverage(span);
      if (gate && !gate.admit(span)) return;
      rememberSpanValueLimit(span);
      // Sampled spans are exported, and so are AI spans recorded with the flag off under
      // an unsampled application parent. Any other span that is recorded but not sampled never is.
      let sampled = false;
      try { sampled = (span.spanContext().traceFlags & TraceFlags.SAMPLED) === TraceFlags.SAMPLED; } catch { /* Not counted. */ }
      if (!agent.processorIsObserver && (sampled || recordOnly.has(span))) agent.unsent++;
      mappedEnd(span);
    };
    if (config.wireCapture && enabled("wire", config) && typeof globalThis.fetch === "function") {
      // A captured AI request marks its trace, so its SERVER span may pass the gate.
      // On the AI-only route the wire layer yields only to model spans the agent made:
      // an application's own model span never reaches Future AGI.
      agent.fetch = patchFetch(config.wireMaxBodyBytes, emit, [config.apiKey, config.secretKey].filter((v): v is string => !!v),
        gate ? parent => gate.note(trace.getSpanContext(parent)?.traceId) : undefined, route && !forwarding);
      agent.statuses.push({ name: "wire", status: "instrumented" });
    } else agent.statuses.push({ name: "wire", status: "disabled" });
    agent.active = true;
    agent.beforeExit = () => { void forceFlush(); };
    process.once("beforeExit", agent.beforeExit);
    // Hold SIGTERM for a flush only while the FI route has spans to send and the
    // collector has not failed.
    agent.removeSignals = chainSigterm(forceFlush, () => !agent.collectorFailed && (agent.unsent > 0 || (agent.fetch?.pending.size ?? 0) > 0));
    if (config.debug) console.info(JSON.stringify(report(), null, 2));
  } catch {
    warn("init", "Auto-instrumentation could not initialize; the application will continue.");
    const failed = state;
    if (failed) {
      failed.active = false; removeHooks(failed);
      failed.spanDelivery?.revoke();
      failed.convertedSpanDestination?.revoke();
      try { failed.detach?.(); } catch { /* Roll back only our processor. */ }
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
      await Promise.all([...(state?.fetch?.pending ?? []), state?.spanDelivery?.drain()]);
      await state?.processor?.forceFlush();
      // A completed flush leaves nothing pending, including spans a full queue dropped.
      if (state) state.unsent = 0;
    });
  } catch { if (state?.collector !== "failed") warn("flush", "Trace flush failed; the application will continue."); }
}
export async function shutdown(): Promise<void> {
  const agent = sharedState();
  if (!agent) return;
  if (agent.shutdown) return agent.shutdown;
  agent.shutdown = (async () => {
    // One absolute 5s budget, retaining the existing 2.5s drain reservation. Stop
    // new exporter admissions before cleanup; already-admitted BaseExporter work
    // (including formatting before the handler) remains deliverable until cutoff.
    const started = Date.now(), deadline = started + 5000, drainDeadline = started + 2500;
    const remaining = (until: number) => Math.max(0, until - Date.now());
    agent.spanDelivery?.stopAdmissions(drainDeadline);
    agent.active = false; removeHooks(agent);
    try {
      await withDeadline(async () => {
        await Promise.all([...(agent.fetch?.pending ?? []), agent.spanDelivery?.drain()]);
        // Never start a late flush after the outer deadline race has returned.
        if (Date.now() < drainDeadline) await agent.processor?.forceFlush();
      }, remaining(drainDeadline));
    } catch { warn("shutdown", "Trace shutdown failed; the application will continue."); }
    finally {
      // Completed bodies can still be decoding. Keep our processor attached until
      // those spans have ended and flushed (or the drain budget expires). Revoke
      // retained framework forwarding before its native observer is detached.
      agent.spanDelivery?.revoke();
      agent.convertedSpanDestination?.revoke();
      try { agent.detach?.(); } catch { /* Leave the application's processors intact. */ }
      try { await withDeadline(async () => { await agent.processor?.shutdown(); }, Math.min(2500, remaining(deadline))); }
      catch { warn("shutdown", "Trace shutdown failed; the application will continue."); }
    }
  })();
  return agent.shutdown;
}
