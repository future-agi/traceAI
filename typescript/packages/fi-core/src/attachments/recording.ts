import { v4 as uuid } from "uuid";
import { attachmentTraceUuid, validAttachmentUuid } from "./correlation";
import type { AttachmentHost, CoreJob, UploadOutcome } from "./internal";
import { attachmentIdempotencyKey } from "./transport-v1";
import type {
  AttachmentAdmission, AttachmentCapturePolicy, AttachmentCompletion, AttachmentContentSource, AttachmentDirection, AttachmentGapReason,
  AttachmentRecordingManifest, AttachmentRecordingOptions, AttachmentRecordingResult, AttachmentRecordingStatus, AttachmentRecordingWriter,
  AttachmentTrackSpec, AttachmentUploadRequest,
} from "./types";

export const ATTACHMENT_WAV_HEADER_BYTES = 44;
const MAX_SEGMENT_BYTES = 8 * 1024 * 1024;
const MAX_SEGMENT_MS = 30000;
const MAX_RECORDING_MS = 7200000;
const MAX_RECORDING_BYTES = 1024 * 1024 * 1024;
const MAX_MANIFEST_BYTES = 1048576;
const MAX_PLAYOUT = 4096;
const GAP_REASONS: readonly AttachmentGapReason[] = ["queue_full", "bootstrap_pending", "source_unavailable", "policy_denied", "truncated", "incomplete"];

function gcd(a: number, b: number): number { while (b) { [a, b] = [b, a % b]; } return a; }
/** Smallest frame count that is a whole number of milliseconds (design 2.5). */
export function attachmentBoundaryStep(sampleRateHz: number): number { return sampleRateHz / gcd(sampleRateHz, 1000); }
/** Largest multiple of the boundary step within both the duration and the byte bound (header included). */
export function attachmentSegmentFrames(sampleRateHz: number, frameBytes: number, segmentMs = MAX_SEGMENT_MS, segmentBytes = MAX_SEGMENT_BYTES): number {
  const g = attachmentBoundaryStep(sampleRateHz);
  const byMs = Math.floor(Math.min(segmentMs, MAX_SEGMENT_MS) * sampleRateHz / 1000);
  const byBytes = Math.floor((Math.min(segmentBytes, MAX_SEGMENT_BYTES) - ATTACHMENT_WAV_HEADER_BYTES) / frameBytes);
  return Math.floor(Math.min(byMs, byBytes) / g) * g;
}
export function attachmentFramesToMs(frames: number, sampleRateHz: number, roundUp = false): number {
  return roundUp ? Math.ceil(frames * 1000 / sampleRateHz) : Math.floor(frames * 1000 / sampleRateHz);
}
/** Canonical 44-byte RIFF/WAVE PCM header (format tag 1, 16-bit). */
export function attachmentWavHeader(channels: number, sampleRateHz: number, dataBytes: number): Uint8Array {
  const header = new Uint8Array(ATTACHMENT_WAV_HEADER_BYTES); const view = new DataView(header.buffer);
  const ascii = (offset: number, text: string) => { for (let i = 0; i < text.length; i++) header[offset + i] = text.charCodeAt(i); };
  ascii(0, "RIFF"); view.setUint32(4, 36 + dataBytes, true); ascii(8, "WAVE"); ascii(12, "fmt ");
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, channels, true); view.setUint32(24, sampleRateHz, true);
  view.setUint32(28, sampleRateHz * channels * 2, true); view.setUint16(32, channels * 2, true); view.setUint16(34, 16, true);
  ascii(36, "data"); view.setUint32(40, dataBytes, true);
  return header;
}
/** Header plus retained chunks, served as one bounded content source without re-materializing. */
export function attachmentChunkedSource(header: Uint8Array, chunks: readonly Uint8Array[]): AttachmentContentSource {
  let size = header.byteLength; for (const chunk of chunks) size += chunk.byteLength;
  if (size <= MAX_SEGMENT_BYTES) {
    const out = new Uint8Array(size); out.set(header, 0); let offset = header.byteLength;
    for (const chunk of chunks) { out.set(chunk, offset); offset += chunk.byteLength; }
    return out;
  }
  const parts = [header, ...chunks];
  return {
    size,
    async read(offset: number, length: number) {
      const out = new Uint8Array(Math.min(length, size - offset)); let base = 0; let written = 0;
      for (const part of parts) {
        const end = base + part.byteLength;
        if (end > offset && base < offset + out.byteLength) {
          const from = Math.max(0, offset - base); const to = Math.min(part.byteLength, offset + out.byteLength - base);
          out.set(part.subarray(from, to), written); written += to - from;
        }
        base = end; if (written === out.byteLength) break;
      }
      return out;
    },
    async check() { /* in-memory */ },
  };
}

/** Cross-field manifest rules mirrored from F01 validate.py, applied locally before every PUT. */
export function validAttachmentManifest(value: AttachmentRecordingManifest): boolean {
  try {
    if (!value || value.schema_version !== 1 || !validAttachmentUuid(value.recording_id) || value.timebase !== "session_relative_ms") return false;
    if (new TextEncoder().encode(JSON.stringify(value)).byteLength > MAX_MANIFEST_BYTES) return false;
    if (!Number.isInteger(value.sample_rate_hz) || value.sample_rate_hz < 8000 || value.sample_rate_hz > 192000) return false;
    if (!Number.isInteger(value.channels) || value.channels < 1 || value.channels > 32) return false;
    if (!Number.isInteger(value.duration_ms) || value.duration_ms < 0 || value.duration_ms > MAX_RECORDING_MS) return false;
    if (value.capture_complete && value.gaps.length) return false;
    if (!Array.isArray(value.tracks) || value.tracks.length < 1 || value.tracks.length > 32) return false;
    const ids = value.tracks.map(track => track.track_id);
    if (new Set(ids).size !== ids.length || !ids.every(validAttachmentUuid)) return false;
    const channels = value.tracks.flatMap(track => track.channels).sort((a, b) => a - b);
    if (channels.length !== value.channels || channels.some((channel, index) => channel !== index)) return false;
    if (value.tracks.some(track => new Set(track.channels).size !== track.channels.length || !["user", "assistant", "mixed", "unknown"].includes(track.role) ||
      !["input", "output", "mixed"].includes(track.direction))) return false;
    if (value.layout === "mixed" && value.tracks.some(track => track.direction !== "mixed")) return false;
    if (value.layout === "stereo" && value.channels !== 2) return false;
    const separate = value.layout === "separate_tracks";
    const seen = new Set<string>(); let previous = -1;
    for (let index = 0; index < value.segments.length; index++) {
      const s = value.segments[index];
      if (s.index !== index || s.start_ms < previous || seen.has(s.occurrence_id) || !validAttachmentUuid(s.occurrence_id)) return false;
      if (!Number.isInteger(s.duration_ms) || s.duration_ms < 1 || s.duration_ms > MAX_SEGMENT_MS) return false;
      seen.add(s.occurrence_id); previous = s.start_ms;
    }
    const timelines = new Map<string | undefined, { start_ms: number; duration_ms: number }[]>();
    if (separate) for (const id of ids) timelines.set(id, []); else timelines.set(undefined, []);
    for (const interval of [...value.segments, ...value.gaps]) {
      if (separate ? interval.track_id === undefined : interval.track_id !== undefined) return false;
      const list = timelines.get(interval.track_id); if (!list) return false;
      if (!Number.isInteger(interval.start_ms) || !Number.isInteger(interval.duration_ms) || interval.duration_ms < 1) return false;
      list.push(interval);
    }
    if (value.gaps.some(gap => !GAP_REASONS.includes(gap.reason))) return false;
    for (const list of timelines.values()) {
      let end = 0;
      for (const interval of [...list].sort((a, b) => a.start_ms - b.start_ms)) { if (interval.start_ms !== end) return false; end += interval.duration_ms; }
      if (end !== value.duration_ms) return false;
    }
    for (const interval of [...value.segments, ...value.gaps, ...value.playout]) {
      if (interval.start_ms + interval.duration_ms > value.duration_ms || interval.duration_ms < 1) return false;
    }
    return true;
  } catch { return false; }
}

