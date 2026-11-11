import { context, trace } from "@opentelemetry/api";
import { isTracingSuppressed } from "@opentelemetry/core";
import { v4 as uuid } from "uuid";
import { AttachmentTicket, type TicketHost } from "./admission";
import { AttachmentCorrelation, attachmentTraceUuid, validAttachmentUuid } from "./correlation";
import { attachmentImportDirection, attachmentResolvedRecordingDenial, validAttachmentImportRequest } from "./imports";
import type { AdmissionFacts, AttachmentHost, CoreJob, UploadOutcome, UploadSpec } from "./internal";
import { validAttachmentWire } from "./wire";
import { attachmentOwnedFile, type AttachmentOwnedFileRecord, type AttachmentOwnedLease } from "./owned";
import { attachmentEnabled, attachmentModality, evaluateAttachmentPolicy } from "./policy";
import {
  ATTACHMENT_WAV_HEADER_BYTES, AttachmentRecording, attachmentChunkedSource, attachmentRecordingShape, attachmentSegmentFrames, attachmentWavHeader,
  type RecordingFileSegment,
} from "./recording";
import { AttachmentConsumedStream, type StreamHost, type StreamState } from "./stream";
import { AttachmentTransportError, attachmentIdempotencyKey, attachmentIsLegacyWire, attachmentWireTransport, validAttachmentSession, validAttachmentUpload } from "./transport-v1";
import { attachmentWireRejected } from "./warn";
import type {
  AttachmentAdmission, AttachmentAdmissionTicket, AttachmentClock, AttachmentCompletedRecordingRequest, AttachmentCompletion, AttachmentContentSource,
  AttachmentFields, AttachmentFlushResult, AttachmentHandle, AttachmentImportHandle, AttachmentImportOptions, AttachmentManagerOptions, AttachmentOptions,
  AttachmentOwnedFileRef, AttachmentOwnedRecordingHandle, AttachmentOwnedRecordingOptions, AttachmentPacer, AttachmentPreflightOptions,
  AttachmentRecordingOptions, AttachmentRecordingWriter, AttachmentReference, AttachmentRuntimeState, AttachmentSession, AttachmentSource,
  AttachmentSourceKind, AttachmentSpan, AttachmentStatus, AttachmentStream, AttachmentStreamOptions, AttachmentTarget, AttachmentTransportV1,
  AttachmentUploadRequest,
} from "./types";

const MiB = 1024 * 1024;
const CHUNK = 256 * 1024;
// Reserve bounded transfer/read/response scratch for both workers inside the 32 MiB envelope.
const MAX_MEMORY = 32 * MiB - 2 * (CHUNK + 2 * 65536);
const SKEW = 5000;
const QUALIFIED = new Set<AttachmentSourceKind>(["Bytes", "Blob", "OwnedRecordingFile"]);
const PURPOSES = ["conversation_recording", "turn_recording", "model_input", "model_output", "tool_result", "computer_screenshot", "diagnostic", "document"];
const ROLES = ["user", "assistant", "tool", "system", "unknown"];
const TOKEN = /^[a-zA-Z0-9_-]{1,128}$/;

function bounded(value: number | undefined, fallback: number, max: number): number {
  return Number.isInteger(value) && value! > 0 ? Math.min(value!, max) : fallback;
}
function unref(timer: ReturnType<typeof setTimeout>): void { (timer as unknown as { unref?: () => void }).unref?.(); }
const defaultClock: AttachmentClock = {
  now: () => (typeof performance !== "undefined" && typeof performance.now === "function" ? performance.now() : Date.now()),
  wall: () => Date.now(),
};
type HandleControl = { value: AttachmentHandle; finish: (status: AttachmentCompletion["status"]) => void };
function handle(admission: AttachmentAdmission): HandleControl {
  let resolve!: (result: AttachmentCompletion) => void;
  const completion = new Promise<AttachmentCompletion>(done => { resolve = done; });
  const value = Object.freeze({ attachmentId: uuid(), occurrenceId: uuid(), admission, completion });
  let settled = false;
  const finish = (status: AttachmentCompletion["status"]) => { if (!settled) { settled = true; resolve(Object.freeze({ status })); } };
  if (admission !== "queued") finish(admission);
  return { value, finish };
}
function codeOf(error: unknown): Exclude<AttachmentCompletion["status"], "failed"> {
  if (error instanceof AttachmentTransportError) return error.code === "failed" ? "incomplete" : error.code;
  return "incomplete";
}
function raceAbort<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) return Promise.reject(new AttachmentTransportError("incomplete"));
  return new Promise<T>((resolve, reject) => {
    const abort = () => reject(new AttachmentTransportError("incomplete", false));
    signal.addEventListener("abort", abort, { once: true });
    promise.then(value => { signal.removeEventListener("abort", abort); resolve(value); }, error => { signal.removeEventListener("abort", abort); reject(error); });
  });
}
function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise(resolve => {
    const done = () => { clearTimeout(timer); signal.removeEventListener("abort", done); resolve(); };
    const timer = setTimeout(done, Math.max(0, ms)); unref(timer);
    signal.addEventListener("abort", done, { once: true });
    if (signal.aborted) done();
  });
}
/** One token bucket per manager at the negotiated upload_bytes_per_second. */
class TokenBucket implements AttachmentPacer {
  private tokens = 0; private last = 0; private started = false;
  constructor(private readonly rate: () => number, private readonly clock: AttachmentClock) {}
  async take(bytes: number, signal: AbortSignal): Promise<void> {
    const rate = this.rate();
    if (!(rate > 0) || rate >= 1073741824) return;
    const now = this.clock.now();
    if (!this.started) { this.started = true; this.tokens = rate; this.last = now; }
    this.tokens = Math.min(rate, this.tokens + (now - this.last) * rate / 1000); this.last = now;
    this.tokens -= bytes;
    if (this.tokens < 0) await pause(-this.tokens / rate * 1000, signal);
  }
}
/** ES2021 FinalizationRegistry, typed locally so consumers compiling this source with an older `lib` still build. */
interface Finalizer { register(target: object, held: () => void, token?: object): void; unregister(token: object): boolean }
type FinalizerConstructor = new (cleanup: (held: () => void) => void) => Finalizer;
class Lane { readonly jobs: CoreJob[] = []; busy = false; constructor(readonly id: string) {} }
interface Reservation { bytes: number; expiresAt: number }
interface StreamExtra { facts: AdmissionFacts; fields: AttachmentFields; correlation: AttachmentCorrelation; purpose?: AttachmentOptions["purpose"] }

/** One per provider/runtime; explicit attachment APIs and adapters use this same instance. */
export class AttachmentManager implements TicketHost, StreamHost, AttachmentHost {
  /** @internal */ readonly clock: AttachmentClock;
  /** @internal */ readonly sessionId = uuid();
  /** @internal */ readonly nonce = uuid();
  /** @internal */ readonly chunkBytes = CHUNK;
  /** @internal */ readonly manifestTimeoutMs: number;
  /** @internal */ readonly finalizationTimeoutMs: number;
  private readonly config: AttachmentManagerOptions;
  private readonly enabled: boolean;
  private session?: AttachmentSession;
  private sessionLifetime = 0;
  private bootStatus: AttachmentRuntimeState = "bootstrap_pending";
  private readonly bootController?: AbortController;
  private readonly boot: Promise<boolean>;
  private readonly wire?: AttachmentTransportV1;
  /** A four-method transport: no renewal, manifests or imports (warned once at construction). */
  private readonly legacyWire: boolean = false;
  private readonly lanes = new Map<string, Lane>();
  private order: Lane[] = [];
  private cursor = 0;
  private queued = 0;
  private readonly active = new Set<CoreJob>();
  private readonly correlations = new WeakMap<object, AttachmentCorrelation>();
  private readonly listeners = new Set<() => void>();
  private readonly reservations = new Map<number, Reservation>();
  private nextReservation = 0;
  private readonly streams = new Set<StreamState & StreamExtra>();
  private readonly recordings = new Map<string, AttachmentRecording>();
  private recordingSlots = 0;
  private bytes = 0;
  private dropped = 0;
  private epoch = 0;
  private closed = false;
  private stoppingFlag = false;
  private closedReason: AttachmentRuntimeState = "incomplete";
  private shutdownPromise?: Promise<AttachmentFlushResult>;
  private renewal = 0;
  private renewing = false;
  private nextRenewalAt = 0;
  private renewalFailures = 0;
  private renewCheckPending = false;
  private registry?: Finalizer;
  private readonly pacer: TokenBucket;
  private readonly maxBytes: number;
  private readonly maxDescriptors: number;
  private readonly workers: number;
  private readonly requestTimeout: number;
  private readonly shutdownTimeout: number;
  private readonly contentTimeout: number;
  private readonly syncCopyBytes: number;
  private readonly ticketTtl: number;
  private readonly streamIdleTtl: number;

