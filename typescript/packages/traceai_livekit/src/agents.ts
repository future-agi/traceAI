/**
 * Opt-in upload of a bound LiveKit Agents (Node) session's EXISTING local recording.
 *
 * Node only; import it from `@traceai/fi-instrumentation-livekit/agents`, never from the root entry.
 * This binding never enables, starts or stops a recording, and never deletes the file. It supports
 * only the exact `@livekit/agents` versions the component suite qualifies; the private lifecycle
 * seams it uses are confined to this file and fail closed on any other version.
 */
import { lstat } from "node:fs/promises";
import * as path from "node:path";
import { trace, type Context } from "@opentelemetry/api";
import type {
  AttachmentAdmission,
  AttachmentCapturePolicy,
  AttachmentCompletion,
  AttachmentCorrelation,
  AttachmentManager,
  AttachmentOwnedRecordingHandle,
  AttachmentRecordingFileScope,
} from "@traceai/fi-core/attachments";
import { createRecordingFileScope } from "@traceai/fi-core/attachments/node";

/** `@livekit/agents` versions whose session-end ordering and recorder seams were qualified. */
export const LIVEKIT_AGENTS_SUPPORTED_VERSIONS: readonly string[] = Object.freeze([
  "1.8.0", "1.8.1", "1.9.0", "1.9.1", "1.9.2", "1.9.3", "1.9.4", "1.9.5",
]);
const RECORDING_FILE = "audio.ogg";
const MAX_TIMEOUT_MS = 30_000;
const PII_FALSY = new Set(["0", "false", "no", "off"]);

/** Content-free outcome. `validating` means the file reached quarantine, NOT that it is playable. */
export type LiveKitSessionRecordingStatus =
  | "session_bound"
  | "disabled"
  | "unsupported_version"
  | "conflict"
  | AttachmentAdmission
  | AttachmentCompletion["status"];

export interface LiveKitSessionRecordingOptions {
  /** The provider's shared manager (`provider.attachments`). This binding never creates another. */
  manager: AttachmentManager;
  /** The `@livekit/agents` module your application uses (`import * as agents from "@livekit/agents"`). */
  agents: { version?: unknown; JobContext?: unknown; voice?: { AgentSession?: unknown } };
  /** Must be exactly `true` to upload an existing recording. Anything else reports `disabled`. */
  ownedRecordingFiles: boolean;
  /** Upload deadline at session end, at most 30 000 ms (default 30 000). */
  finalizationTimeoutMs?: number;
  /** Additional restrictions only; a binding never relaxes the manager's policy. */
  capture?: AttachmentCapturePolicy;
}

interface RecorderLike { readonly recording?: unknown; readonly outputPath?: unknown }
interface SessionLike {
  rootSpanContext?: Context;
  _recorderIO?: RecorderLike;
  _redactionEnabled?: unknown;
  sessionOptions?: { recordingOptions?: { audio?: unknown; redaction?: unknown } };
}
interface JobLike {
  _primaryAgentSession?: unknown;
  readonly sessionDirectory?: unknown;
  _redactionEnabled?: unknown;
  readonly job?: { enableRedaction?: unknown };
  makeSessionReport?: (session?: unknown) => { audioRecordingPath?: unknown };
  _onSessionEnd?: () => Promise<void>;
}

const BINDING = Symbol.for("traceai.livekit.agents.recording_binding.v1");
const isFn = (value: unknown): value is (...args: unknown[]) => unknown => typeof value === "function";

function nativeDenial(session: SessionLike, job: JobLike, env: NodeJS.ProcessEnv): LiveKitSessionRecordingStatus | undefined {
  // Native integration restrictions come first, before any recording-path inspection.
  const pii = env.LIVEKIT_TELEMETRY_ALLOW_PII;
  if (pii !== undefined && PII_FALSY.has(pii.trim().toLowerCase())) return "policy_denied";
  const options = session.sessionOptions?.recordingOptions;
  // Text redaction never sanitizes a binary recording; any redaction requirement denies it.
  if (options?.redaction || session._redactionEnabled !== false || job._redactionEnabled !== false || job.job?.enableRedaction)
    return "redaction_unavailable";
  if (options?.audio !== true) return "policy_denied";
  return undefined;
}

/**
 * The binding's state is content-free. `close()` only detaches this adapter: it never stops the
 * application's session, shuts down the shared manager, or enables or deletes a recording.
 */
export class LiveKitSessionRecording {
  status: LiveKitSessionRecordingStatus = "session_bound";
  private session?: SessionLike;
  private job?: JobLike;
  private correlation?: AttachmentCorrelation;
  private root?: string;
  private wrapper?: () => Promise<void>;
  private hadOwnHook = false;
  private ownHook?: () => Promise<void>;
  private handle?: AttachmentOwnedRecordingHandle;
  private finalizing?: Promise<LiveKitSessionRecordingStatus>;
  private closed = false;
  /** Set when the deadline wins; a late step must not open, upload or keep anything. */
  private stopped = false;

