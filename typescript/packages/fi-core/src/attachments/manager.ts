import { v4 as uuid } from "uuid";
import { AttachmentCorrelation, attachmentTraceUuid, validAttachmentUuid } from "./correlation";
import { attachmentEnabled, attachmentModality, evaluateAttachmentPolicy } from "./policy";
import { AttachmentTransportError, validAttachmentSession, validAttachmentUpload } from "./transport-v1";
import type { AttachmentAdmission, AttachmentCompletion, AttachmentFlushResult, AttachmentHandle, AttachmentManagerOptions, AttachmentOptions, AttachmentReference, AttachmentSession, AttachmentSource, AttachmentSourceKind, AttachmentSpan, AttachmentUpload, AttachmentUploadRequest } from "./types";

// Reserve bounded transfer/read/response scratch for both workers inside the 32 MiB envelope.
const MAX_MEMORY = 32 * 1024 * 1024 - 2 * (256 * 1024 + 2 * 65536);
function bounded(value: number | undefined, fallback: number, max: number): number {
  return Number.isInteger(value) && value! > 0 ? Math.min(value!, max) : fallback;
}
function unref(timer: ReturnType<typeof setTimeout>): void { (timer as unknown as { unref?: () => void }).unref?.(); }
function handle(admission: AttachmentAdmission): { value: AttachmentHandle; finish: (status: AttachmentCompletion["status"]) => void } {
  let resolve!: (result: AttachmentCompletion) => void;
  const completion = new Promise<AttachmentCompletion>(done => { resolve = done; });
  const value = Object.freeze({ attachmentId: uuid(), occurrenceId: uuid(), admission, completion });
  const finish = (status: AttachmentCompletion["status"]) => resolve(Object.freeze({ status }));
  if (admission !== "queued") finish(admission);
  return { value, finish };
}
interface Job {
  handle: ReturnType<typeof handle>;
  data?: Uint8Array | Blob;
  size: number;
  kind: AttachmentSourceKind;
  options: AttachmentOptions;
  request: AttachmentUploadRequest;
  controller: AbortController;
  dropped?: boolean;
}
/** One per provider/runtime; explicit attachment APIs and adapters use this same instance. */
export class AttachmentManager {
  private readonly config: AttachmentManagerOptions;
  private readonly sessionId = uuid();
  private readonly nonce = uuid();
  private session?: AttachmentSession;
  private bootStatus: AttachmentAdmission = "bootstrap_pending";
  private readonly bootController = new AbortController();
  private readonly boot: Promise<boolean>;
  private readonly queue: Job[] = [];
  private readonly active = new Set<Job>();
  private readonly correlations = new WeakMap<object, AttachmentCorrelation>();
  private readonly listeners = new Set<() => void>();
  private bytes = 0;
  private dropped = 0;
  private closed = false;
  private closedReason: AttachmentAdmission = "incomplete";
  private shutdownPromise?: Promise<AttachmentFlushResult>;
  private readonly maxBytes: number;
  private readonly maxDescriptors: number;
  private readonly workers: number;
  private readonly requestTimeout: number;
  private readonly shutdownTimeout: number;