  constructor(options: AttachmentManagerOptions = {}) {
    this.config = { ...options, traceConfig: { ...options.traceConfig } };
    this.clock = options.clock ?? defaultClock;
    this.maxBytes = bounded(options.maxOwnedBytes, MAX_MEMORY, MAX_MEMORY);
    this.maxDescriptors = bounded(options.maxDescriptors, 128, 128);
    this.workers = bounded(options.workers, 2, 2);
    this.requestTimeout = bounded(options.requestTimeoutMs, 2000, 30000);
    this.shutdownTimeout = bounded(options.shutdownTimeoutMs, 5000, 30000);
    this.contentTimeout = bounded(options.contentTimeoutMs, 120000, 600000);
    this.manifestTimeoutMs = bounded(options.manifestTimeoutMs, 10000, 30000);
    this.syncCopyBytes = bounded(options.syncCopyBytes, 8 * MiB, 32 * MiB);
    this.ticketTtl = bounded(options.ticketTtlMs, 10000, 30000);
    this.streamIdleTtl = bounded(options.streamIdleTtlMs, 60000, 300000);
    this.finalizationTimeoutMs = bounded(options.finalizationTimeoutMs, 30000, 30000);
    this.pacer = new TokenBucket(() => this.session?.limits.upload_bytes_per_second ?? 0, this.clock);
    // Resolved ONCE (design 8.1): enabling later means building a new provider.
    this.enabled = attachmentEnabled(this.config, this.env());
    if (!this.enabled) { this.bootStatus = "disabled"; this.boot = Promise.resolve(false); return; }
    this.bootController = new AbortController();
    this.wire = attachmentWireTransport(options.transport);
    this.legacyWire = attachmentIsLegacyWire(this.wire);
    this.boot = this.bootstrap();
  }
  private env(): Readonly<Record<string, string | undefined>> {
    try { return this.config.environment?.() ?? {}; }
    catch { return { FI_ATTACHMENTS_ENABLED: "false" }; }
  }
  private async bootstrap(): Promise<boolean> {
    try {
      if (!this.wire) { this.bootStatus = "endpoint_unconfigured"; return false; }
      if (typeof this.config.projectName !== "string" || !this.config.projectName || this.config.projectName.length > 255 ||
        typeof this.config.projectType !== "string" || !this.config.projectType || this.config.projectType.length > 64 ||
        (this.config.workspaceId !== undefined && !validAttachmentUuid(this.config.workspaceId))) { this.bootStatus = "scope_unresolved"; return false; }
      const request = { schema_versions: [1] as [1], project_name: this.config.projectName, project_type: this.config.projectType,
        capture_session_id: this.sessionId, correlation_nonce: this.nonce,
        ...(this.config.workspaceId ? { workspace_id: this.config.workspaceId } : {}) };
      const key = attachmentIdempotencyKey("bootstrap", this.sessionId, 0);
      const session = await this.attempt(signal => this.wire!.bootstrap(request, key, signal), this.bootController!.signal);
      if (!validAttachmentSession(session, this.sessionId, this.clock.wall())) throw attachmentWireRejected("AttachmentSession", new AttachmentTransportError("unsupported_version"));
      if (this.config.workspaceId && session.scope.workspace_id !== this.config.workspaceId) throw new AttachmentTransportError("scope_unresolved");
      if (this.closed) return false;
      // Detached bounded wire object: a caller/transport cannot later loosen policy through mutation.
      this.session = JSON.parse(JSON.stringify(session));
      this.sessionLifetime = Math.max(0, Date.parse(session.expires_at) - this.clock.wall());
      this.bootStatus = "ready";
      return true;
    } catch (error) {
      const code = error instanceof AttachmentTransportError ? error.code : "source_unavailable";
      this.bootStatus = code === "credential_scope_unsupported" || code === "policy_denied" ? "credential_scope_unsupported" :
        code === "scope_unresolved" || code === "not_found" ? "scope_unresolved" :
        code === "unsupported_version" || code === "endpoint_unconfigured" ? code : "source_unavailable";
      return false;
    }
  }
  /** Explicit startup wait; setup and inference do not await it. */
  async ready(timeoutMs = 5000): Promise<boolean> {
    if (!this.enabled) return false;
    return this.deadline(this.boot, bounded(timeoutMs, 5000, 30000), false);
  }
  /** Snapshot (cached per span) for later or late capture. Stamping happens only at an ADMITTED capture (design 0.8). */
  correlation(spanOrContext?: AttachmentSpan): AttachmentCorrelation {
    if (!spanOrContext) return new AttachmentCorrelation();
    let result = this.correlations.get(spanOrContext);
    if (!result) { result = new AttachmentCorrelation(spanOrContext); this.correlations.set(spanOrContext, result); }
    return result;
  }

  // --- state and admission ------------------------------------------------------------------------
  /** @internal */
  currentSession(): AttachmentSession | undefined { return this.session; }
  /** @internal */
  policyEpoch(): number { return this.epoch; }
  /** @internal */
  stopping(): boolean { return this.stoppingFlag || this.closed; }
  /** @internal */
  runtimeState(): AttachmentRuntimeState {
    if (this.closed) return this.closedReason;
    if (this.stoppingFlag) return "incomplete";
    if (this.bootStatus === "ready" && this.session && this.clock.wall() >= Date.parse(this.session.expires_at) - SKEW) return "expired";
    return this.bootStatus;
  }
  /** @internal Design 1.4 steps 1-8 against the CURRENT policy epoch. Never touches a source. */
  recheck(facts: AdmissionFacts, options: { ignoreSessionExpiry?: boolean; admission?: boolean } = {}): AttachmentAdmission | undefined {
    const env = this.env();
    if (!this.enabled || !attachmentEnabled(this.config, env)) return "disabled";
    if (this.closed) return this.closedReason as AttachmentAdmission;
    if (options.admission && (this.stoppingFlag || this.shutdownPromise)) return "incomplete";
    const state = this.runtimeState();
    if (state === "expired") { if (!options.ignoreSessionExpiry) return "expired"; }
    else if (state !== "ready" && !(state === "incomplete" && !options.admission)) return state as AttachmentAdmission;
    if (!this.session) return "bootstrap_pending";
    if (options.admission) { try { if (isTracingSuppressed(context.active())) return "policy_denied"; } catch { return "policy_denied"; } }
    if (facts.purpose !== undefined && !PURPOSES.includes(facts.purpose)) return "policy_denied";
    const capture = { ...facts.capture, newRecording: facts.operation === "create_recording" || facts.capture?.newRecording === true };
    for (const key of Object.keys(facts.capture ?? {})) {
      const value = (facts.capture as Record<string, unknown>)[key];
      if (value !== undefined && typeof value !== "boolean") return "policy_denied";
    }
    const denied = evaluateAttachmentPolicy(this.config, this.config.traceConfig ?? {}, this.session.policy, facts.sourceKind,
      { mimeType: facts.mimeType, direction: facts.direction, purpose: facts.purpose, capture }, env);
    if (denied) return denied;
    if (!QUALIFIED.has(facts.sourceKind) && !(facts.importSource && (facts.sourceKind === "ProviderFile" || facts.sourceKind === "RemoteUrl"))) return "source_unavailable";
    return undefined;
  }
  private resolveCorrelation(target: AttachmentTarget): AttachmentCorrelation | undefined {
    try {
      if (target instanceof AttachmentCorrelation) return target;
      if (target === undefined) {
        // The current span is read ONCE at call time.
        const span = trace.getSpan(context.active());
        return span ? this.correlation(span) : undefined;
      }
      return this.correlation(target);
    } catch { return undefined; }
  }
  private descriptors(): number { return this.queued + this.active.size + this.reservations.size + this.streams.size + this.recordings.size; }
  private uploadCap(mimeType: string, sourceKind: AttachmentSourceKind): number {
    const limit = Math.min(this.session?.limits.max_upload_bytes ?? 0, 50 * MiB);
    const kind = attachmentModality(mimeType) === "image" ? Math.min(limit, 20 * MiB) : limit;
    return sourceKind === "OwnedRecordingFile" ? kind : Math.min(kind, this.maxBytes);
  }
  /** @internal */
  reserveBytes(bytes: number): boolean {
    if (!(bytes >= 0) || this.bytes + bytes > this.maxBytes) return false;
    this.bytes += bytes; return true;
  }
  /** @internal */
  releaseBytes(bytes: number): void { if (bytes > 0) this.bytes = Math.max(0, this.bytes - bytes); }
  /** @internal */
  countDrop(): void { this.dropped++; }
  /** @internal */
  notify(): void { for (const notify of this.listeners) notify(); }
  /** @internal */
  recordingClosed(id: string): void { this.recordings.delete(id); this.notify(); }
  private validFields(fields: AttachmentFields): boolean {
    if (fields.role !== undefined && !ROLES.includes(fields.role)) return false;
    for (const [value, max] of [[fields.messageIndex, 65535], [fields.partIndex, 65535], [fields.channel, 31]] as const) {
      if (value !== undefined && (!Number.isInteger(value) || value < 0 || value > max)) return false;
    }
    return [fields.conversationId, fields.turnId, fields.toolCallId].every(value =>
      value === undefined || typeof value === "string" && value.trim() === value && TOKEN.test(value));
  }
  private validOptions(options: AttachmentOptions): boolean {
    if (options.purpose !== undefined && !PURPOSES.includes(options.purpose)) return false;
    return this.validFields(options);
  }
  private finalizer(): Finalizer | undefined {
    const Registry = (globalThis as unknown as { FinalizationRegistry?: FinalizerConstructor }).FinalizationRegistry;
    if (!this.registry && typeof Registry === "function") this.registry = new Registry((release: () => void) => { try { release(); } catch { /* bounded */ } });
    return this.registry;
  }
  /** Lazy sweep at every entry point: no timers are armed for tickets or streams (design 1.5, 6.3). */
  private sweep(): void {
    const now = this.clock.now();
    for (const [id, reservation] of this.reservations) if (reservation.expiresAt <= now) this.releaseReservation(id);
    for (const state of this.streams) if (state.open && now - state.lastWrite > state.idleTtl) this.streamAbort(state, "incomplete");
  }
  private maybeRenew(): void {
    const session = this.session;
    // A legacy wire has no renewal route: retrying it would only spin; the session runs to `expired` (warned at construction).
    if (!session || this.renewing || this.closed || this.stoppingFlag || this.bootStatus !== "ready" || !this.wire || this.legacyWire) return;
    const now = this.clock.wall(); const expires = Date.parse(session.expires_at);
    if (expires - now > Math.max(60000, 0.2 * this.sessionLifetime)) return;
    if (this.clock.now() < this.nextRenewalAt) return;
    this.renewing = true;
    const sequence = this.renewal + 1;
    void (async () => {
      try {
        const next = await this.attempt(signal => this.wire!.renewSession(session, sequence, signal), this.bootController!.signal, () => undefined, 3);
        if (!validAttachmentSession(next, this.sessionId, this.clock.wall())) throw attachmentWireRejected("AttachmentSession", new AttachmentTransportError("unsupported_version"));
        const scope = (s: AttachmentSession) => `${s.scope.organization_id}/${s.scope.workspace_id}/${s.scope.project_id}`;
        if (scope(next) !== scope(session)) { this.invalidate("scope_unresolved" as "deleted"); return; }
        if (this.session !== session || this.closed) return;
        const changed = JSON.stringify(next.policy) !== JSON.stringify(session.policy);
        this.renewal = sequence;
        this.session = JSON.parse(JSON.stringify(next));
        this.sessionLifetime = Math.max(0, Date.parse(next.expires_at) - this.clock.wall());
        this.renewalFailures = 0; this.nextRenewalAt = this.clock.now() + 30000;
        if (changed) { this.epoch++; this.cancelDenied(); }
      } catch {
        this.renewalFailures++;
        this.nextRenewalAt = this.clock.now() + Math.min(30000, 1000 * 2 ** Math.min(this.renewalFailures, 5));
      } finally { this.renewing = false; }
    })();
  }
  private enter(): void { this.sweep(); this.maybeRenew(); }
  /** @internal Deduped deferred renewal check for hot paths such as recording `tryWrite` (design 3.1). */
  touch(): void {
    if (this.renewCheckPending || !this.enabled) return;
    this.renewCheckPending = true;
    void Promise.resolve().then(() => { this.renewCheckPending = false; this.maybeRenew(); });
  }
  private preflightFacts(options: AttachmentPreflightOptions, importSource = false): AdmissionFacts {
    return { mimeType: options.mimeType, direction: options.direction, purpose: options.purpose,
      capture: options.capture ? { ...options.capture } : undefined, operation: options.operation ?? "observe_consumed",
      sourceKind: options.sourceKind ?? "Bytes", importSource };
  }
  private admitTarget(target: AttachmentTarget, facts: AdmissionFacts): { reason?: AttachmentAdmission; correlation?: AttachmentCorrelation; identity?: { traceId: string; spanId: string } } {
    if (typeof facts.mimeType !== "string" || !["input", "output", "mixed"].includes(facts.direction) ||
      !["observe_consumed", "import_existing", "create_recording"].includes(facts.operation)) {
      const first = this.recheck(facts, { admission: true });
      return { reason: first ?? "policy_denied" };
    }
    const reason = this.recheck(facts, { admission: true });
    if (reason) return { reason };
    const correlation = this.resolveCorrelation(target);
    const identity = correlation?.snapshot();
    if (!correlation || !identity) return { reason: "correlation_unavailable" };
    if (!identity.sampled) return { reason: "unsampled" };
    return { correlation, identity };
  }
  /** @internal */
  deniedHandle(reason: AttachmentAdmission): AttachmentHandle { return handle(reason).value; }
  /** @internal */
  deniedStream(reason: AttachmentAdmission): AttachmentStream {
    const denied = handle(reason).value;
    return new AttachmentConsumedStream(undefined, { reason, chunks: [], length: 0, maxBytes: 0, reserved: 0, lastWrite: 0, idleTtl: 0, epoch: 0, open: false, handle: denied });
  }