  /** @internal */
  constructor(
    private readonly manager: AttachmentManager,
    private readonly timeoutMs: number,
    private readonly capture: AttachmentCapturePolicy | undefined,
    private readonly env: NodeJS.ProcessEnv,
  ) {}

  /** @internal */
  attach(session: SessionLike, job: JobLike, correlation: AttachmentCorrelation, root: string): void {
    this.session = session; this.job = job; this.correlation = correlation; this.root = root;
    const original = job._onSessionEnd!;
    this.hadOwnHook = Object.prototype.hasOwnProperty.call(job, "_onSessionEnd");
    this.ownHook = this.hadOwnHook ? original : undefined;
    const wrapper = async () => {
      // The SDK awaits this after AgentSession.close() (recorder closed) and the application's
      // onSessionEnd, and before its own report upload. Its own work always still runs.
      try { await this.finalize(); } finally { await original.call(job); }
    };
    this.wrapper = wrapper;
    job._onSessionEnd = wrapper;
    (job as Record<symbol, unknown>)[BINDING] = this;
  }

  /** @internal Same arguments as a previous bind. */
  same(manager: AttachmentManager, session: unknown, timeoutMs: number, capture: AttachmentCapturePolicy | undefined): boolean {
    return manager === this.manager && session === this.session && timeoutMs === this.timeoutMs &&
      JSON.stringify(capture ?? {}) === JSON.stringify(this.capture ?? {});
  }

  /** The bounded transfer handle, once a file was handed to the manager. */
  get recording(): AttachmentOwnedRecordingHandle | undefined { return this.handle; }

  /** At most once; bounded by the finalization deadline. Never rejects. */
  finalize(): Promise<LiveKitSessionRecordingStatus> {
    this.finalizing ??= this.run();
    return this.finalizing;
  }

  private async run(): Promise<LiveKitSessionRecordingStatus> {
    if (this.closed || this.status !== "session_bound") return this.status;
    let scope: AttachmentRecordingFileScope | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const deadline = Date.now() + this.timeoutMs;
    try {
      const outcome = await Promise.race([
        this.capture_(s => { scope = s; }, deadline),
        new Promise<LiveKitSessionRecordingStatus>(resolve => { timer = setTimeout(() => resolve("incomplete"), this.timeoutMs); }),
      ]);
      this.status = outcome;
    } catch {
      // Never log the report, path or error text.
      this.status = "source_unavailable";
    } finally {
      this.stopped = true;
      if (timer) clearTimeout(timer);
      if (this.handle && this.status !== "validating") this.handle.cancel();
      if (scope) await scope.close().catch(() => undefined);
      this.close();
    }
    return this.status;
  }

  private async capture_(keep: (scope: AttachmentRecordingFileScope) => void, deadline: number): Promise<LiveKitSessionRecordingStatus> {
    const session = this.session!, job = this.job!, root = this.root!;
    const denial = nativeDenial(session, job, this.env);
    if (denial) return denial;
    // The manager policy is checked again FIRST at session end, before the SDK report is built and
    // before any stat or open of the session directory: a policy tightened mid-session wins.
    const ticket = this.manager.preflight(this.correlation, { mimeType: "audio/ogg", direction: "mixed", maxBytes: 1,
      operation: "import_existing", sourceKind: "OwnedRecordingFile", purpose: "conversation_recording", capture: this.capture });
    ticket.release();
    if (!ticket.admitted) return ticket.reason;
    // The report path is NOT authority. Only the job's primary session, its SDK recorder (closed)
    // and the SDK's fixed filename under the bound job's session directory are eligible.
    if (job._primaryAgentSession !== session || job.sessionDirectory !== root) return "source_unavailable";
    const recorder = session._recorderIO;
    const expected = path.join(root, RECORDING_FILE);
    if (!recorder || recorder.recording !== false || recorder.outputPath !== expected) return "source_unavailable";
    const report = job.makeSessionReport!(session);
    if (report?.audioRecordingPath !== expected) return "source_unavailable";
    if (this.stopped) return "incomplete";
    const info = await lstat(expected);
    if (this.stopped) return "incomplete";
    if (!info.isFile() || info.nlink !== 1 || (typeof process.getuid === "function" && info.uid !== process.getuid())) return "source_unavailable";
    const scope = await createRecordingFileScope(root, { enabled: true });
    if (this.stopped) { await scope.close(); return "incomplete"; }
    keep(scope);
    const identity = await scope.identify(RECORDING_FILE);
    if (this.stopped) return "incomplete";
    const handle = this.manager.attachOwnedRecording(this.correlation, scope.source(RECORDING_FILE, identity), {
      mimeType: "audio/ogg", direction: "mixed", purpose: "conversation_recording", capture: this.capture,
    });
    this.handle = handle;
    if (handle.reason !== "queued") return handle.reason;
    const result = await handle.wait(Math.max(1, deadline - Date.now()));
    return result.status;
  }