  constructor(options: AttachmentManagerOptions = {}) {
    this.config = { ...options, traceConfig: { ...options.traceConfig } };
    this.maxBytes = bounded(options.maxOwnedBytes, MAX_MEMORY, MAX_MEMORY);
    this.maxDescriptors = bounded(options.maxDescriptors, 128, 128);
    this.workers = bounded(options.workers, 2, 2);
    this.requestTimeout = bounded(options.requestTimeoutMs, 2000, 30000);
    this.shutdownTimeout = bounded(options.shutdownTimeoutMs, 5000, 30000);
    this.boot = this.bootstrap();
  }
  private env(): Readonly<Record<string, string | undefined>> {
    try { return this.config.environment?.() ?? {}; }
    catch { return { FI_ATTACHMENTS_ENABLED: "false" }; }
  }
  private async bootstrap(): Promise<boolean> {
    try {
      if (!attachmentEnabled(this.config, this.env())) { this.bootStatus = "disabled"; return false; }
      if (!this.config.transport) { this.bootStatus = "endpoint_unconfigured"; return false; }
      if (typeof this.config.projectName !== "string" || !this.config.projectName || this.config.projectName.length > 255 ||
        typeof this.config.projectType !== "string" || !this.config.projectType || this.config.projectType.length > 64 ||
        (this.config.workspaceId !== undefined && !validAttachmentUuid(this.config.workspaceId))) { this.bootStatus = "scope_unresolved"; return false; }
      const request = { schema_versions: [1] as [1], project_name: this.config.projectName, project_type: this.config.projectType,
        capture_session_id: this.sessionId, correlation_nonce: this.nonce,
        ...(this.config.workspaceId ? { workspace_id: this.config.workspaceId } : {}) };
      const session = await this.attempt(signal => this.config.transport!.bootstrap(request, signal), this.bootController.signal);
      if (!validAttachmentSession(session, this.sessionId)) throw new AttachmentTransportError("unsupported_version");
      if (this.config.workspaceId && session.scope.workspace_id !== this.config.workspaceId) throw new AttachmentTransportError("scope_unresolved");
      if (this.closed) return false;
      // Detached bounded wire object: a caller/transport cannot later loosen policy through mutation.
      this.session = JSON.parse(JSON.stringify(session));
      this.bootStatus = "queued";
      return true;
    } catch (error) { this.bootStatus = error instanceof AttachmentTransportError && error.code !== "failed" ? error.code : "source_unavailable"; return false; }
  }
  /** Explicit startup wait; setup and inference do not await it. */
  async ready(timeoutMs = 5000): Promise<boolean> {
    return this.deadline(this.boot, bounded(timeoutMs, 5000, 30000), false);
  }
  correlation(spanOrContext?: AttachmentSpan): AttachmentCorrelation {
    if (!spanOrContext) return new AttachmentCorrelation();
    let result = this.correlations.get(spanOrContext);
    if (!result) { result = new AttachmentCorrelation(spanOrContext); this.correlations.set(spanOrContext, result); }
    if (attachmentEnabled(this.config, this.env())) result.prepare(this.sessionId, this.nonce);
    return result;
  }
  private denial(kind: AttachmentSourceKind, options: AttachmentOptions): AttachmentAdmission | undefined {
    if (this.closed) return this.closedReason;
    const policy = evaluateAttachmentPolicy(this.config, this.config.traceConfig ?? {}, this.session?.policy, kind, options, this.env());
    if (policy === "bootstrap_pending" && this.bootStatus !== "queued") return this.bootStatus;
    if (policy) return policy;
    if (this.session && Date.parse(this.session.expires_at) <= Date.now()) return "expired";
    return undefined;
  }
  attach(spanOrContext: AttachmentSpan | AttachmentCorrelation, source: AttachmentSource, options: AttachmentOptions): AttachmentHandle {
    try {
      if (this.shutdownPromise) return handle("incomplete").value;
      // No access to source.data/path/url until the complete privacy gate passes.
      const kind = source.kind;
      const denied = this.denial(kind, options);
      if (denied) return handle(denied).value;
      const correlation = spanOrContext instanceof AttachmentCorrelation ? spanOrContext : this.correlation(spanOrContext);
      const identity = correlation.snapshot();
      if (!identity) return handle("correlation_unavailable").value;
      if (!identity.sampled) return handle("unsampled").value;
      if (!this.validOptions(options)) return handle("policy_denied").value;
      if (kind !== "Bytes" && kind !== "Blob") return handle("source_unavailable").value;
      if (this.queue.length + this.active.size >= this.maxDescriptors) return handle("queue_full").value;
      const data = source.data;
      const bytes = kind === "Bytes";
      if (bytes ? !(data instanceof Uint8Array) : typeof Blob === "undefined" || !(data instanceof Blob)) return handle("source_unavailable").value;
      const size = bytes ? (data as Uint8Array).byteLength : (data as Blob).size;
      const modality = attachmentModality(options.mimeType)!;
      const cap = modality === "image" ? 20 * 1024 * 1024 : modality === "audio" || modality === "video" ? 8 * 1024 * 1024 : 50 * 1024 * 1024;
      if (!size || size > Math.min(cap, this.session!.limits.max_upload_bytes, this.maxBytes)) return handle("too_large").value;
      if (this.queue.length + this.active.size >= this.maxDescriptors || this.bytes + size > this.maxBytes) return handle("queue_full").value;
      // Mutable inputs are detached synchronously, after all admission and budget checks.
      const owned = bytes ? new Uint8Array(data as Uint8Array) : data as Blob;
      const result = handle("queued");
      const snapshot: AttachmentOptions = { mimeType: options.mimeType, direction: options.direction, purpose: options.purpose,
        messageIndex: options.messageIndex, partIndex: options.partIndex, role: options.role, channel: options.channel,
        conversationId: options.conversationId, turnId: options.turnId, toolCallId: options.toolCallId,
        capture: { captureContent: options.capture?.captureContent, recordInputs: options.capture?.recordInputs, recordOutputs: options.capture?.recordOutputs,
          muted: options.capture?.muted, temporaryChat: options.capture?.temporaryChat, requested: options.capture?.requested,
          modelInput: options.capture?.modelInput, containsInput: options.capture?.containsInput, containsOutput: options.capture?.containsOutput,
          errorDetail: options.capture?.errorDetail, newRecording: options.capture?.newRecording } };
      const request: AttachmentUploadRequest = { schema_version: 1, attachment_id: result.value.attachmentId, occurrence_id: result.value.occurrenceId,
        association: { capture_session_id: this.sessionId, correlation_nonce: this.nonce, parent: { trace_id: attachmentTraceUuid(identity.traceId), span_id: identity.spanId } },
        policy_version: this.session!.policy.version, deletion_generation: 0, mime_type: snapshot.mimeType,
        modality, direction: snapshot.direction, source_kind: kind, declared_bytes: size,
        ...(snapshot.purpose ? { purpose: snapshot.purpose } : {}),
        ...(snapshot.messageIndex === undefined ? {} : { message_index: snapshot.messageIndex }),
        ...(snapshot.partIndex === undefined ? {} : { part_index: snapshot.partIndex }),
        ...(snapshot.conversationId ? { conversation_id: snapshot.conversationId } : {}),
        ...(snapshot.turnId ? { turn_id: snapshot.turnId } : {}),
        ...(snapshot.toolCallId ? { tool_call_id: snapshot.toolCallId } : {}) };
      const job: Job = { handle: result, data: owned, size, kind, options: snapshot, request, controller: new AbortController() };
      this.bytes += size;
      this.queue.push(job);
      const reference: AttachmentReference = { attachment_id: result.value.attachmentId, occurrence_id: result.value.occurrenceId,
        direction: snapshot.direction, modality, mime_type: snapshot.mimeType, ...(snapshot.purpose ? { purpose: snapshot.purpose } : {}),
        ...(snapshot.messageIndex === undefined ? {} : { message_index: snapshot.messageIndex }),
        ...(snapshot.partIndex === undefined ? {} : { part_index: snapshot.partIndex }),
        ...(snapshot.role ? { role: snapshot.role } : {}), ...(snapshot.channel === undefined ? {} : { channel: snapshot.channel }),
        ...(snapshot.conversationId ? { conversation_id: snapshot.conversationId } : {}),
        ...(snapshot.turnId ? { turn_id: snapshot.turnId } : {}),
        ...(snapshot.toolCallId ? { tool_call_id: snapshot.toolCallId } : {}) };
      correlation.project(reference, this.sessionId, this.nonce);
      // Avoid even synchronous user transport execution in the inference callback.
      void Promise.resolve().then(() => this.pump());
      return result.value;
    } catch { return handle("source_unavailable").value; }
  }
  private validOptions(options: AttachmentOptions): boolean {
    const purposes = ["conversation_recording", "turn_recording", "model_input", "model_output", "tool_result", "computer_screenshot", "diagnostic", "document"];
    if (options.purpose !== undefined && !purposes.includes(options.purpose)) return false;
    if (options.role !== undefined && !["user", "assistant", "tool", "system", "unknown"].includes(options.role)) return false;
    for (const [value, max] of [[options.messageIndex, 65535], [options.partIndex, 65535], [options.channel, 31]] as const) {
      if (value !== undefined && (!Number.isInteger(value) || value < 0 || value > max)) return false;
    }
    return [options.conversationId, options.turnId, options.toolCallId].every(value =>
      value === undefined || typeof value === "string" && value.trim() === value && /^[a-zA-Z0-9_-]{1,128}$/.test(value));
  }
  private pump(): void {
    while (this.active.size < this.workers && this.queue.length) {
      const job = this.queue.shift()!;
      this.active.add(job);
      void this.transfer(job).finally(() => {
        job.data = undefined; this.bytes -= job.size; this.active.delete(job);
        this.notify(); this.pump();
      });
    }
  }
  private check(job: Job): void {
    const denied = job.controller.signal.aborted ? "incomplete" : this.denial(job.kind, job.options);
    if (denied) throw new AttachmentTransportError(denied);
    if (job.request.policy_version !== this.session!.policy.version) throw new AttachmentTransportError("policy_denied");
  }
  private async transfer(job: Job): Promise<void> {
    try {
      const transport = this.config.transport!;
      const guard = () => this.check(job);
      guard();
      const upload = await this.attempt(signal => transport.initialize(this.session!, job.request, signal), job.controller.signal, guard);
      if (!validAttachmentUpload(upload, job.request)) throw new AttachmentTransportError("failed");
      const checkUpload = () => { guard(); if (Date.parse(upload.expires_at) <= Date.now()) throw new AttachmentTransportError("expired"); };
      // v1 cannot safely replay a partially consumed PUT. Only idempotent control operations retry.
      await this.attempt(signal => transport.upload(this.session!, upload, job.data!, signal), job.controller.signal, checkUpload, 1);
      await this.attempt(signal => transport.complete(this.session!, upload, job.request, signal), job.controller.signal, checkUpload);
      checkUpload(); job.handle.finish("validating");
    } catch (error) {
      this.drop(job, error instanceof AttachmentTransportError ? error.code : "failed");
    }
  }
  private async attempt<T>(operation: (signal: AbortSignal) => Promise<T>, parent: AbortSignal, guard: () => void = () => undefined, attempts = 3): Promise<T> {
    for (let attempt = 0; ; attempt++) {
      if (parent.aborted) throw new AttachmentTransportError("incomplete");
      guard();
      const controller = new AbortController();
      const abort = () => controller.abort();
      parent.addEventListener("abort", abort, { once: true });
      const timer = setTimeout(abort, this.requestTimeout); unref(timer);
      try {
        const result = await operation(controller.signal);
        if (controller.signal.aborted) throw new AttachmentTransportError("incomplete");
        return result;
      } catch (error) {
        if (!(error instanceof AttachmentTransportError) || !error.retryable || attempt + 1 >= attempts || parent.aborted || controller.signal.aborted) throw error;
        await this.pause(Math.max(error.retryAfterMs, 25 * 2 ** attempt + Math.random() * 25), parent);
      } finally { clearTimeout(timer); parent.removeEventListener("abort", abort); }
    }
  }
  private pause(ms: number, signal: AbortSignal): Promise<void> {
    return new Promise(resolve => {
      const done = () => { clearTimeout(timer); signal.removeEventListener("abort", done); resolve(); };
      const timer = setTimeout(done, ms); unref(timer);
      signal.addEventListener("abort", done, { once: true });
      if (signal.aborted) done();
    });
  }
  private drop(job: Job, reason: AttachmentCompletion["status"]): void {
    if (!job.dropped) { job.dropped = true; this.dropped++; job.handle.finish(reason); }
  }
  /** Tightening discards all queued data and cancels transfers; re-enable never resurrects it. */
  updatePolicy(update: Pick<AttachmentManagerOptions, "enabled" | "traceConfig">): void {
    if (update.enabled !== undefined) this.config.enabled = update.enabled;
    if (update.traceConfig) this.config.traceConfig = { ...this.config.traceConfig, ...update.traceConfig };
    this.cancelDenied();
  }
  /** A revoked capability/session/generation never resumes or retains capture data. */
  invalidate(reason: "deleted" | "expired" | "policy_denied" = "deleted"): void {
    this.closedReason = reason; this.closed = true; this.bootController.abort(); this.cancelDenied();
  }
  private cancelDenied(): void {
    for (let index = this.queue.length - 1; index >= 0; index--) {
      const job = this.queue[index]; const reason = this.denial(job.kind, job.options);
      if (reason) { this.queue.splice(index, 1); job.data = undefined; this.bytes -= job.size; this.drop(job, reason); }
    }
    for (const job of this.active) {
      const reason = this.denial(job.kind, job.options);
      if (reason) { job.controller.abort(); this.drop(job, reason); }
    }
    this.notify();
  }
  private notify(): void { for (const notify of this.listeners) notify(); }
  private deadline<T>(promise: Promise<T>, timeout: number, fallback: T): Promise<T> {
    return new Promise(resolve => {
      const timer = setTimeout(() => resolve(fallback), timeout); unref(timer);
      void promise.then(value => { clearTimeout(timer); resolve(value); }, () => { clearTimeout(timer); resolve(fallback); });
    });
  }
  async flush(timeoutMs = this.shutdownTimeout): Promise<AttachmentFlushResult> {
    this.cancelDenied();
    if (this.queue.length || this.active.size) {
      await new Promise<void>(resolve => {
        const done = () => { clearTimeout(timer); this.listeners.delete(changed); resolve(); };
        const changed = () => { if (!this.queue.length && !this.active.size) done(); };
        const timer = setTimeout(done, bounded(timeoutMs, this.shutdownTimeout, 30000)); unref(timer);
        this.listeners.add(changed); changed();
      });
    }
    return Object.freeze({ unfinished: this.queue.length + this.active.size, dropped: this.dropped });
  }
  shutdown(timeoutMs = this.shutdownTimeout): Promise<AttachmentFlushResult> {
    if (!this.shutdownPromise) {
      // Stop new admissions, but give accepted jobs a bounded finalization window.
      this.shutdownPromise = (async () => {
        const result = await this.flush(timeoutMs);
        this.closed = true; this.bootController.abort(); this.cancelDenied();
        return Object.freeze({ unfinished: result.unfinished, dropped: this.dropped });
      })();
    }
    return this.shutdownPromise;
  }
  /** Aggregate diagnostics only; no per-source metadata, names, hashes or exception text. */
  status(): Readonly<{ state: AttachmentAdmission; queued: number; active: number; ownedBytes: number; dropped: number }> {
    return Object.freeze({ state: this.closed ? this.closedReason : this.bootStatus, queued: this.queue.length, active: this.active.size, ownedBytes: this.bytes, dropped: this.dropped });
  }
}