  /**
   * Full deny-wins check BEFORE any decode, read, copy, file open, provider call or stream observer (design section 1).
   * Returns a single-use ticket holding a descriptor and a byte budget.
   */
  preflight(target: AttachmentTarget, options: AttachmentPreflightOptions): AttachmentAdmissionTicket {
    const denied = (reason: AttachmentAdmission) => new AttachmentTicket(this, reason, 0, undefined, undefined, -1, 0);
    try {
      this.enter();
      const facts = this.preflightFacts(options ?? ({} as AttachmentPreflightOptions));
      const { reason, correlation } = this.admitTarget(target, facts);
      if (reason) return denied(reason);
      const maxBytes = options.maxBytes;
      if (!Number.isInteger(maxBytes) || maxBytes <= 0 || maxBytes > this.uploadCap(facts.mimeType, facts.sourceKind)) return denied("too_large");
      const reserve = facts.sourceKind === "OwnedRecordingFile" ? CHUNK : maxBytes;
      if (this.descriptors() >= this.maxDescriptors || this.bytes + reserve > this.maxBytes) return denied("queue_full");
      const ttl = bounded(options.ttlMs, this.ticketTtl, 30000);
      const id = ++this.nextReservation; const expiresAt = this.clock.now() + ttl;
      this.bytes += reserve; this.reservations.set(id, { bytes: reserve, expiresAt });
      const ticket = new AttachmentTicket(this, "queued", maxBytes, correlation, facts, id, expiresAt);
      this.finalizer()?.register(ticket, () => this.releaseReservation(id), ticket);
      return ticket;
    } catch { return denied("source_unavailable"); }
  }
  /** @internal */
  releaseReservation(id: number): void {
    const reservation = this.reservations.get(id);
    if (!reservation) return;
    this.reservations.delete(id); this.releaseBytes(reservation.bytes); this.notify();
  }
  private takeTicket(ticket: AttachmentTicket): { reason?: AttachmentAdmission; reservation?: Reservation } {
    ticket.used = true;
    try { this.registry?.unregister(ticket); } catch { /* bounded */ }
    this.sweep();
    const reservation = this.reservations.get(ticket.reservation);
    if (!reservation || this.clock.now() >= ticket.expiresAt) { this.releaseReservation(ticket.reservation); return { reason: "expired" }; }
    // Commit re-check against the CURRENT policy epoch; tightening denies without reading the source.
    const reason = this.recheck(ticket.facts!, { admission: true });
    if (reason) { this.releaseReservation(ticket.reservation); return { reason }; }
    this.reservations.delete(ticket.reservation);
    return { reservation };
  }
  /** @internal */
  commitTicket(ticket: AttachmentTicket, source: Extract<AttachmentSource, { kind: "Bytes" | "Blob" | "OwnedRecordingFile" }>, fields: AttachmentFields): AttachmentHandle {
    const taken = this.takeTicket(ticket);
    if (taken.reason) return handle(taken.reason).value;
    const reserved = taken.reservation!.bytes;
    const fail = (reason: AttachmentAdmission) => { this.releaseBytes(reserved); this.notify(); return handle(reason).value; };
    const facts = ticket.facts!;
    if (!source || source.kind !== facts.sourceKind) return fail("conflict" as AttachmentAdmission);
    if (!this.validFields(fields)) return fail("policy_denied");
    const read = this.readSource(source, ticket.maxBytes);
    if ("reason" in read) return fail(read.reason);
    const keep = facts.sourceKind === "OwnedRecordingFile" ? reserved : read.size;
    this.releaseBytes(reserved - keep); // the reservation shrinks to the actual length
    return this.enqueueUpload(facts, fields, ticket.correlation!, read.size, read.content, keep, read.owned);
  }
  private readSource(source: AttachmentSource, maxBytes: number): { reason: AttachmentAdmission } | { size: number; content?: AttachmentContentSource; owned?: AttachmentOwnedFileRecord } {
    if (source.kind === "Bytes") {
      const data = source.data;
      if (!(data instanceof Uint8Array)) return { reason: "source_unavailable" };
      const size = data.byteLength;
      if (!size || size > maxBytes || size > this.syncCopyBytes) return { reason: "too_large" };
      // Mutable inputs are detached synchronously, after all admission and budget checks.
      return { size, content: new Uint8Array(data) };
    }
    if (source.kind === "Blob") {
      const data = source.data;
      if (typeof Blob === "undefined" || !(data instanceof Blob)) return { reason: "source_unavailable" };
      if (!data.size || data.size > maxBytes) return { reason: "too_large" };
      return { size: data.size, content: data };
    }
    if (source.kind === "OwnedRecordingFile") {
      const record = attachmentOwnedFile(source.lease);
      if (!record || record.closed()) return { reason: "source_unavailable" };
      const size = record.identity.size;
      if (!size || size > maxBytes) return { reason: "too_large" };
      return { size, owned: record };
    }
    return { reason: "source_unavailable" };
  }
  private uploadRequest(facts: AdmissionFacts, fields: AttachmentFields, identity: { traceId: string; spanId: string }, ids: { attachmentId: string; occurrenceId: string }, size: number): AttachmentUploadRequest {
    return { schema_version: 1, attachment_id: ids.attachmentId, occurrence_id: ids.occurrenceId,
      association: { capture_session_id: this.sessionId, correlation_nonce: this.nonce, parent: { trace_id: attachmentTraceUuid(identity.traceId), span_id: identity.spanId } },
      policy_version: this.session!.policy.version, deletion_generation: 0, mime_type: facts.mimeType,
      modality: attachmentModality(facts.mimeType)!, direction: facts.direction, source_kind: facts.sourceKind, declared_bytes: size,
      ...(facts.purpose ? { purpose: facts.purpose } : {}),
      ...(fields.messageIndex === undefined ? {} : { message_index: fields.messageIndex }),
      ...(fields.partIndex === undefined ? {} : { part_index: fields.partIndex }),
      ...(fields.conversationId ? { conversation_id: fields.conversationId } : {}),
      ...(fields.turnId ? { turn_id: fields.turnId } : {}),
      ...(fields.toolCallId ? { tool_call_id: fields.toolCallId } : {}) };
  }
  private reference(request: AttachmentUploadRequest, fields: AttachmentFields): AttachmentReference {
    // role and channel are display hints on the live span only; UploadRequest never carries them (design 7).
    return { attachment_id: request.attachment_id, occurrence_id: request.occurrence_id, direction: request.direction, modality: request.modality,
      mime_type: request.mime_type, ...(request.purpose ? { purpose: request.purpose } : {}),
      ...(fields.messageIndex === undefined ? {} : { message_index: fields.messageIndex }),
      ...(fields.partIndex === undefined ? {} : { part_index: fields.partIndex }),
      ...(fields.role ? { role: fields.role } : {}), ...(fields.channel === undefined ? {} : { channel: fields.channel }),
      ...(fields.conversationId ? { conversation_id: fields.conversationId } : {}),
      ...(fields.turnId ? { turn_id: fields.turnId } : {}),
      ...(fields.toolCallId ? { tool_call_id: fields.toolCallId } : {}) };
  }
  private enqueueUpload(facts: AdmissionFacts, fields: AttachmentFields, correlation: AttachmentCorrelation, size: number,
    content: AttachmentContentSource | undefined, bytes: number, owned?: AttachmentOwnedFileRecord, project = true): AttachmentHandle {
    const result = handle("queued");
    const identity = correlation.snapshot()!;
    const request = this.uploadRequest(facts, fields, identity, result.value, size);
    const body = JSON.stringify(request);
    const controller = new AbortController();
    let data = content;
    const job: CoreJob = {
      lane: request.occurrence_id, bytes, controller,
      denied: () => this.recheck(facts),
      fail: reason => { data = undefined; result.finish(reason); },
      run: async signal => {
        let lease: AttachmentOwnedLease | undefined;
        try {
          let source = data;
          if (owned) {
            lease = await raceAbort(owned.open(), signal);
            const opened = lease;
            source = { size: opened.size, read: (offset, length) => opened.read(offset, length), check: () => opened.check() };
          }
          if (!source) { result.finish("incomplete"); return; }
          const outcome = await this.runUpload({ body, occurrenceId: request.occurrence_id, declaredBytes: size, source, recheck: () => this.recheck(facts, { ignoreSessionExpiry: true }) }, signal);
          result.finish(outcome.status);
          if (outcome.status !== "validating") this.dropped++;
        } catch (error) { result.finish(codeOf(error)); this.dropped++; }
        finally { data = undefined; if (lease) await lease.close().catch(() => undefined); }
      },
    };
    this.enqueue(job);
    if (project) correlation.project(this.reference(request, fields), this.sessionId, this.nonce);
    return result.value;
  }
  attach(spanOrContext: AttachmentTarget, source: AttachmentSource, options: AttachmentOptions): AttachmentHandle {
    try {
      if (this.shutdownPromise) return handle("incomplete").value;
      this.enter();
      // No access to source.data/path/url until the complete privacy gate passes.
      const kind = source.kind;
      const facts: AdmissionFacts = { mimeType: options.mimeType, direction: options.direction, purpose: options.purpose,
        capture: options.capture ? { ...options.capture } : undefined, operation: options.capture?.newRecording ? "create_recording" : "observe_consumed", sourceKind: kind };
      const { reason, correlation } = this.admitTarget(spanOrContext, facts);
      if (reason) return handle(reason).value;
      if (!this.validOptions(options)) return handle("policy_denied").value;
      if (this.descriptors() >= this.maxDescriptors) return handle("queue_full").value;
      const read = this.readSource(source, this.uploadCap(options.mimeType, kind));
      if ("reason" in read) return handle(read.reason).value;
      const reserve = kind === "OwnedRecordingFile" ? CHUNK : read.size;
      if (this.descriptors() >= this.maxDescriptors || this.bytes + reserve > this.maxBytes) return handle("queue_full").value;
      this.bytes += reserve;
      const fields: AttachmentFields = { messageIndex: options.messageIndex, partIndex: options.partIndex, role: options.role, channel: options.channel,
        conversationId: options.conversationId, turnId: options.turnId, toolCallId: options.toolCallId };
      return this.enqueueUpload(facts, fields, correlation!, read.size, read.content, reserve, read.owned);
    } catch { return handle("source_unavailable").value; }
  }