interface Piece {
  kind: "segment" | "gap";
  track: number;
  start: number;
  end: number;
  reason?: AttachmentGapReason;
  // Segments only.
  source?: AttachmentContentSource;
  bytes?: number;
  state?: "sealed" | "queued" | "uploading" | "done" | "failed";
  occurrenceId?: string;
  attachmentId?: string;
  index?: number;
  outcome?: UploadOutcome;
  failure?: AttachmentCompletion["status"];
  job?: CoreJob;
}
interface Track {
  ordinal: number;
  spec: AttachmentTrackSpec;
  manifestId?: string;
  frameBytes: number;
  channels: number;
  denied?: AttachmentAdmission;
  pos: number;
  fill: number;
  blockGap?: AttachmentGapReason;
  blockData: number;
  runStart: number;
  runFrames: number;
  chunks: Uint8Array[];
  chunkBytes: number;
  carry?: Uint8Array;
  pieces: Piece[];
}
export interface RecordingIdentity { traceId: string; spanId: string }
export interface RecordingInit {
  host: AttachmentHost;
  options: AttachmentRecordingOptions;
  identity?: RecordingIdentity;
  /** queued (active), bootstrap_pending (pending mode) or a denial (inert). */
  admission: AttachmentAdmission;
  /** Per-track denial for separate_tracks. */
  trackDenials?: readonly (AttachmentAdmission | undefined)[];
  /** Re-run full admission (pending writers and policy epoch changes). */
  readmit(): { reason: AttachmentAdmission; trackDenials?: readonly (AttachmentAdmission | undefined)[] };
  /** Applies the carrier stamp once admitted. */
  stamp(): void;
  /** Releases the recording slot and descriptor exactly once. */
  close(): void;
  sourceKind?: "Bytes" | "OwnedRecordingFile";
}
export interface RecordingFileSegment { track: number; startFrame: number; endFrame: number; source: AttachmentContentSource }

/** Validates options, returning the layout's stream shape or undefined (fail closed). */
export function attachmentRecordingShape(options: AttachmentRecordingOptions): { streams: number; frameBytes: number[]; channels: number } | undefined {
  try {
    const rate = options?.format?.sampleRateHz;
    if (!Number.isInteger(rate) || rate < 8000 || rate > 192000 || (options.format.sampleFormat ?? "s16le") !== "s16le") return undefined;
    if (attachmentBoundaryStep(rate) > 4800) return undefined;
    if (!["mixed", "stereo", "separate_tracks"].includes(options.layout) || !["observe_consumed", "create_recording"].includes(options.operation)) return undefined;
    if (options.purpose !== undefined && options.purpose !== "conversation_recording" && options.purpose !== "turn_recording") return undefined;
    const tracks = options.tracks;
    if (!Array.isArray(tracks) || tracks.length < 1 || tracks.length > 32) return undefined;
    for (const track of tracks) {
      if (!track || !Array.isArray(track.channels) || track.channels.length < 1 || new Set(track.channels).size !== track.channels.length ||
        track.channels.some((c: number) => !Number.isInteger(c) || c < 0 || c > 31) || !["input", "output", "mixed"].includes(track.direction) ||
        !["user", "assistant", "mixed", "unknown"].includes(track.role)) return undefined;
    }
    const all = tracks.flatMap(track => [...track.channels]).sort((a, b) => a - b);
    if (all.some((channel, index) => channel !== index)) return undefined;
    const window = options.windowSegments ?? 2;
    if (!Number.isInteger(window) || window < 1 || window > 4) return undefined;
    for (const id of [options.conversationId, options.turnId]) if (id !== undefined && !(typeof id === "string" && /^[a-zA-Z0-9_-]{1,128}$/.test(id))) return undefined;
    if (options.layout === "mixed") return tracks.length === 1 && tracks[0].direction === "mixed" ? { streams: 1, frameBytes: [all.length * 2], channels: all.length } : undefined;
    if (options.layout === "stereo") return tracks.length === 2 && tracks.every(track => track.channels.length === 1) ? { streams: 1, frameBytes: [4], channels: 2 } : undefined;
    return { streams: tracks.length, frameBytes: tracks.map(track => track.channels.length * 2), channels: all.length };
  } catch { return undefined; }
}