  /** Detach. Restores the SDK hook only if nobody replaced this binding's wrapper since. */
  close(): void {
    if (this.closed) return;
    this.closed = true;
    const job = this.job;
    if (job && this.wrapper && job._onSessionEnd === this.wrapper) {
      if (this.hadOwnHook) job._onSessionEnd = this.ownHook;
      else delete job._onSessionEnd;
    }
    if (job && (job as Record<symbol, unknown>)[BINDING] === this) delete (job as Record<symbol, unknown>)[BINDING];
    this.session = undefined; this.job = undefined; this.root = undefined; this.wrapper = undefined; this.ownHook = undefined;
  }
}

/**
 * Bind AFTER `await session.start(...)`. The session span is snapshotted now, before the SDK ends it.
 * At session end the SDK's own `_onSessionEnd` is chained, not replaced: it runs after
 * `AgentSession.close()` and your `onSessionEnd`, and before LiveKit's report upload. If your
 * `onSessionEnd` deletes or replaces `audio.ogg`, this binding reports `source_unavailable`.
 *
 * Give the worker a shutdown grace longer than this deadline plus your own callbacks. Hard process
 * termination loses the upload.
 */
export function bindLiveKitSessionRecording(session: unknown, jobContext: unknown, options: LiveKitSessionRecordingOptions): LiveKitSessionRecording {
  if (!options || typeof options.ownedRecordingFiles !== "boolean") throw new TypeError("ownedRecordingFiles must be an explicit boolean");
  const timeoutMs = options.finalizationTimeoutMs ?? MAX_TIMEOUT_MS;
  if (typeof timeoutMs !== "number" || !Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > MAX_TIMEOUT_MS)
    throw new RangeError("finalizationTimeoutMs must be within (0, 30000]");
  if (options.capture?.newRecording) throw new TypeError("a session upload never creates a recording");
  const job = jobContext as JobLike | undefined;
  const previous = job && typeof job === "object" ? (job as Record<symbol, unknown>)[BINDING] : undefined;
  const binding = new LiveKitSessionRecording(options.manager, timeoutMs, options.capture, process.env);
  if (previous instanceof LiveKitSessionRecording) {
    if (previous.same(options.manager, session, timeoutMs, options.capture) && options.ownedRecordingFiles) return previous;
    binding.status = "conflict";
    return binding;
  }
  const fail = (status: LiveKitSessionRecordingStatus) => { binding.status = status; binding.close(); return binding; };
  if (options.ownedRecordingFiles !== true) return fail("disabled");
  try {
    const agents = options.agents;
    if (!agents || typeof agents.version !== "string" || !LIVEKIT_AGENTS_SUPPORTED_VERSIONS.includes(agents.version)) return fail("unsupported_version");
    const JobContext = agents.JobContext, AgentSession = agents.voice?.AgentSession;
    if (!isFn(JobContext) || !isFn(AgentSession) || !(jobContext instanceof JobContext) || !(session instanceof AgentSession)) return fail("unsupported_version");
    const s = session as SessionLike;
    if (!isFn(job!._onSessionEnd) || !isFn(job!.makeSessionReport)) return fail("unsupported_version");
    // Never fall back to an ambient span from an unrelated late callback.
    const span = s.rootSpanContext ? trace.getSpan(s.rootSpanContext) : undefined;
    if (!span || !span.isRecording()) return fail("correlation_unavailable");
    const correlation = options.manager.correlation(span);
    // Public admission only; the file is neither stat'ed nor opened while bound.
    const ticket = options.manager.preflight(correlation, { mimeType: "audio/ogg", direction: "mixed", maxBytes: 1,
      operation: "import_existing", sourceKind: "OwnedRecordingFile", purpose: "conversation_recording", capture: options.capture });
    ticket.release();
    if (!ticket.admitted) return fail(ticket.reason);
    if (job!._primaryAgentSession !== session) return fail("source_unavailable");
    const denial = nativeDenial(s, job!, process.env);
    if (denial) return fail(denial);
    const root = job!.sessionDirectory;
    if (typeof root !== "string" || !path.isAbsolute(root)) return fail("source_unavailable");
    binding.attach(s, job!, correlation, root);
  } catch {
    return fail("source_unavailable");
  }
  return binding;
}