  // --- consumed streams ------------------------------------------------------------------------------
  startAttachmentStream(target: AttachmentTarget, options: AttachmentStreamOptions): AttachmentStream {
    try {
      const ticket = this.preflight(target, { mimeType: options.mimeType, direction: options.direction, maxBytes: options.maxBytes,
        purpose: options.purpose, capture: options.capture, operation: "observe_consumed", sourceKind: "Bytes" });
      return ticket.startStream(options);
    } catch { return this.deniedStream("source_unavailable"); }
  }
  /** @internal */
  commitStream(ticket: AttachmentTicket, options: AttachmentStreamOptions): AttachmentStream {
    const taken = this.takeTicket(ticket);
    if (taken.reason) return this.deniedStream(taken.reason);
    const reserved = taken.reservation!.bytes;
    const facts = ticket.facts!;
    const fail = (reason: AttachmentAdmission) => { this.releaseBytes(reserved); this.notify(); return this.deniedStream(reason); };
    // Narrowing only: a stream cannot change MIME, direction, purpose or source kind, nor exceed the ticket budget.
    if (!options || options.mimeType !== facts.mimeType || options.direction !== facts.direction || options.purpose !== facts.purpose || facts.sourceKind !== "Bytes") return fail("conflict" as AttachmentAdmission);
    if (!Number.isInteger(options.maxBytes) || options.maxBytes <= 0 || options.maxBytes > ticket.maxBytes) return fail("too_large");
    const fields: AttachmentFields = { messageIndex: options.messageIndex, partIndex: options.partIndex, role: options.role, channel: options.channel,
      conversationId: options.conversationId, turnId: options.turnId, toolCallId: options.toolCallId };
    if (!this.validFields(fields)) return fail("policy_denied");
    const keep = options.maxBytes; this.releaseBytes(reserved - keep);
    const state: StreamState & StreamExtra = { reason: "queued", attachmentId: uuid(), occurrenceId: uuid(), chunks: [], length: 0, maxBytes: options.maxBytes,
      reserved: keep, lastWrite: this.clock.now(), idleTtl: bounded(options.idleTtlMs, this.streamIdleTtl, 300000), epoch: this.epoch, open: true,
      facts, fields, correlation: ticket.correlation!, purpose: options.purpose };
    this.streams.add(state);
    const stream = new AttachmentConsumedStream(this, state);
    this.finalizer()?.register(stream, () => { if (state.open) this.streamAbort(state, "incomplete"); }, state);
    return stream;
  }
  /** @internal */
  streamWrite(state: StreamState, chunk: Uint8Array): boolean {
    const s = state as StreamState & StreamExtra;
    if (!s.open || !(chunk instanceof Uint8Array)) return false;
    if (s.epoch !== this.epoch || this.closed) {
      const reason = this.recheck(s.facts);
      if (reason) { this.streamAbort(s, "policy_denied"); return false; }
    }
    if (s.length + chunk.byteLength > s.maxBytes) { this.streamAbort(s, "too_large"); return false; }
    // Copy in bounded slices; the caller may reuse a BYOB buffer immediately.
    for (let offset = 0; offset < chunk.byteLength; offset += CHUNK) s.chunks.push(chunk.slice(offset, Math.min(chunk.byteLength, offset + CHUNK)));
    s.length += chunk.byteLength; s.lastWrite = this.clock.now();
    return true;
  }
  /** @internal */
  streamFinish(state: StreamState): AttachmentHandle {
    const s = state as StreamState & StreamExtra;
    if (!s.open) return s.handle ?? handle(s.reason === "queued" ? "incomplete" : s.reason).value;
    s.open = false; this.streams.delete(s);
    try { this.registry?.unregister(s); } catch { /* bounded */ }
    const chunks = s.chunks; s.chunks = [];
    const release = (reason: AttachmentAdmission) => { this.releaseBytes(s.reserved); s.reserved = 0; s.reason = reason; s.handle = handle(reason).value; this.notify(); return s.handle; };
    if (!s.length) return release("incomplete");
    const reason = this.recheck(s.facts);
    if (reason) return release(reason);
    this.releaseBytes(s.reserved - s.length); const bytes = s.length; s.reserved = 0;
    const content = attachmentChunkedSource(new Uint8Array(0), chunks);
    s.handle = this.enqueueUploadWithIds(s, content, bytes);
    return s.handle;
  }
  private enqueueUploadWithIds(s: StreamState & StreamExtra, content: AttachmentContentSource, bytes: number): AttachmentHandle {
    const result = handle("queued");
    const ids = { attachmentId: s.attachmentId!, occurrenceId: s.occurrenceId! };
    const identity = s.correlation.snapshot()!;
    const request = this.uploadRequest(s.facts, s.fields, identity, ids, bytes);
    const body = JSON.stringify(request);
    let data: AttachmentContentSource | undefined = content;
    const value = Object.freeze({ attachmentId: ids.attachmentId, occurrenceId: ids.occurrenceId, admission: "queued" as AttachmentAdmission, completion: result.value.completion });
    this.enqueue({ lane: ids.occurrenceId, bytes, controller: new AbortController(), denied: () => this.recheck(s.facts),
      fail: reason => { data = undefined; result.finish(reason); },
      run: async signal => {
        try {
          if (!data) { result.finish("incomplete"); return; }
          const outcome = await this.runUpload({ body, occurrenceId: ids.occurrenceId, declaredBytes: bytes, source: data, recheck: () => this.recheck(s.facts, { ignoreSessionExpiry: true }) }, signal);
          result.finish(outcome.status); if (outcome.status !== "validating") this.dropped++;
        } finally { data = undefined; }
      } });
    // Projected only at finish, only within the cap, only while the span still records.
    s.correlation.project(this.reference(request, s.fields), this.sessionId, this.nonce);
    return value;
  }
  /** @internal */
  streamAbort(state: StreamState, reason: "incomplete" | "policy_denied" | "too_large"): void {
    if (!state.open) return;
    state.open = false; this.streams.delete(state as StreamState & StreamExtra);
    try { this.registry?.unregister(state); } catch { /* bounded */ }
    state.chunks = []; state.length = 0;
    this.releaseBytes(state.reserved); state.reserved = 0;
    state.reason = reason; state.handle = handle(reason).value;
    this.notify();
  }