/** Live PCM recording writer and the shared segment/manifest pipeline (design section 2). */
export class AttachmentRecording implements AttachmentRecordingWriter {
  readonly recordingId = uuid();
  private mode: "pending" | "active" | "finishing" | "terminal";
  private admissionReason: AttachmentAdmission;
  private terminalReason?: AttachmentAdmission | AttachmentCompletion["status"];
  private readonly host: AttachmentHost;
  private readonly options: AttachmentRecordingOptions;
  private readonly identity?: RecordingIdentity;
  private readonly init: RecordingInit;
  private readonly rate: number;
  private readonly g: number;
  private F = 0;
  private readonly tracks: Track[] = [];
  private readonly manifestTracks: { track_id: string; channels: number[]; direction: AttachmentDirection; role: AttachmentTrackSpec["role"] }[] = [];
  private readonly windowSegments: number;
  private epoch: number;
  private totalBytes = 0;
  private truncated = false;
  private maxFrames = 0;
  private recordingBytesCap = MAX_RECORDING_BYTES;
  private completed = 0;
  private sinceManifest = 0;
  private lastPutEnd = 0;
  private revision = 0;
  private publishedSegments = 0;
  private publishedGapMs = 0;
  private publishedComplete = false;
  private readonly cuts: number[] = [];
  private manifestQueued = false;
  private manifestFallback = false;
  private manifestDisabled = false;
  private indexReused = false;
  private readonly playout: { startMs: number; durationMs: number; evidence: "generated" | "played" | "unknown" | "truncated" }[] = [];
  private outstanding = 0;
  private idleWaiters: (() => void)[] = [];
  private resultPromise?: Promise<AttachmentRecordingResult>;
  private closed = false;
  private finalEnd?: number;
  private stamped = false;
  private finalPublished = false;
  private sealWaiters: (() => void)[] = [];

  constructor(init: RecordingInit) {
    this.init = init; this.host = init.host; this.options = init.options; this.identity = init.identity;
    this.rate = init.options.format?.sampleRateHz ?? 8000;
    this.g = Number.isInteger(this.rate) && this.rate > 0 ? attachmentBoundaryStep(this.rate) : 1;
    this.windowSegments = init.options.windowSegments ?? 2;
    this.epoch = this.host.policyEpoch();
    this.admissionReason = init.admission;
    const shape = attachmentRecordingShape(init.options);
    if (!shape) { this.admissionReason = this.admissionReason === "queued" || this.admissionReason === "bootstrap_pending" ? "policy_denied" : this.admissionReason; }
    if (shape) {
      const specs = init.options.tracks;
      for (const spec of specs) this.manifestTracks.push({ track_id: uuid(), channels: [...spec.channels], direction: spec.direction, role: spec.role });
      for (let i = 0; i < shape.streams; i++) {
        this.tracks.push({ ordinal: i, spec: init.options.layout === "separate_tracks" ? specs[i] : { channels: specs.flatMap(s => [...s.channels]), direction: "mixed", role: "mixed" },
          manifestId: init.options.layout === "separate_tracks" ? this.manifestTracks[i].track_id : undefined,
          frameBytes: shape.frameBytes[i], channels: shape.frameBytes[i] / 2, denied: init.trackDenials?.[i], pos: 0, fill: 0, blockData: 0,
          runStart: 0, runFrames: 0, chunks: [], chunkBytes: 0, pieces: [] });
      }
    }
    if (this.admissionReason === "queued") { this.mode = "active"; this.activate(); }
    else if (this.admissionReason === "bootstrap_pending" && shape) this.mode = "pending";
    else { this.mode = "terminal"; this.terminalReason = this.admissionReason; this.closeOnce(); }
  }
  get reason(): AttachmentAdmission { return this.admissionReason; }

  private activate(): boolean {
    const session = this.host.currentSession();
    if (!session) return false;
    const limits = session.limits;
    const segMs = Math.min(limits.segment_duration_ms ?? MAX_SEGMENT_MS, MAX_SEGMENT_MS);
    const segBytes = Math.min(limits.segment_bytes ?? MAX_SEGMENT_BYTES, limits.max_upload_bytes ?? MAX_SEGMENT_BYTES, MAX_SEGMENT_BYTES);
    this.F = attachmentSegmentFrames(this.rate, Math.max(...this.tracks.map(t => t.frameBytes)), segMs, segBytes);
    if (this.F < this.g) { this.terminal("too_large"); return false; }
    const recMs = Math.min(limits.recording_duration_ms ?? MAX_RECORDING_MS, MAX_RECORDING_MS);
    this.maxFrames = Math.floor(recMs * this.rate / 1000);
    this.recordingBytesCap = Math.min(limits.recording_bytes ?? MAX_RECORDING_BYTES, MAX_RECORDING_BYTES);
    if (!this.stamped) { this.stamped = true; try { this.init.stamp(); } catch { /* never throw into audio callbacks */ } }
    return true;
  }
  private terminal(reason: AttachmentAdmission | AttachmentCompletion["status"]): void {
    if (this.mode === "terminal") return;
    this.mode = "terminal"; this.terminalReason = reason; this.wakeSeal();
  }
  private wakeSeal(): void { const waiters = this.sealWaiters; this.sealWaiters = []; for (const wake of waiters) wake(); }
  private closeOnce(): void { if (!this.closed) { this.closed = true; try { this.init.close(); } catch { /* bounded */ } this.host.recordingClosed(this.recordingId); } }