  // --- recordings ------------------------------------------------------------------------------------
  private recordingFacts(options: AttachmentRecordingOptions, direction: "input" | "output" | "mixed"): AdmissionFacts {
    return { mimeType: "audio/wav", direction, purpose: options.purpose ?? "conversation_recording",
      capture: options.capture ? { ...options.capture } : undefined, operation: options.operation, sourceKind: "Bytes" };
  }
  private trackDenials(options: AttachmentRecordingOptions, admission = true): { reason?: AttachmentAdmission; tracks?: (AttachmentAdmission | undefined)[] } {
    if (options.layout !== "separate_tracks") {
      const reason = this.recheck(this.recordingFacts(options, "mixed"), { admission });
      return reason ? { reason } : { tracks: [] };
    }
    const tracks = options.tracks.map(track => this.recheck(this.recordingFacts(options, track.direction), { admission }));
    if (tracks.every(Boolean)) return { reason: tracks[0] };
    return { tracks };
  }
  private recordingLimit(): number { return Math.min(this.session?.limits.active_recordings ?? 4, 4); }
  startRecording(target: AttachmentTarget, options: AttachmentRecordingOptions): AttachmentRecordingWriter {
    const inert = (reason: AttachmentAdmission) => new AttachmentRecording({ host: this, options, admission: reason, readmit: () => ({ reason }), stamp: () => undefined, close: () => undefined });
    try {
      this.enter();
      if (!attachmentRecordingShape(options)) {
        const first = this.recheck(this.recordingFacts(options ?? ({} as AttachmentRecordingOptions), "mixed"), { admission: true });
        return inert(first ?? "policy_denied");
      }
      const pending = this.enabled && attachmentEnabled(this.config, this.env()) && !this.closed && !this.stoppingFlag && this.runtimeState() === "bootstrap_pending";
      let trackDenials: (AttachmentAdmission | undefined)[] | undefined;
      if (!pending) {
        const evaluated = this.trackDenials(options);
        if (evaluated.reason) return inert(evaluated.reason);
        trackDenials = evaluated.tracks;
      }
      // Fail at start, not at the manifest: a legacy wire cannot publish, so no bytes are retained or uploaded.
      if (this.legacyWire) return inert("unsupported_version");
      const correlation = this.resolveCorrelation(target);
      const identity = correlation?.snapshot();
      if (!correlation || !identity) return inert("correlation_unavailable");
      if (!identity.sampled) return inert("unsampled");
      if (this.descriptors() >= this.maxDescriptors) return inert("queue_full");
      if (!pending && this.recordingSlots >= this.recordingLimit()) return inert("queue_full");
      let slot = false;
      const take = () => { if (!slot) { slot = true; this.recordingSlots++; } };
      if (!pending) take();
      const writer: AttachmentRecording = new AttachmentRecording({ host: this, options, identity, admission: pending ? "bootstrap_pending" : "queued", trackDenials,
        readmit: () => {
          const again = this.trackDenials(options, false);
          if (again.reason) return { reason: again.reason };
          if (!slot && this.recordingSlots >= this.recordingLimit()) return { reason: "queue_full" };
          take();
          return { reason: "queued", trackDenials: again.tracks };
        },
        stamp: () => correlation.prepare(this.sessionId, this.nonce),
        close: () => { if (slot) { slot = false; this.recordingSlots--; } } });
      if (writer.reason === "queued" || writer.reason === "bootstrap_pending") this.recordings.set(writer.recordingId, writer);
      return writer;
    } catch { return inert("source_unavailable"); }
  }

  // --- owned recording files ---------------------------------------------------------------------------
  attachOwnedRecording(target: AttachmentTarget, source: { kind: "OwnedRecordingFile"; lease: AttachmentOwnedFileRef }, options: AttachmentOwnedRecordingOptions): AttachmentOwnedRecordingHandle {
    const denied = (reason: AttachmentAdmission): AttachmentOwnedRecordingHandle => {
      const completion = Promise.resolve(Object.freeze({ status: reason }) as AttachmentCompletion);
      return Object.freeze({ tier: "denied" as const, reason, completion, wait: () => completion, cancel: () => undefined });
    };
    try {
      this.enter();
      const record = source && source.kind === "OwnedRecordingFile" ? attachmentOwnedFile(source.lease) : undefined;
      const purpose = options?.purpose ?? "conversation_recording";
      const facts: AdmissionFacts = { mimeType: options?.mimeType, direction: options?.direction, purpose, capture: options?.capture ? { ...options.capture } : undefined,
        operation: "import_existing", sourceKind: "OwnedRecordingFile" };
      const { reason, correlation, identity } = this.admitTarget(target, facts);
      if (reason) return denied(reason);
      if (!record || record.closed()) return denied("source_unavailable");
      if (purpose !== "conversation_recording" && purpose !== "turn_recording") return denied("policy_denied");
      if (options.conversationId !== undefined && !TOKEN.test(options.conversationId)) return denied("policy_denied");
      const size = record.identity.size;
      const singleCap = Math.min(this.session!.limits.max_upload_bytes, 50 * MiB);
      const wav = options.mimeType === "audio/wav" && (size > singleCap || !!options.wavTracks);
      if (!Number.isInteger(size) || size <= 0 || (!wav && size > singleCap)) return denied("too_large");
      if (this.descriptors() >= this.maxDescriptors || this.bytes + CHUNK > this.maxBytes) return denied("queue_full");
      if (wav && options.wavTracks && !this.validWavTracks(options.wavTracks)) return denied("policy_denied");
      if (wav && this.legacyWire) return denied("unsupported_version");
      this.bytes += CHUNK;
      return this.ownedTransfer(record, facts, correlation!, identity!, options, wav, singleCap);
    } catch { return denied("source_unavailable"); }
  }
  private validWavTracks(tracks: readonly { channels: readonly number[]; direction: string; role: string }[]): boolean {
    return Array.isArray(tracks) && tracks.length >= 1 && tracks.length <= 32 && tracks.every(t => Array.isArray(t.channels) && t.channels.length >= 1 &&
      ["input", "output", "mixed"].includes(t.direction) && ["user", "assistant", "mixed", "unknown"].includes(t.role));
  }
  private ownedTransfer(record: AttachmentOwnedFileRecord, facts: AdmissionFacts, correlation: AttachmentCorrelation, identity: { traceId: string; spanId: string },
    options: AttachmentOwnedRecordingOptions, wav: boolean, singleCap: number): AttachmentOwnedRecordingHandle {
    let tier: "single" | "wav_recording" | "denied" = wav ? "wav_recording" : "single";
    let recordingId: string | undefined;
    let resolve!: (value: AttachmentCompletion) => void;
    const completion = new Promise<AttachmentCompletion>(done => { resolve = done; });
    let settled = false;
    const finish = (status: AttachmentCompletion["status"]) => { if (!settled) { settled = true; resolve(Object.freeze({ status })); } };
    const controller = new AbortController();
    let lease: AttachmentOwnedLease | undefined;
    let reservation = CHUNK;
    const releaseSlice = () => { this.releaseBytes(reservation); reservation = 0; };
    const closeLease = async () => { const open = lease; lease = undefined; if (open) await open.close().catch(() => undefined); };
    correlation.prepare(this.sessionId, this.nonce);
    const fields: AttachmentFields = { conversationId: options.conversationId };
    const job: CoreJob = {
      lane: uuid(), bytes: 0, controller,
      denied: () => this.recheck(facts),
      fail: reason => { finish(reason); releaseSlice(); void closeLease(); },
      run: async signal => {
        try {
          lease = await raceAbort(record.open(), signal);
          if (tier === "wav_recording") {
            const layout = await this.wavLayout(lease, options, singleCap);
            if (typeof layout === "string") {
              if (layout === "single") tier = "single";
              else { tier = "denied"; finish(layout); releaseSlice(); await closeLease(); return; }
            } else {
              // Segmented recording from lease ranges; the outer job returns so it never holds a worker.
              const writer = this.ownedWavRecording(identity, correlation, options, layout, lease);
              if (typeof writer === "string") { tier = "denied"; finish(writer); releaseSlice(); await closeLease(); return; }
              recordingId = writer.recordingId;
              const openLease = lease; lease = undefined;
              void writer.completeFile(controller.signal).then(status => finish(status), () => finish("incomplete"))
                .finally(() => { releaseSlice(); void openLease.close().catch(() => undefined); });
              return;
            }
          }
          const opened = lease;
          const request = this.uploadRequest({ ...facts }, fields, identity, { attachmentId: uuid(), occurrenceId: uuid() }, opened.size);
          const outcome = await this.runUpload({ body: JSON.stringify(request), occurrenceId: request.occurrence_id, declaredBytes: opened.size,
            source: { size: opened.size, read: (o, n) => opened.read(o, n), check: () => opened.check() }, recheck: () => this.recheck(facts, { ignoreSessionExpiry: true }) }, signal);
          finish(outcome.status);
          releaseSlice(); await closeLease();
        } catch (error) { finish(codeOf(error)); releaseSlice(); await closeLease(); }
      },
    };
    this.enqueue(job);
    const cancel = () => { controller.abort(); finish("incomplete"); };
    const value: AttachmentOwnedRecordingHandle = {
      get tier() { return tier; },
      reason: "queued",
      get recordingId() { return recordingId; },
      completion,
      wait: (timeoutMs?: number) => new Promise<AttachmentCompletion>(done => {
        const timer = setTimeout(() => { cancel(); done(Object.freeze({ status: "incomplete" as const })); }, bounded(timeoutMs, this.finalizationTimeoutMs, 30000));
        void completion.then(result => { clearTimeout(timer); done(result); });
      }),
      cancel,
    };
    return value;
  }
  /** Parses at most 4 KiB of a WAV header through the lease, after admission (design 4.6). */
  private async wavLayout(lease: AttachmentOwnedLease, options: AttachmentOwnedRecordingOptions, singleCap: number):
    Promise<{ rate: number; channels: number; dataOffset: number; dataBytes: number; layout: AttachmentRecordingOptions["layout"]; tracks: AttachmentRecordingOptions["tracks"] } | "single" | AttachmentAdmission> {
    const fallback = lease.size <= singleCap ? "single" as const : "too_large" as const;
    const head = await lease.read(0, Math.min(4096, lease.size));
    const view = new DataView(head.buffer, head.byteOffset, head.byteLength);
    const tag = (offset: number) => String.fromCharCode(...head.subarray(offset, offset + 4));
    if (head.byteLength < 44 || tag(0) !== "RIFF" || tag(8) !== "WAVE") return fallback;
    let offset = 12; let fmt: { format: number; channels: number; rate: number; bits: number; sub?: string } | undefined;
    while (offset + 8 <= head.byteLength) {
      const id = tag(offset); const size = view.getUint32(offset + 4, true); const body = offset + 8;
      if (id === "fmt ") {
        if (size < 16 || body + size > head.byteLength) return fallback;
        const format = view.getUint16(body, true);
        fmt = { format, channels: view.getUint16(body + 2, true), rate: view.getUint32(body + 4, true), bits: view.getUint16(body + 14, true) };
        if (format === 0xfffe) {
          if (size < 40) return fallback;
          fmt.sub = Array.from(head.subarray(body + 24, body + 40)).map(b => b.toString(16).padStart(2, "0")).join("");
        }
      } else if (id === "data") {
        if (!fmt) return fallback;
        const pcm = fmt.format === 1 || (fmt.format === 0xfffe && fmt.sub === "0100000000001000800000aa00389b71");
        if (!pcm || fmt.bits !== 16 || fmt.channels < 1 || fmt.channels > 32 || fmt.rate < 8000 || fmt.rate > 192000) return fallback;
        if (body + size !== lease.size || size % (fmt.channels * 2) !== 0 || size === 0) return fallback;
        const frames = size / (fmt.channels * 2);
        if (lease.size > Math.min(this.session!.limits.recording_bytes, 1073741824) ||
          Math.ceil(frames * 1000 / fmt.rate) > Math.min(this.session!.limits.recording_duration_ms, 7200000)) return "too_large";
        const all = Array.from({ length: fmt.channels }, (_, i) => i);
        const given = options.wavTracks;
        let layout: AttachmentRecordingOptions["layout"] = "mixed";
        let tracks: AttachmentRecordingOptions["tracks"] = [{ channels: all, direction: "mixed", role: fmt.channels === 1 ? (given?.[0]?.role ?? "unknown") : "unknown" }];
        if (fmt.channels === 2 && given && given.length === 2 && given[0].channels.length === 1 && given[0].channels[0] === 0 &&
          given[1].channels.length === 1 && given[1].channels[0] === 1) { layout = "stereo"; tracks = given.map(t => ({ channels: [...t.channels], direction: t.direction, role: t.role })); }
        return { rate: fmt.rate, channels: fmt.channels, dataOffset: body, dataBytes: size, layout, tracks };
      }
      offset = body + size + (size % 2);
    }
    return fallback;
  }
  private ownedWavRecording(identity: { traceId: string; spanId: string }, correlation: AttachmentCorrelation, options: AttachmentOwnedRecordingOptions,
    layout: { rate: number; channels: number; dataOffset: number; dataBytes: number; layout: AttachmentRecordingOptions["layout"]; tracks: AttachmentRecordingOptions["tracks"] },
    lease: AttachmentOwnedLease): AttachmentRecording | AttachmentAdmission {
    if (this.recordingSlots >= this.recordingLimit()) return "queue_full";
    this.recordingSlots++;
    let slot = true;
    const recordingOptions: AttachmentRecordingOptions = { format: { sampleRateHz: layout.rate }, layout: layout.layout, tracks: layout.tracks,
      operation: "observe_consumed", purpose: options.purpose ?? "conversation_recording", capture: options.capture, conversationId: options.conversationId, windowSegments: 1 };
    const writer = new AttachmentRecording({ host: this, options: recordingOptions, identity, admission: "queued", sourceKind: "OwnedRecordingFile",
      readmit: () => ({ reason: "queued" }), stamp: () => correlation.prepare(this.sessionId, this.nonce),
      close: () => { if (slot) { slot = false; this.recordingSlots--; } } });
    if (writer.reason !== "queued") return writer.reason;
    const frameBytes = layout.channels * 2;
    const limits = this.session!.limits;
    const F = attachmentSegmentFrames(layout.rate, frameBytes, limits.segment_duration_ms, Math.min(limits.segment_bytes, limits.max_upload_bytes));
    const total = layout.dataBytes / frameBytes;
    const segments: RecordingFileSegment[] = [];
    for (let start = 0; start < total; start += F) {
      const end = Math.min(total, start + F); const bytes = (end - start) * frameBytes;
      const header = attachmentWavHeader(layout.channels, layout.rate, bytes); const base = layout.dataOffset + start * frameBytes;
      segments.push({ track: 0, startFrame: start, endFrame: end, source: {
        size: ATTACHMENT_WAV_HEADER_BYTES + bytes,
        read: async (offset, length) => {
          // WavSegmentSource: a synthesized header then a pread of the frame range; never materialized.
          const out = new Uint8Array(Math.min(length, ATTACHMENT_WAV_HEADER_BYTES + bytes - offset)); let written = 0;
          if (offset < ATTACHMENT_WAV_HEADER_BYTES) { const part = header.subarray(offset, Math.min(ATTACHMENT_WAV_HEADER_BYTES, offset + out.byteLength)); out.set(part); written = part.byteLength; }
          if (written < out.byteLength) {
            const from = Math.max(0, offset - ATTACHMENT_WAV_HEADER_BYTES);
            const part = await lease.read(base + from, out.byteLength - written);
            if (part.byteLength !== out.byteLength - written) throw new AttachmentTransportError("source_unavailable");
            out.set(part, written);
          }
          return out;
        },
        check: () => lease.check(),
      } });
    }
    this.recordings.set(writer.recordingId, writer);
    writer.loadFileSegments(segments, total);
    return writer;
  }