  // --- block timeline -----------------------------------------------------------------------------
  private releaseTrackBlock(track: Track): void {
    if (!track.blockData) return;
    let drop = track.blockData * track.frameBytes; track.blockData = 0;
    this.host.releaseBytes(drop); track.chunkBytes -= drop;
    while (drop > 0 && track.chunks.length) {
      const last = track.chunks[track.chunks.length - 1];
      if (last.byteLength <= drop) { drop -= last.byteLength; track.chunks.pop(); }
      else { track.chunks[track.chunks.length - 1] = last.subarray(0, last.byteLength - drop); drop = 0; }
    }
  }
  private commitBlock(track: Track, frames = this.g): void {
    if (track.blockGap === undefined) { track.runFrames += frames; track.blockData = 0; }
    else { this.closeRun(track); this.appendGap(track, track.blockGap, frames); }
    track.pos += frames; track.fill = 0; track.blockGap = undefined;
    if (track.runFrames === 0) track.runStart = track.pos;
    if (this.F && track.pos % this.F === 0) { this.closeRun(track); this.boundary(track); }
  }
  private appendGap(track: Track, reason: AttachmentGapReason, frames: number): void {
    const last = track.pieces[track.pieces.length - 1];
    const start = track.pos;
    if (last && last.kind === "gap" && last.reason === reason && last.end === start) last.end = start + frames;
    else track.pieces.push({ kind: "gap", track: track.ordinal, start, end: start + frames, reason });
    track.runStart = start + frames;
  }
  private closeRun(track: Track): void {
    if (track.runFrames > 0) {
      const dataBytes = track.runFrames * track.frameBytes;
      const chunks = track.chunks; let kept = 0; const used: Uint8Array[] = [];
      for (const chunk of chunks) { if (kept >= dataBytes) break; const take = Math.min(chunk.byteLength, dataBytes - kept); used.push(take === chunk.byteLength ? chunk : chunk.subarray(0, take)); kept += take; }
      // Bytes of the still-open block stay with the track.
      const rest: Uint8Array[] = []; let skip = dataBytes;
      for (const chunk of chunks) { if (skip >= chunk.byteLength) { skip -= chunk.byteLength; continue; } rest.push(skip ? chunk.subarray(skip) : chunk); skip = 0; }
      track.chunks = rest; track.chunkBytes -= dataBytes;
      const header = attachmentWavHeader(track.channels, this.rate, dataBytes);
      this.totalBytes += dataBytes + ATTACHMENT_WAV_HEADER_BYTES;
      track.pieces.push({ kind: "segment", track: track.ordinal, start: track.runStart, end: track.runStart + track.runFrames,
        source: attachmentChunkedSource(header, used), bytes: dataBytes, state: "sealed", occurrenceId: uuid(), attachmentId: uuid() });
      this.wakeSeal();
    }
    track.runFrames = 0; track.runStart = track.pos;
    this.release();
  }
  /** Common boundaries: a track more than one window behind the leader is filled with a gap (design 2.5). */
  private boundary(leader: Track): void {
    if (this.tracks.length < 2) return;
    const target = leader.pos - this.F;
    if (target <= 0) return;
    for (const track of this.tracks) {
      if (track === leader || track.pos + track.fill >= target) continue;
      this.gapFrames(track, target - track.pos - track.fill, track.denied ? "policy_denied" : "source_unavailable");
    }
  }
  private gapFrames(track: Track, frames: number, reason: AttachmentGapReason): void {
    while (frames > 0) {
      if (track.blockData) this.releaseTrackBlock(track);
      if (track.blockGap === undefined) track.blockGap = reason;
      const take = Math.min(frames, this.g - track.fill);
      track.fill += take; frames -= take;
      if (track.fill === this.g) this.commitBlock(track);
    }
  }
  private dataFrames(track: Track, bytes: Uint8Array): void {
    const frames = bytes.byteLength / track.frameBytes; let offset = 0; let remaining = frames;
    while (remaining > 0) {
      const take = Math.min(remaining, this.g - track.fill);
      if (track.blockGap === undefined) {
        const part = bytes.subarray(offset * track.frameBytes, (offset + take) * track.frameBytes);
        track.chunks.push(part); track.chunkBytes += part.byteLength; track.blockData += take;
      } else this.host.releaseBytes(take * track.frameBytes); // tainted block: data becomes part of the gap
      track.fill += take; remaining -= take; offset += take;
      if (track.fill === this.g) this.commitBlock(track);
    }
  }
  private totalFrames(track: Track): number { return track.pos + track.fill; }
  private waitingWindows(): number {
    const starts = new Set<number>();
    for (const track of this.tracks) for (const piece of track.pieces) if (piece.kind === "segment" && (piece.state === "sealed" || piece.state === "queued" || piece.state === "uploading")) starts.add(piece.start);
    return starts.size;
  }

  // --- public writer API ----------------------------------------------------------------------------
  tryWrite(pcm: Uint8Array, track = 0): boolean {
    try {
      if (this.mode === "terminal" || this.mode === "finishing" || !(pcm instanceof Uint8Array) || !Number.isInteger(track)) return false;
      const t = this.tracks[track];
      if (!t) return false;
      // Partial frames carry to the next call (at most frameBytes - 1 bytes).
      let data = pcm; let ok = true;
      if (t.carry) {
        const need = t.frameBytes - t.carry.byteLength;
        if (pcm.byteLength < need) { const joined = new Uint8Array(t.carry.byteLength + pcm.byteLength); joined.set(t.carry); joined.set(pcm, t.carry.byteLength); t.carry = joined; return true; }
        const frame = new Uint8Array(t.frameBytes); frame.set(t.carry); frame.set(pcm.subarray(0, need), t.carry.byteLength); t.carry = undefined;
        ok = this.writeFrames(t, frame);
        data = pcm.subarray(need);
      }
      const whole = data.byteLength - (data.byteLength % t.frameBytes);
      if (data.byteLength !== whole) t.carry = data.slice(whole);
      if (!whole) return ok;
      return this.writeFrames(t, data.subarray(0, whole)) && ok;
    } catch { return false; }
  }
  private writeFrames(t: Track, data: Uint8Array): boolean {
    if (this.mode === "terminal" || this.mode === "finishing") return false;
    const whole = data.byteLength; const frames = whole / t.frameBytes;
    if (this.mode === "pending") {
      const state = this.host.runtimeState();
      if (state === "ready") {
        const again = this.init.readmit();
        if (again.reason !== "queued") { this.admissionReason = again.reason; this.discardAll(); this.terminal(again.reason); this.closeOnce(); return false; }
        for (let i = 0; i < this.tracks.length; i++) this.tracks[i].denied = again.trackDenials?.[i];
        this.admissionReason = "queued"; this.mode = "active"; this.epoch = this.host.policyEpoch();
        if (!this.activate()) return false;
      } else if (state === "bootstrap_pending") { this.gapFrames(t, frames, "bootstrap_pending"); return false; }
      else { this.admissionReason = state as AttachmentAdmission; this.discardAll(); this.terminal(this.admissionReason); this.closeOnce(); return false; }
    }
    if (this.host.policyEpoch() !== this.epoch) {
      this.epoch = this.host.policyEpoch();
      const again = this.init.readmit();
      if (again.reason !== "queued") { this.gapFrames(t, frames, "policy_denied"); this.policyStop(); return false; }
      for (let i = 0; i < this.tracks.length; i++) if (again.trackDenials?.[i] && !this.tracks[i].denied) { this.tracks[i].denied = again.trackDenials[i]; this.dropTrack(this.tracks[i]); }
    }
    if (t.denied) { this.gapFrames(t, frames, "policy_denied"); return false; }
    if (this.host.runtimeState() !== "ready" || this.host.stopping()) { this.gapFrames(t, frames, "incomplete"); return false; }
    if (this.truncated || this.totalFrames(t) + frames > this.maxFrames || this.totalBytes + t.chunkBytes + whole > this.recordingBytesCap) {
      this.truncate(t, frames); return false;
    }
    if (this.waitingWindows() >= this.windowSegments) { this.gapFrames(t, frames, "queue_full"); return false; }
    let accepted = true;
    for (let offset = 0; offset < whole; offset += this.host.chunkBytes) {
      const slice = data.subarray(offset, Math.min(whole, offset + this.host.chunkBytes));
      if (!this.host.reserveBytes(slice.byteLength)) { this.gapFrames(t, slice.byteLength / t.frameBytes, "queue_full"); accepted = false; continue; }
      // Mutable input: copied before the caller can reuse it.
      this.dataFrames(t, slice.slice());
    }
    return accepted;
  }
  private truncate(track: Track, frames: number): void {
    this.truncated = true;
    const ceiling = Math.floor(MAX_RECORDING_MS * this.rate / 1000);
    const allowed = Math.max(0, Math.min(frames, ceiling - this.totalFrames(track)));
    if (allowed) this.gapFrames(track, allowed, "truncated");
  }
  markGap(durationMs: number, reason: AttachmentGapReason = "source_unavailable", track?: number): void {
    try {
      if (this.mode === "terminal" || this.mode === "finishing" || !Number.isFinite(durationMs) || durationMs <= 0 || !GAP_REASONS.includes(reason)) return;
      let frames = Math.ceil(durationMs * this.rate / 1000); frames = Math.ceil(frames / this.g) * this.g;
      const targets = track === undefined ? this.tracks : [this.tracks[track]].filter(Boolean);
      for (const t of targets) {
        const ceiling = Math.floor(MAX_RECORDING_MS * this.rate / 1000);
        const allowed = Math.max(0, Math.min(frames, ceiling - this.totalFrames(t)));
        if (allowed) this.gapFrames(t, allowed, reason);
      }
    } catch { /* bounded */ }
  }
  markPlayout(startMs: number, durationMs: number, evidence: "generated" | "played" | "unknown" | "truncated"): void {
    if (this.mode === "terminal" || !Number.isInteger(startMs) || !Number.isInteger(durationMs) || startMs < 0 || durationMs < 1 ||
      startMs + durationMs > MAX_RECORDING_MS || !["generated", "played", "unknown", "truncated"].includes(evidence)) return;
    if (this.playout.length >= MAX_PLAYOUT) return;
    this.playout.push({ startMs, durationMs, evidence });
  }
  status(): AttachmentRecordingStatus {
    let sealed = 0;
    for (const track of this.tracks) for (const piece of track.pieces) if (piece.kind === "segment") sealed++;
    let owned = 0; for (const track of this.tracks) owned += track.chunkBytes;
    for (const track of this.tracks) for (const piece of track.pieces) if (piece.kind === "segment" && piece.state !== "done" && piece.state !== "failed") owned += piece.bytes ?? 0;
    return Object.freeze({ state: this.mode === "active" ? "recording" as const : this.mode, reason: (this.terminalReason ?? this.admissionReason) as AttachmentRecordingStatus["reason"], sealed,
      uploaded: this.completed, gapMs: this.publishedGapMs, manifestRevision: this.revision, ownedBytes: owned });
  }

  // --- upload pipeline ------------------------------------------------------------------------------
  /** Where the next not-yet-sealed interval of this track starts. */
  private openStart(track: Track): number { return track.runFrames > 0 ? track.runStart : track.pos; }
  private frontier(track: Track): number {
    for (const piece of track.pieces) if (piece.kind === "segment" && piece.state !== "done" && piece.state !== "failed") return Math.min(piece.start, this.openStart(track));
    return this.openStart(track);
  }
  /** Releases sealed segments in (start, track ordinal) order once no earlier interval can still appear. */
  private release(): void {
    if (this.mode === "terminal" && this.terminalReason !== "policy_denied") return;
    for (;;) {
      let head: Piece | undefined;
      for (const track of this.tracks) for (const piece of track.pieces) {
        if (piece.kind === "segment" && piece.state === "sealed" && (!head || piece.start < head.start || (piece.start === head.start && piece.track < head.track))) head = piece;
      }
      if (!head) return;
      const finishing = this.mode === "finishing" || this.finalEnd !== undefined;
      if (!finishing) {
        for (const track of this.tracks) {
          if (track.ordinal === head.track) continue;
          const open = this.openStart(track);
          if (open < head.start || (open === head.start && track.ordinal < head.track)) return;
        }
      }
      this.enqueueSegment(head);
    }
  }
  private enqueueSegment(piece: Piece): void {
    piece.state = "queued"; this.outstanding++;
    const controller = new AbortController();
    const job: CoreJob = {
      lane: this.recordingId, bytes: piece.bytes ?? 0, controller,
      run: signal => this.uploadSegment(piece, signal),
      denied: () => this.segmentDenied(piece),
      fail: reason => this.segmentSettled(piece, reason),
    };
    piece.job = job; piece.bytes = 0; // the manager now owns the reservation and releases it when the job ends
    this.host.enqueue(job);
  }
  private segmentDenied(piece: Piece): AttachmentAdmission | undefined {
    if (this.mode === "terminal" && this.terminalReason !== "policy_denied" && this.terminalReason !== "conflict") return "incomplete";
    const track = this.tracks[piece.track];
    if (track?.denied) return "policy_denied";
    const reason = this.host.recheck({ mimeType: "audio/wav", direction: this.trackDirection(piece.track), purpose: this.options.purpose ?? "conversation_recording",
      capture: this.options.capture, operation: this.init.sourceKind === "OwnedRecordingFile" ? "import_existing" : this.options.operation,
      sourceKind: this.init.sourceKind ?? "Bytes" }, { ignoreSessionExpiry: false });
    return reason;
  }
  private trackDirection(ordinal: number): AttachmentDirection {
    return this.options.layout === "separate_tracks" ? this.tracks[ordinal].spec.direction : "mixed";
  }
  private segmentRequest(piece: Piece, index: number): AttachmentUploadRequest | undefined {
    const session = this.host.currentSession();
    if (!session || !this.identity) return undefined;
    const declared = piece.source instanceof Uint8Array ? piece.source.byteLength : (piece.source as { size: number }).size;
    return { schema_version: 1, attachment_id: piece.attachmentId!, occurrence_id: piece.occurrenceId!,
      association: { capture_session_id: this.host.sessionId, correlation_nonce: this.host.nonce,
        parent: { trace_id: attachmentTraceUuid(this.identity.traceId), span_id: this.identity.spanId } },
      policy_version: session.policy.version, deletion_generation: 0, mime_type: "audio/wav", modality: "audio",
      direction: this.trackDirection(piece.track), source_kind: this.init.sourceKind ?? "Bytes",
      purpose: this.options.purpose ?? "conversation_recording", declared_bytes: declared,
      recording_id: this.recordingId, segment_index: index,
      ...(this.options.conversationId ? { conversation_id: this.options.conversationId } : {}),
      ...(this.options.turnId ? { turn_id: this.options.turnId } : {}) };
  }
  private async uploadSegment(piece: Piece, signal: AbortSignal): Promise<void> {
    if (piece.state !== "queued") return;
    // Both manifest orders failed: the recording cannot publish, so stop spending uploads (design 2.8).
    if (this.manifestFallback && this.manifestDisabled) { this.segmentSettled(piece, "incomplete"); return; }
    const reason = this.segmentDenied(piece);
    if (reason) { this.segmentSettled(piece, reason); return; }
    piece.state = "uploading";
    const index = this.completed; const reused = this.indexReused;
    const request = this.segmentRequest(piece, index);
    if (!request) { this.segmentSettled(piece, "incomplete"); return; }
    const body = JSON.stringify(request);
    const outcome = await this.host.runUpload({ body, occurrenceId: piece.occurrenceId!, declaredBytes: request.declared_bytes, source: piece.source!,
      recheck: () => this.segmentDenied(piece),
      beforeComplete: this.manifestFallback ? async (o, s) => { await this.fallbackManifest(piece, o, s); } : undefined }, signal);
    piece.outcome = outcome;
    if (outcome.status === "validating") { piece.index = index; this.segmentSettled(piece, "validating"); return; }
    if (outcome.status === "conflict" && reused && outcome.upload === undefined) this.closeOnConflict();
    this.segmentSettled(piece, outcome.status);
  }
  private segmentSettled(piece: Piece, status: AttachmentCompletion["status"]): void {
    if (piece.state === "done" || piece.state === "failed") return;
    piece.source = undefined;
    if (status === "validating") { piece.state = "done"; this.completed++; this.sinceManifest++; this.indexReused = false; }
    else { piece.state = "failed"; piece.failure = status; this.indexReused = true; this.host.countDrop(); }
    this.outstanding--;
    this.maybeManifest(false);
    if (this.outstanding === 0) { const waiters = this.idleWaiters; this.idleWaiters = []; for (const w of waiters) w(); }
  }
  private closeOnConflict(): void {
    // BR-4 fallback: a rejected reused index closes the recording with the published prefix plus an incomplete gap.
    this.discardAll(); this.terminal("conflict");
  }