  // --- completed-recording import ---------------------------------------------------------------------
  async importCompletedRecording(target: AttachmentTarget, request: AttachmentCompletedRecordingRequest, options: AttachmentImportOptions): Promise<AttachmentImportHandle> {
    const finished = (reason: AttachmentAdmission | AttachmentCompletion["status"]): AttachmentImportHandle => {
      const completion = Promise.resolve(Object.freeze({ status: reason }) as AttachmentCompletion);
      return Object.freeze({ reason, completion, status: async () => ({ state: "failed", reason: String(reason) }) }) as AttachmentImportHandle;
    };
    try {
      this.enter();
      const direction = request?.direction ?? "mixed";
      const facts: AdmissionFacts = { mimeType: "audio/ogg", direction, purpose: "conversation_recording", capture: options?.capture ? { ...options.capture } : undefined,
        operation: "import_existing", sourceKind: "ProviderFile", importSource: true };
      const { reason, correlation, identity } = this.admitTarget(target, facts);
      // Denied preflight never calls the resolver.
      if (reason) return finished(reason);
      if (this.legacyWire) return finished("unsupported_version");
      if (!validAttachmentImportRequest(request) || !options?.resolver || typeof options.resolver.resolve !== "function") return finished("policy_denied");
      if (this.descriptors() >= this.maxDescriptors || this.bytes + 1 > this.maxBytes) return finished("queue_full");
      const id = ++this.nextReservation; this.bytes += 1; this.reservations.set(id, { bytes: 1, expiresAt: Infinity });
      const release = () => this.releaseReservation(id);
      correlation!.prepare(this.sessionId, this.nonce);
      const timeout = bounded(options.timeoutMs, 30000, 30000);
      const controller = new AbortController();
      let resolved: unknown;
      try {
        resolved = await new Promise<unknown>(resolve => {
          const timer = setTimeout(() => { controller.abort(); resolve("source_unavailable"); }, timeout);
          Promise.resolve().then(() => options.resolver.resolve({ provider: request.provider, providerRef: request.providerRef, direction }, controller.signal))
            .then(value => { clearTimeout(timer); resolve(value); }, () => { clearTimeout(timer); resolve("source_unavailable"); });
        });
      } catch { resolved = "source_unavailable"; }
      const invalid = attachmentResolvedRecordingDenial(resolved, this.session?.limits as { recording_bytes?: number; recording_duration_ms?: number } | undefined);
      if (invalid) { release(); return finished(invalid); }
      const result = resolved as import("./types").AttachmentResolvedCompletedRecording;
      const finalDirection = attachmentImportDirection(direction, result.direction);
      if (!finalDirection) { release(); return finished("policy_denied"); }
      const sourceKind: AttachmentSourceKind = result.source.kind === "ProviderFile" ? "ProviderFile" : "RemoteUrl";
      const finalFacts: AdmissionFacts = { ...facts, mimeType: result.mimeType, direction: finalDirection, sourceKind };
      const again = this.recheck(finalFacts, { admission: true });
      if (again) { release(); return finished(again); }
      const recordingId = uuid();
      const body = JSON.stringify({ mode: "completed_recording", recording: { schema_version: 1,
        association: { capture_session_id: this.sessionId, correlation_nonce: this.nonce, parent: { trace_id: attachmentTraceUuid(identity!.traceId), span_id: identity!.spanId } },
        policy_version: this.session!.policy.version, deletion_generation: 0, mime_type: result.mimeType, direction: finalDirection, source_kind: sourceKind,
        recording_id: recordingId, modality: "audio", declared_bytes: result.declaredBytes, declared_duration_ms: result.declaredDurationMs },
        source: result.source.kind === "ProviderFile" ? { kind: "ProviderFile", connection_id: result.source.connectionId, registered_output_id: result.source.registeredOutputId } :
          { kind: "RemoteUrlEnvelope", envelope_id: result.source.envelopeId } });
      if (!validAttachmentWire("RecordingImportRequest", JSON.parse(body))) { release(); return finished("unsupported_version"); }
      let state: AttachmentAdmission | Exclude<AttachmentCompletion["status"], "failed"> = "queued";
      let jobId: string | undefined;
      const done = handle("queued");
      this.reservations.delete(id); // the descriptor becomes the job
      this.enqueue({ lane: recordingId, bytes: 1, controller: new AbortController(), denied: () => this.recheck(finalFacts),
        fail: reason => { state = reason; done.finish(state); },
        run: async signal => {
          try {
            const job = await this.attempt(s => this.wire!.submitImport(this.session!, body, attachmentIdempotencyKey("import", recordingId), s), signal,
              () => { const r = this.recheck(finalFacts); if (r) throw new AttachmentTransportError(r); });
            if (!job || job.recordingId !== recordingId || !validAttachmentUuid(job.jobId) || job.state !== "importing") throw new AttachmentTransportError("unsupported_version");
            jobId = job.jobId; state = "queued"; done.finish("importing");
          } catch (error) { state = codeOf(error); done.finish(state); this.dropped++; }
        } });
      const manager = this;
      return {
        get reason() { return state; },
        recordingId,
        get jobId() { return jobId; },
        completion: done.value.completion,
        async status(timeoutMs?: number) {
          if (!jobId || !manager.session || !manager.wire) return { state: "failed", reason: String(state === "queued" ? "incomplete" : state) };
          const c = new AbortController();
          const timer = setTimeout(() => c.abort(), bounded(timeoutMs, 2000, 30000)); unref(timer);
          try { return await raceAbort(manager.wire.importStatus(manager.session, jobId, c.signal), c.signal); }
          catch (error) { return { state: "failed", reason: codeOf(error) }; }
          finally { clearTimeout(timer); }
        },
      };
    } catch { return finished("source_unavailable"); }
  }