  // --- manifest -------------------------------------------------------------------------------------
  private settledEnd(): number {
    let end = Infinity;
    for (const track of this.tracks) end = Math.min(end, this.frontier(track));
    if (!Number.isFinite(end)) return 0;
    for (let changed = true; changed;) {
      changed = false;
      for (const track of this.tracks) for (const piece of track.pieces) {
        if (piece.kind === "segment" && piece.start < end && piece.end > end) { end = piece.start; changed = true; }
      }
    }
    return end;
  }
  private latestCapability(): UploadOutcome | undefined {
    let best: Piece | undefined;
    for (const track of this.tracks) for (const piece of track.pieces) if (piece.state === "done" && piece.outcome?.upload && (!best || (piece.index ?? -1) > (best.index ?? -1))) best = piece;
    return best?.outcome;
  }
  private capabilities(): UploadOutcome[] {
    const all: Piece[] = [];
    for (const track of this.tracks) for (const piece of track.pieces) if (piece.state === "done" && piece.outcome?.upload) all.push(piece);
    return all.sort((a, b) => (b.index ?? 0) - (a.index ?? 0)).map(piece => piece.outcome!);
  }
  private ms(frames: number, final: boolean): number {
    return final && this.finalEnd !== undefined && frames === this.finalEnd ? attachmentFramesToMs(frames, this.rate, true) : attachmentFramesToMs(frames, this.rate);
  }
  /** Cumulative, prefix-monotonic manifest over [0, end): earlier cut points always split later gaps. */
  buildManifest(end: number, final: boolean, extra?: Piece): AttachmentRecordingManifest {
    const separate = this.options.layout === "separate_tracks";
    const segments: AttachmentRecordingManifest["segments"] = [];
    const gaps: AttachmentRecordingManifest["gaps"] = [];
    const cuts = [...this.cuts].sort((a, b) => a - b);
    for (const track of this.tracks) {
      const trackId = separate ? track.manifestId : undefined;
      for (const piece of track.pieces) {
        if (piece.start >= end) continue;
        const pieceEnd = Math.min(piece.end, end);
        const done = piece.kind === "segment" && (piece.state === "done" || piece === extra);
        if (done && piece.end <= end) {
          segments.push({ occurrence_id: piece.occurrenceId!, index: piece === extra ? this.completed : piece.index!, start_ms: this.ms(piece.start, final),
            duration_ms: this.ms(piece.end, final) - this.ms(piece.start, final), ...(trackId ? { track_id: trackId } : {}) });
          continue;
        }
        const reason: AttachmentGapReason = piece.kind === "gap" ? piece.reason! :
          piece.failure === "policy_denied" ? "policy_denied" : "incomplete";
        let start = piece.start;
        for (const cut of [...cuts.filter(c => c > piece.start && c < pieceEnd), pieceEnd]) {
          const a = this.ms(start, final); const b = this.ms(cut, final);
          if (b > a) gaps.push({ start_ms: a, duration_ms: b - a, reason, ...(trackId ? { track_id: trackId } : {}) });
          start = cut;
        }
      }
    }
    segments.sort((a, b) => a.index - b.index);
    const duration = this.ms(end, final);
    const playout = this.playout.filter(p => p.startMs + p.durationMs <= duration).map(p => ({ start_ms: p.startMs, duration_ms: p.durationMs, evidence: p.evidence }));
    const all = this.options.tracks.flatMap(t => [...t.channels]);
    return { schema_version: 1, recording_id: this.recordingId, timebase: "session_relative_ms", sample_format: "s16le", sample_rate_hz: this.rate,
      channels: all.length, codec: "pcm", container: "wav", layout: this.options.layout,
      tracks: this.manifestTracks.map(t => ({ ...t, channels: [...t.channels] })), segments, gaps, playout,
      capture_complete: final && gaps.length === 0 && segments.length > 0, duration_ms: duration };
  }
  private maybeManifest(force: boolean): void {
    if (this.manifestQueued || this.manifestDisabled || this.manifestFallback || this.finalEnd !== undefined) return;
    if (this.mode === "terminal") return;
    const end = this.settledEnd();
    if (end <= this.lastPutEnd || !this.latestCapability()) return;
    const due = force || this.sinceManifest >= 4 || this.ms(end, false) - this.ms(this.lastPutEnd, false) >= 30000;
    if (!due) return;
    this.manifestQueued = true; this.outstanding++;
    const controller = new AbortController();
    this.host.enqueue({ lane: this.recordingId, bytes: 0, controller,
      run: async signal => { try { await this.putManifest(this.settledEnd(), false, signal); } finally { this.manifestQueued = false; this.manifestJobDone(); } },
      denied: () => this.mode === "terminal" && this.terminalReason !== "policy_denied" ? "incomplete" : undefined,
      fail: () => { this.manifestQueued = false; this.manifestJobDone(); } });
  }
  private manifestJobDone(): void {
    this.outstanding--;
    if (this.outstanding === 0) { const waiters = this.idleWaiters; this.idleWaiters = []; for (const w of waiters) w(); }
  }
  private async putManifest(end: number, final: boolean, signal: AbortSignal, extra?: Piece, holder?: UploadOutcome): Promise<boolean> {
    if (end <= 0 && !final) return false;
    const manifest = this.buildManifest(end, final, extra);
    if (!validAttachmentManifest(manifest)) { this.manifestDisabled = true; return false; }
    const body = JSON.stringify(manifest);
    if (new TextEncoder().encode(body).byteLength > MAX_MANIFEST_BYTES) {
      // Closes as partial at the last successful revision; the writer stops retaining.
      this.manifestDisabled = true; this.truncated = true; return false;
    }
    const revision = this.revision + 1;
    const key = attachmentIdempotencyKey("manifest", this.recordingId, revision);
    this.cuts.push(end);
    const holders = holder ? [holder] : this.capabilities();
    for (const candidate of holders) {
      if (signal.aborted) return false;
      let result = await this.host.putManifest(candidate.upload!.write_capability, this.recordingId, body, key, signal, false);
      if (result === "credential_scope_unsupported" || result === "expired") {
        if (await this.host.renewUpload(candidate, signal)) result = await this.host.putManifest(candidate.upload!.write_capability, this.recordingId, body, key, signal, true);
      }
      if (result === "ok") {
        this.revision = revision; this.lastPutEnd = end; this.sinceManifest = 0;
        this.publishedSegments = manifest.segments.length; this.publishedComplete = manifest.capture_complete;
        this.publishedGapMs = manifest.gaps.reduce((sum, gap) => sum + gap.duration_ms, 0);
        return true;
      }
      if (result === "credential_scope_unsupported" || result === "policy_denied" || result === "conflict") {
        // BR-7 fallback: put the manifest before complete for the remaining segments.
        if (!final && !this.manifestFallback && !holder) { this.manifestFallback = true; return false; }
        if (holder) throw new Error("manifest");
        continue;
      }
      if (result === "expired") continue; // an older completed segment's capability may still be valid (design 3.3)
      return false;
    }
    return false;
  }
  private async fallbackManifest(piece: Piece, outcome: UploadOutcome, signal: AbortSignal): Promise<void> {
    piece.outcome = outcome;
    // Hold this segment's complete until a later segment is sealed or the recording finishes, so the LAST
    // segment's capability can still PUT the final manifest (no completed capability can do it afterwards).
    const later = () => this.finalEnd !== undefined || this.mode === "terminal" || signal.aborted ||
      this.tracks.some(track => track.pieces.some(p => p !== piece && p.kind === "segment" && p.state !== "done" && p.state !== "failed"));
    if (!later()) {
      await new Promise<void>(resolve => {
        const done = () => { clearTimeout(timer); signal.removeEventListener("abort", done); resolve(); };
        const timer = setTimeout(done, MAX_SEGMENT_MS + 5000); (timer as unknown as { unref?: () => void }).unref?.();
        this.sealWaiters.push(done); signal.addEventListener("abort", done, { once: true });
      });
    }
    // Prospectively include this segment; its own capability authorizes the PUT.
    const savedState = piece.state; piece.state = "done"; piece.index = this.completed;
    const end = this.settledEnd();
    let pending = 0;
    for (const track of this.tracks) for (const p of track.pieces) if (p !== piece && p.kind === "segment" && p.state !== "done" && p.state !== "failed") pending++;
    piece.state = savedState;
    // The last segment of a finishing recording carries the FINAL manifest (no completed capability can PUT it later).
    const final = this.finalEnd !== undefined && pending === 0 && end === this.finalEnd;
    let ok = false;
    try { ok = await this.putManifest(end, final, signal, piece, outcome); } catch { ok = false; }
    if (ok && final) this.finalPublished = true;
    piece.index = undefined;
    if (!ok) { this.manifestDisabled = true; throw new Error("manifest"); }
  }