  // --- queue, workers and transfers ----------------------------------------------------------------------
  /** @internal */
  enqueue(job: CoreJob): void {
    let lane = this.lanes.get(job.lane);
    if (!lane) { lane = new Lane(job.lane); this.lanes.set(job.lane, lane); this.order.push(lane); }
    lane.jobs.push(job); this.queued++;
    // Avoid even synchronous user transport execution in the caller's callback.
    void Promise.resolve().then(() => this.pump());
  }
  private nextLane(): Lane | undefined {
    const count = this.order.length;
    for (let i = 0; i < count; i++) {
      const index = (this.cursor + i) % count; const lane = this.order[index];
      if (!lane.busy && lane.jobs.length) { this.cursor = index + 1; return lane; }
    }
    return undefined;
  }
  private dropLane(lane: Lane): void {
    if (lane.busy || lane.jobs.length) return;
    this.lanes.delete(lane.id); this.order = this.order.filter(item => item !== lane);
    if (this.cursor > this.order.length) this.cursor = 0;
  }
  private endJob(job: CoreJob): void {
    if (job.settled) return;
    job.settled = true; this.releaseBytes(job.bytes); job.bytes = 0;
  }
  private pump(): void {
    // Worker wake is a renewal trigger (design 3.1): recording-only workloads never pass through preflight.
    this.maybeRenew();
    while (this.active.size < this.workers) {
      const lane = this.nextLane();
      if (!lane) break;
      const job = lane.jobs.shift()!; this.queued--;
      if (job.controller.signal.aborted) { job.fail("incomplete"); this.endJob(job); this.dropLane(lane); this.notify(); continue; }
      lane.busy = true; this.active.add(job);
      void (async () => { try { await job.run(job.controller.signal); } catch { job.fail("incomplete"); } })().finally(() => {
        this.endJob(job); this.active.delete(job); lane.busy = false; this.dropLane(lane);
        this.notify(); this.pump();
      });
    }
  }
  private async attempt<T>(operation: (signal: AbortSignal) => Promise<T>, parent: AbortSignal, guard: () => void = () => undefined, attempts = 3,
    timeoutMs = this.requestTimeout): Promise<T> {
    for (let attempt = 0; ; attempt++) {
      if (parent.aborted) throw new AttachmentTransportError("incomplete");
      guard();
      const controller = new AbortController();
      const abort = () => controller.abort();
      parent.addEventListener("abort", abort, { once: true });
      const timer = setTimeout(abort, timeoutMs); unref(timer);
      try {
        // Raced against the abort signal: a transport that ignores its signal can never hold shutdown.
        const result = await raceAbort(Promise.resolve().then(() => operation(controller.signal)), controller.signal);
        if (controller.signal.aborted) throw new AttachmentTransportError("incomplete", !parent.aborted);
        return result;
      } catch (raw) {
        const timedOut = controller.signal.aborted && !parent.aborted;
        const error = raw instanceof AttachmentTransportError ? (raw.code === "failed" ? new AttachmentTransportError("incomplete", raw.retryable, raw.retryAfterMs, raw.httpStatus) : raw)
          : new AttachmentTransportError("incomplete", timedOut);
        const retryable = error.retryable || (timedOut && error.code === "incomplete");
        if (!retryable || attempt + 1 >= attempts || parent.aborted) throw error;
        const backoff = (0.5 + Math.random() * 0.5) * 100 * 2 ** (attempt + 1);
        await pause(Math.max(Math.min(error.retryAfterMs, 5000), backoff), parent);
      } finally { clearTimeout(timer); parent.removeEventListener("abort", abort); }
    }
  }
  /** @internal Initialize -> content -> complete, with capability renewal and a best-effort abort (design 2.7, 3.2). */
  async runUpload(spec: UploadSpec, signal: AbortSignal): Promise<UploadOutcome> {
    const wire = this.wire!;
    const outcome: UploadOutcome = { status: "incomplete", body: spec.body, renewals: 0 };
    let request: AttachmentUploadRequest;
    try { request = JSON.parse(spec.body); } catch { outcome.status = "unsupported_version"; return outcome; }
    // Closed-schema check before any request: never a role, channel, URL, path or credential (T-CH-01).
    if (!validAttachmentWire("UploadRequest", request)) { outcome.status = "unsupported_version"; return outcome; }
    const guard = () => {
      if (signal.aborted) throw new AttachmentTransportError("incomplete");
      const reason = spec.recheck(); if (reason) throw new AttachmentTransportError(reason);
      if (!this.session || request.policy_version !== this.session.policy.version) throw new AttachmentTransportError("policy_denied");
    };
    try {
      const live = () => { guard(); if (this.runtimeState() === "expired") throw new AttachmentTransportError("expired"); };
      const upload = await this.attempt(s => wire.initialize(this.session!, spec.body, attachmentIdempotencyKey("upload", spec.occurrenceId), s), signal, live);
      if (!validAttachmentUpload(upload, request, this.clock.wall())) throw new AttachmentTransportError("unsupported_version");
      outcome.upload = upload;
    } catch (error) { outcome.status = codeOf(error); return outcome; }
    const fresh = async () => {
      if (this.clock.wall() >= Date.parse(outcome.upload!.expires_at) - SKEW && !(await this.renewUpload(outcome, signal))) throw new AttachmentTransportError("expired");
    };
    const withCapability = async <T>(op: (s: AbortSignal) => Promise<T>, timeout: number): Promise<T> => {
      for (let renewed = false; ;) {
        try { return await this.attempt(async s => { await fresh(); return op(s); }, signal, guard, 3, timeout); }
        catch (error) {
          // One renewal after a 401 on a capability route, then one more try.
          if (error instanceof AttachmentTransportError && error.httpStatus === 401 && !renewed) {
            renewed = true;
            if (await this.renewUpload(outcome, signal)) continue;
            throw new AttachmentTransportError("expired");
          }
          throw error;
        }
      }
    };
    try {
      const transferTimeout = Math.min(this.contentTimeout, (this.session?.limits.transfer_timeout_seconds ?? 600) * 1000);
      await withCapability(s => wire.putContent(this.session!, outcome.upload!, spec.source, s, this.pacer), transferTimeout);
      if (spec.beforeComplete) await spec.beforeComplete(outcome, signal);
      await withCapability(s => wire.complete(this.session!, outcome.upload!, attachmentIdempotencyKey("complete", outcome.upload!.upload_id), s), this.requestTimeout);
      guard();
      outcome.status = "validating";
    } catch (error) {
      outcome.status = codeOf(error);
      // Exactly one best-effort abort after a terminal failure that followed a successful initialize.
      const upload = outcome.upload;
      if (upload && this.session) {
        const c = new AbortController(); const timer = setTimeout(() => c.abort(), this.requestTimeout); unref(timer);
        try { await raceAbort(wire.abort(this.session, upload, attachmentIdempotencyKey("abort", upload.upload_id), c.signal), c.signal); }
        catch { /* best effort */ } finally { clearTimeout(timer); }
      }
      outcome.upload = undefined;
    }
    return outcome;
  }
  /** @internal Replay initialize with the original identity for a fresh capability (BR-1). */
  async renewUpload(outcome: UploadOutcome, signal: AbortSignal): Promise<boolean> {
    const upload = outcome.upload;
    if (!upload || !this.session || !this.wire || outcome.renewals >= 3 || signal.aborted) return false;
    outcome.renewals++;
    try {
      const next = await this.attempt(s => this.wire!.renewUpload(this.session!, upload, outcome.body, s), signal, () => undefined, 1);
      if (!next || next.upload_id !== upload.upload_id || next.attachment_id !== upload.attachment_id || next.occurrence_id !== upload.occurrence_id ||
        next.reserved_bytes !== upload.reserved_bytes || next.write_capability === upload.write_capability ||
        !(Date.parse(next.expires_at) > Date.parse(upload.expires_at))) return false;
      outcome.upload = next;
      return true;
    } catch { return false; }
  }
  /** @internal Manifest PUT authorized by a segment capability only; a 409 on a replayed revision is success (BR-5). */
  async putManifest(capability: string, recordingId: string, body: string, key: string, signal: AbortSignal, replay: boolean): Promise<AttachmentCompletion["status"] | "ok"> {
    if (!this.wire || !this.session) return "incomplete";
    let tries = 0;
    try {
      await this.attempt(async s => {
        const n = tries++;
        try { await this.wire!.putManifest(this.session!, capability, recordingId, body, key, s); }
        catch (error) { if ((n > 0 || replay) && error instanceof AttachmentTransportError && error.code === "conflict") return; throw error; }
      }, signal, () => undefined, 3, this.manifestTimeoutMs);
      return "ok";
    } catch (error) { return codeOf(error); }
  }
  private drop(job: CoreJob, reason: AttachmentCompletion["status"]): void { this.dropped++; job.fail(reason); }

  // --- policy and lifecycle ------------------------------------------------------------------------------
  /** Tightening discards all queued data and cancels transfers; re-enable never resurrects it or turns a disabled manager on. */
  updatePolicy(update: Pick<AttachmentManagerOptions, "enabled" | "traceConfig">): void {
    if (!this.enabled) return;
    if (update.enabled === false) this.config.enabled = false;
    if (update.traceConfig) this.config.traceConfig = { ...this.config.traceConfig, ...update.traceConfig };
    this.epoch++;
    this.cancelDenied();
  }
  /** A revoked capability/session/generation never resumes or retains capture data. */
  invalidate(reason: "deleted" | "expired" | "policy_denied" = "deleted"): void {
    this.closedReason = reason; this.closed = true; this.bootController?.abort(); this.epoch++;
    this.cancelDenied();
    for (const state of [...this.streams]) this.streamAbort(state, "policy_denied");
    for (const id of [...this.reservations.keys()]) this.releaseReservation(id);
    for (const writer of [...this.recordings.values()]) writer.abort();
  }
  private cancelDenied(): void {
    for (const lane of [...this.order]) {
      for (let index = lane.jobs.length - 1; index >= 0; index--) {
        const job = lane.jobs[index]; const reason = job.denied();
        if (reason) { lane.jobs.splice(index, 1); this.queued--; this.endJob(job); this.drop(job, reason); }
      }
      this.dropLane(lane);
    }
    for (const job of this.active) {
      const reason = job.denied();
      if (reason) { job.controller.abort(); this.drop(job, reason); }
    }
    for (const state of [...this.streams]) if (this.recheck(state.facts)) this.streamAbort(state, "policy_denied");
    this.notify();
  }
  private deadline<T>(promise: Promise<T>, timeout: number, fallback: T): Promise<T> {
    return new Promise(resolve => {
      // An explicit lifecycle wait owns a bounded grace period; background work stays unref'd.
      const timer = setTimeout(() => resolve(fallback), timeout);
      void promise.then(value => { clearTimeout(timer); resolve(value); }, () => { clearTimeout(timer); resolve(fallback); });
    });
  }
  async flush(timeoutMs = this.shutdownTimeout): Promise<AttachmentFlushResult> {
    if (!this.enabled) return Object.freeze({ unfinished: 0, dropped: 0 });
    this.enter();
    this.cancelDenied();
    if (this.queued || this.active.size) {
      await new Promise<void>(resolve => {
        const done = () => { clearTimeout(timer); this.listeners.delete(changed); resolve(); };
        const changed = () => { if (!this.queued && !this.active.size) done(); };
        const timer = setTimeout(done, bounded(timeoutMs, this.shutdownTimeout, 30000));
        this.listeners.add(changed); changed();
      });
    }
    return Object.freeze({ unfinished: this.queued + this.active.size + this.streams.size + this.recordings.size, dropped: this.dropped });
  }
  shutdown(timeoutMs = this.shutdownTimeout): Promise<AttachmentFlushResult> {
    if (!this.shutdownPromise) {
      // Stop new admissions, but give accepted jobs and open recordings a bounded finalization window.
      this.shutdownPromise = (async () => {
        if (!this.enabled) return Object.freeze({ unfinished: 0, dropped: 0 });
        const budget = bounded(timeoutMs, this.shutdownTimeout, 30000);
        const deadline = this.clock.now() + budget;
        this.stoppingFlag = true;
        const writers = [...this.recordings.values()];
        if (writers.length) await this.deadline(Promise.all(writers.map(writer => writer.shutdown(Math.max(1, deadline - this.clock.now())))), Math.max(1, deadline - this.clock.now()), []);
        for (const state of [...this.streams]) this.streamAbort(state, "incomplete");
        for (const id of [...this.reservations.keys()]) this.releaseReservation(id);
        const result = await this.flush(Math.max(1, Math.floor(deadline - this.clock.now())));
        this.closed = true; this.closedReason = "incomplete"; this.bootController?.abort();
        for (const lane of [...this.order]) {
          for (const job of lane.jobs.splice(0)) { this.queued--; this.endJob(job); this.drop(job, "incomplete"); }
          this.dropLane(lane);
        }
        for (const job of this.active) { job.controller.abort(); this.drop(job, "incomplete"); }
        for (const writer of [...this.recordings.values()]) writer.abort();
        this.notify();
        return Object.freeze({ unfinished: result.unfinished, dropped: this.dropped });
      })();
    }
    return this.shutdownPromise;
  }
  /** Aggregate diagnostics only; no per-source metadata, names, hashes, capabilities or exception text. */
  status(): AttachmentStatus {
    if (this.enabled) this.enter();
    return Object.freeze({ state: this.runtimeState(), queued: this.queued, active: this.active.size, openTickets: this.reservations.size,
      openStreams: this.streams.size, openRecordings: this.recordings.size, ownedBytes: this.bytes, dropped: this.dropped });
  }
}