  // --- finish / abort ------------------------------------------------------------------------------
  private discardAll(): void {
    for (const track of this.tracks) {
      if (track.chunkBytes) this.host.releaseBytes(track.chunkBytes);
      track.chunks = []; track.chunkBytes = 0; track.blockData = 0; track.runFrames = 0; track.carry = undefined;
      for (const piece of track.pieces) {
        if (piece.kind !== "segment") continue;
        if (piece.state === "sealed") { if (piece.bytes) this.host.releaseBytes(piece.bytes); piece.bytes = 0; piece.state = "failed"; piece.failure = "incomplete"; piece.source = undefined; }
        else if (piece.state === "queued" || piece.state === "uploading") piece.job?.controller.abort();
      }
    }
  }
  private dropTrack(track: Track): void {
    if (track.chunkBytes) this.host.releaseBytes(track.chunkBytes);
    track.chunks = []; track.chunkBytes = 0; track.blockData = 0;
    if (track.runFrames) { this.appendGap(track, "policy_denied", track.runFrames); track.runFrames = 0; }
    for (const piece of track.pieces) if (piece.kind === "segment" && (piece.state === "sealed")) {
      if (piece.bytes) this.host.releaseBytes(piece.bytes); piece.bytes = 0; piece.state = "failed"; piece.failure = "policy_denied"; piece.source = undefined;
    }
  }
  private policyStop(): void {
    // Tightening: retained bytes are freed and the remaining timeline becomes a policy_denied gap.
    for (const track of this.tracks) {
      if (track.blockData) this.releaseTrackBlock(track);
      if (track.runFrames) { const frames = track.runFrames; this.host.releaseBytes(track.chunkBytes); track.chunks = []; track.chunkBytes = 0; track.runFrames = 0;
        track.pieces.push({ kind: "gap", track: track.ordinal, start: track.runStart, end: track.runStart + frames, reason: "policy_denied" }); track.runStart = track.pos; }
      for (const piece of track.pieces) if (piece.kind === "segment" && piece.state === "sealed") {
        if (piece.bytes) this.host.releaseBytes(piece.bytes); piece.bytes = 0; piece.state = "failed"; piece.failure = "policy_denied"; piece.source = undefined;
      }
    }
    this.terminal("policy_denied");
  }
  private sealAll(): void {
    for (const track of this.tracks) {
      if (track.carry) track.carry = undefined;
      if (track.fill) {
        const frames = track.fill;
        if (track.blockGap === undefined) { track.runFrames += frames; track.blockData = 0; track.pos += frames; track.fill = 0; }
        else { this.closeRun(track); this.appendGap(track, track.blockGap, frames); track.pos += frames; track.fill = 0; track.blockGap = undefined; }
      }
      this.closeRun(track);
    }
    const end = Math.max(0, ...this.tracks.map(track => track.pos));
    for (const track of this.tracks) if (track.pos < end) { this.appendGap(track, track.denied ? "policy_denied" : "source_unavailable", end - track.pos); track.pos = end; }
    this.finalEnd = end;
    this.wakeSeal();
  }
  private idle(deadline: number): Promise<void> {
    if (this.outstanding === 0) return Promise.resolve();
    return new Promise(resolve => {
      const done = () => { clearTimeout(timer); resolve(); };
      // The caller awaits finish(): a ref'd timer bounded by its own deadline gives it the grace period.
      const timer = setTimeout(() => { this.idleWaiters = this.idleWaiters.filter(w => w !== done); resolve(); }, Math.max(0, deadline - this.host.clock.now()));
      this.idleWaiters.push(done);
    });
  }
  finish(timeoutMs?: number): Promise<AttachmentRecordingResult> {
    if (!this.resultPromise) this.resultPromise = this.finishInternal(timeoutMs).catch(() => this.result("incomplete"));
    return this.resultPromise;
  }
  private async finishInternal(timeoutMs?: number): Promise<AttachmentRecordingResult> {
    const budget = Math.min(Number.isFinite(timeoutMs) && timeoutMs! >= 0 ? timeoutMs! : this.host.finalizationTimeoutMs, this.host.finalizationTimeoutMs);
    const start = this.host.clock.now(); const deadline = start + budget;
    if (this.mode === "pending" || (this.mode === "terminal" && this.revision === 0 && !this.latestCapability() && this.admissionReason !== "queued")) {
      const denied = this.admissionReason !== "queued";
      this.discardAll(); this.terminal(this.terminalReason ?? this.admissionReason); this.closeOnce();
      return this.result(denied ? "denied" : undefined);
    }
    const aborted = this.terminalReason === "incomplete" && this.mode === "terminal";
    if (aborted) { this.closeOnce(); return this.result(); }
    const wasTerminal = this.mode === "terminal";
    if (!wasTerminal) this.mode = "finishing";
    this.sealAll();
    this.release();
    const reserve = Math.min(2000, budget / 4);
    await this.idle(deadline - reserve);
    // At the deadline: queued and in-flight segments are dropped and become incomplete gaps.
    for (const track of this.tracks) for (const piece of track.pieces) {
      if (piece.kind === "segment" && piece.state !== "done" && piece.state !== "failed") {
        piece.job?.controller.abort();
        if (piece.state === "sealed" && piece.bytes) { this.host.releaseBytes(piece.bytes); piece.bytes = 0; }
        piece.state = "failed"; piece.failure = "incomplete"; piece.source = undefined;
      }
    }
    if (this.latestCapability() && !this.manifestDisabled && !this.finalPublished) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), Math.max(1, deadline - this.host.clock.now()));
      try { await this.raceDeadline(this.putManifest(this.finalEnd ?? 0, true, controller.signal), deadline); }
      catch { /* bounded */ } finally { clearTimeout(timer); controller.abort(); }
    }
    this.terminal(this.terminalReason ?? "incomplete");
    this.closeOnce();
    return this.result();
  }
  private raceDeadline<T>(promise: Promise<T>, deadline: number): Promise<T | undefined> {
    return new Promise(resolve => {
      const timer = setTimeout(() => resolve(undefined), Math.max(0, deadline - this.host.clock.now()));
      promise.then(value => { clearTimeout(timer); resolve(value); }, () => { clearTimeout(timer); resolve(undefined); });
    });
  }
  private result(forced?: "denied" | "incomplete"): AttachmentRecordingResult {
    const state: AttachmentRecordingResult["state"] = forced === "denied" || (this.admissionReason !== "queued" && this.revision === 0 && !this.completed) ? "denied" :
      this.revision === 0 ? "unpublished" : this.publishedComplete ? "published" : "partial";
    const reason = state === "published" ? undefined : String(this.terminalReason ?? this.admissionReason);
    return Object.freeze({ recordingId: this.recordingId, state, ...(reason ? { reason } : {}), segmentsPublished: this.publishedSegments,
      gapMs: this.publishedGapMs, captureComplete: this.publishedComplete, manifestRevision: this.revision });
  }
  abort(): void {
    try {
      if (this.mode === "terminal" && this.closed) return;
      this.discardAll(); this.terminal("incomplete");
      if (!this.resultPromise) this.resultPromise = Promise.resolve(this.result());
      this.closeOnce();
    } catch { /* abort never throws */ }
  }
  /** Manager shutdown/fork-style invalidation: finish with the remaining budget. */
  shutdown(timeoutMs: number): Promise<AttachmentRecordingResult> { return this.finish(timeoutMs); }

  /** Owned WAV (tier wav_recording): segments are pre-built lease ranges. */
  loadFileSegments(segments: readonly RecordingFileSegment[], totalFrames: number): void {
    if (this.mode !== "active") return;
    for (const segment of segments) {
      const track = this.tracks[segment.track];
      track.pieces.push({ kind: "segment", track: segment.track, start: segment.startFrame, end: segment.endFrame, source: segment.source, bytes: 0,
        state: "sealed", occurrenceId: uuid(), attachmentId: uuid() });
    }
    for (const track of this.tracks) track.pos = totalFrames;
    this.finalEnd = totalFrames; this.mode = "finishing";
    this.release();
  }
  /** Owned WAV: wait for every segment and PUT the final manifest; cancellation drops the rest. */
  async completeFile(signal: AbortSignal): Promise<AttachmentCompletion["status"]> {
    await new Promise<void>(resolve => {
      if (this.outstanding === 0 || signal.aborted) { resolve(); return; }
      const done = () => { signal.removeEventListener("abort", done); resolve(); };
      this.idleWaiters.push(done); signal.addEventListener("abort", done, { once: true });
    });
    if (signal.aborted) { this.discardAll(); this.terminal("incomplete"); this.closeOnce(); return "incomplete"; }
    let failed: AttachmentCompletion["status"] | undefined;
    for (const track of this.tracks) for (const piece of track.pieces) if (piece.state === "failed") failed = failed ?? piece.failure;
    if (this.latestCapability() && !this.finalPublished && !this.manifestDisabled) await this.putManifest(this.finalEnd ?? 0, true, signal);
    this.terminal(failed ?? "incomplete"); this.closeOnce();
    return this.revision === 0 ? (failed ?? "incomplete") : failed ?? (this.publishedComplete ? "validating" : "incomplete");
  }
  /** @internal capture policy snapshot for owned file admission. */
  get capture(): AttachmentCapturePolicy | undefined { return this.options.capture; }
}
