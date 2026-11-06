import { constants, openSync, closeSync, unlinkSync, readSync, writeSync } from 'node:fs';
import { join } from 'node:path';
import { randomBytes, randomUUID, createCipheriv, createDecipheriv, createHmac } from 'node:crypto';
import type { Attributes, Context, Span } from '@opentelemetry/api';
import { SemanticConventions as SC } from '@traceai/fi-semantic-conventions';
import type { ReadableSpan, SpanProcessor } from '@opentelemetry/sdk-trace-base';
import { resourceFromAttributes } from '@opentelemetry/resources';
import { BoundedSpanProcessor, collectionEnabled, ensurePrivateDirectory, type ResolvedConfig } from '@traceai/agent-core';
import { normalizeSpan } from './normalize';

// A one-shot OpenClaw CLI can call process.exit without awaiting external SDKs.
// Its existing launcher therefore owns delivery. Completed spans cross an
// inherited, immediately unlinked 0600 descriptor, encrypted with an ephemeral
// key. No plaintext spool, daemon, socket listener, copied interpreter or exit patch.
const HEADER = 12;
const MAGIC = 0x46494f31;
const MAX_BYTES = 4 * 1024 * 1024;
const MAX_FRAME = 256 * 1024;
function writeAll(fd: number, data: Buffer, offset: number): void {
  let written = 0;
  while (written < data.length) {
    const count = writeSync(fd, data, written, data.length - written, offset + written);
    if (!count) throw new Error('OPENCLAW_BRIDGE_WRITE');
    written += count;
  }
}
function readAll(fd: number, length: number, offset: number): Buffer {
  const result = Buffer.alloc(length);
  let read = 0;
  while (read < length) {
    const count = readSync(fd, result, read, length - read, offset + read);
    if (!count) throw new Error('OPENCLAW_BRIDGE_INCOMPLETE');
    read += count;
  }
  return result;
}
function header(bytes: number, dropped: number): Buffer {
  const result = Buffer.alloc(HEADER);
  result.writeUInt32BE(MAGIC, 0);
  result.writeUInt32BE(bytes, 4);
  result.writeUInt32BE(dropped, 8);
  return result;
}
function context(value: ReturnType<ReadableSpan['spanContext']>) {
  return { traceId: value.traceId, spanId: value.spanId, traceFlags: value.traceFlags };
}
function pack(span: ReadableSpan): Buffer {
  return Buffer.from(JSON.stringify({
    name: span.name, kind: span.kind, spanContext: context(span.spanContext()),
    parentSpanContext: span.parentSpanContext && context(span.parentSpanContext),
    startTime: span.startTime, endTime: span.endTime, duration: span.duration,
    attributes: span.attributes, status: span.status,
  }));
}
function unpack(data: Buffer, config: ResolvedConfig): ReadableSpan {
  const value = JSON.parse(data.toString('utf8'));
  const validContext = (c: any) => c && /^[0-9a-f]{32}$/.test(c.traceId) && /^[0-9a-f]{16}$/.test(c.spanId) &&
    Number.isInteger(c.traceFlags) && c.traceFlags >= 0 && c.traceFlags <= 255;
  const validTime = (t: unknown) => Array.isArray(t) && t.length === 2 &&
    t.every(n => Number.isSafeInteger(n) && n >= 0) && t[1] < 1e9;
  if (!validContext(value.spanContext) || (value.parentSpanContext && !validContext(value.parentSpanContext)) ||
    ![value.startTime, value.endTime, value.duration].every(validTime) ||
    typeof value.name !== 'string' || value.name.length > 128 ||
    !value.attributes || typeof value.attributes !== 'object' || Array.isArray(value.attributes) ||
    !Object.entries(value.attributes).every(([k, v]) => k.length <= 128 &&
      ((typeof v === 'string' && v.length <= 20000) || typeof v === 'boolean' || (typeof v === 'number' && Number.isFinite(v)))) ||
    !value.status || ![0, 1, 2].includes(value.status.code) ||
    (value.status.message !== undefined && (typeof value.status.message !== 'string' || value.status.message.length > 20000)))
    throw new Error('OPENCLAW_BRIDGE_SHAPE');
  return {
    ...value, spanContext: () => value.spanContext, ended: true, links: [], events: [],
    resource: resourceFromAttributes({ 'service.name': 'openclaw', project_name: config.projectName, project_type: 'observe' }),
    instrumentationScope: { name: '@traceai/openclaw' },
    droppedAttributesCount: 0, droppedEventsCount: 0, droppedLinksCount: 0,
  };
}

/** Child side: synchronous, bounded capture survives the host's explicit exit. */
export class LauncherCaptureProcessor implements SpanProcessor {
  private bytes = 0;
  private dropped = 0;
  constructor(private readonly config: ResolvedConfig, private readonly fd: number,
    private readonly key: Buffer, private readonly session?: string) {}
  onStart(_span: Span, _context: Context): void {}
  onEnd(span: ReadableSpan): void {
    try {
      if (!collectionEnabled(this.config)) return;
      const normalized = normalizeSpan(span, this.config, this.session);
      if (!normalized) return;
      const plaintext = pack(normalized);
      if (plaintext.length + 32 > MAX_FRAME || this.bytes + plaintext.length + 32 > MAX_BYTES) {
        this.recordDrop(); return;
      }
      const nonce = randomBytes(12);
      const cipher = createCipheriv('aes-256-gcm', this.key, nonce);
      const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
      const size = Buffer.alloc(4); size.writeUInt32BE(28 + ciphertext.length);
      const frame = Buffer.concat([size, nonce, cipher.getAuthTag(), ciphertext]);
      writeAll(this.fd, frame, HEADER + this.bytes);
      // Publish only fully written frames. An interrupted trailing write is ignored.
      this.bytes += frame.length;
      writeAll(this.fd, header(this.bytes, this.dropped), 0);
    } catch { this.recordDrop(); }
  }
  private recordDrop(): void {
    this.dropped = Math.min(0xffffffff, this.dropped + 1);
    try { writeAll(this.fd, header(this.bytes, this.dropped), 0); } catch { /* Fail open. */ }
  }
  async forceFlush(): Promise<void> {}
  async shutdown(): Promise<void> { this.key.fill(0); }
}

/** One explicit local invocation is one prompt, even when native retries start new trees.
 * Remap the entire native coordinate (not just spanId), retaining it as provenance.
 * This context never enters the model/tool environment. Request usage stays on
 * native generations; opaque turn-only usage belongs to the prompt root once.
 */
function promptContext(config: ResolvedConfig, session?: string) {
  const rootContext = { traceId: randomBytes(16).toString('hex'), spanId: randomBytes(8).toString('hex'), traceFlags: 1 };
  const started = Date.now(), clock = process.hrtime.bigint();
  const seen = new Set<string>();
  const turnUsage: Attributes = {};
  const usageKeys = [SC.LLM_TOKEN_COUNT_PROMPT, SC.LLM_TOKEN_COUNT_COMPLETION,
    SC.LLM_TOKEN_COUNT_TOTAL, SC.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_READ,
    SC.LLM_TOKEN_COUNT_PROMPT_DETAILS_CACHE_WRITE, SC.LLM_TOKEN_COUNT_COMPLETION_DETAILS_REASONING];
  let hasRequests = false, lastModelFailed = false, latestModelEnd = -1;
  const remap = (native: ReturnType<ReadableSpan['spanContext']>) => ({
    traceId: rootContext.traceId,
    spanId: createHmac('sha256', rootContext.traceId).update(native.traceId + ':' + native.spanId).digest('hex').slice(0, 16),
    traceFlags: 1,
  });
  const attributes = {
    'fi.span.kind': 'AGENT', 'fi.agent.name': 'openclaw',
    'fi.agent.trace_unit': 'prompt', 'fi.agent.turn_correlation': 'launcher-invocation',
    ...(session ? { 'session.id': session } : {}),
    ...(config.userId ? { 'user.id': config.userId } : {}),
  };
  return {
    attach(span: ReadableSpan): ReadableSpan | undefined {
      const native = span.spanContext(), coordinate = native.traceId + ':' + native.spanId;
      if (seen.has(coordinate)) return;
      seen.add(coordinate);
      const childAttributes = { ...span.attributes };
      const isRequest = childAttributes[SC.FI_AGENT_FIDELITY] === 'native-request';
      const isTurn = childAttributes[SC.FI_USAGE_SCOPE] === 'turn';
      if (isRequest || isTurn) {
        const end = span.endTime[0] * 1e3 + span.endTime[1] / 1e6;
        if (end >= latestModelEnd) { latestModelEnd = end; lastModelFailed = span.status.code === 2; }
      }
      hasRequests ||= isRequest;
      if (isTurn) {
        for (const key of usageKeys) {
          const value = childAttributes[key];
          if (typeof value === 'number') turnUsage[key] = Number(turnUsage[key] ?? 0) + value;
          delete childAttributes[key];
        }
        childAttributes[SC.FI_USAGE_CONTRIBUTION] = 'none';
        childAttributes[SC.FI_AGENT_USAGE_ADDITIVE] = false;
        childAttributes['fi.agent.usage_owner'] = 'none';
      }
      return {
        ...span, spanContext: () => remap(native),
        parentSpanContext: span.parentSpanContext ? remap(span.parentSpanContext) : rootContext,
        attributes: {
          ...childAttributes, ...attributes, 'fi.span.kind': childAttributes['fi.span.kind'],
          'fi.agent.native.trace_id': native.traceId, 'fi.agent.native.span_id': native.spanId,
          ...(span.parentSpanContext ? { 'fi.agent.native.parent_span_id': span.parentSpanContext.spanId } : {}),
        },
      };
    },
    end(exitCode: number): ReadableSpan {
      const ownsTurnUsage = !hasRequests && Object.keys(turnUsage).length > 0;
      const success = exitCode === 0 && !lastModelFailed;
      const elapsed = process.hrtime.bigint() - clock;
      const start = BigInt(started) * 1000000n;
      const time = (ns: bigint): [number, number] => [Number(ns / 1000000000n), Number(ns % 1000000000n)];
      return {
        name: 'openclaw.prompt', kind: 0, spanContext: () => rootContext, parentSpanContext: undefined,
        startTime: time(start), endTime: time(start + elapsed), duration: time(elapsed), ended: true,
        attributes: { ...attributes,
          ...(ownsTurnUsage ? turnUsage : {}),
          [SC.FI_USAGE_CONTRIBUTION]: ownsTurnUsage ? 'additive' : 'none',
          [SC.FI_AGENT_USAGE_ADDITIVE]: ownsTurnUsage,
          [SC.FI_AGENT_USAGE_PLAN]: hasRequests ? 'request' : ownsTurnUsage ? 'turn' : 'unknown',
          ...(ownsTurnUsage ? { [SC.FI_USAGE_SCOPE]: 'turn' } : {}),
          ...(!seen.size ? { [SC.FI_AGENT_GAP]: 'MISSING_NATIVE_SPANS' } : {}),
          'fi.agent.usage_owner': ownsTurnUsage ? 'turn' : 'none',
          [SC.FI_AGENT_TIMING]: 'observed-event',
          [SC.FI_AGENT_OUTCOME]: success ? 'success' : exitCode === 130 || exitCode === 143 ? 'cancelled' : 'error' },
        status: { code: success ? 1 : 2 }, events: [], links: [],
        resource: resourceFromAttributes({ 'service.name': 'openclaw', project_name: config.projectName, project_type: 'observe' }),
        instrumentationScope: { name: '@traceai/openclaw' },
        droppedAttributesCount: 0, droppedEventsCount: 0, droppedLinksCount: 0,
      };
    },
  };
}

/** Parent side: all network delivery outlives the agent child, under one deadline. */
export async function createLauncherDelivery(config: ResolvedConfig, session?: string) {
  await ensurePrivateDirectory(config.paths.stateDir);
  const path = join(config.paths.stateDir, `.openclaw-${randomUUID()}`);
  const fd = openSync(path, constants.O_CREAT | constants.O_EXCL | constants.O_RDWR | constants.O_NOFOLLOW, 0o600);
  try { unlinkSync(path); writeAll(fd, header(0, 0), 0); }
  catch (error) { closeSync(fd); throw error; }
  const key = randomBytes(32);
  const processor = new BoundedSpanProcessor(config);
  const prompt = promptContext(config, session);
  let offset = 0, reportedDrops = 0, broken = false, finished: Promise<void> | undefined;
  const readAvailable = () => {
    if (broken) return;
    try {
      const info = readAll(fd, HEADER, 0);
      const length = info.readUInt32BE(4), dropped = info.readUInt32BE(8);
      if (info.readUInt32BE(0) !== MAGIC || length > MAX_BYTES || length < offset || dropped < reportedDrops)
        throw new Error('OPENCLAW_BRIDGE_HEADER');
      if (dropped > reportedDrops) processor.recordDropped(dropped - reportedDrops, 'CAPTURE_LIMIT');
      reportedDrops = dropped;
      while (offset < length) {
        const size = readAll(fd, 4, HEADER + offset).readUInt32BE();
        if (size < 28 || size + 4 > MAX_FRAME || offset + 4 + size > length)
          throw new Error('OPENCLAW_BRIDGE_FRAME');
        const frame = readAll(fd, size, HEADER + offset + 4);
        const decipher = createDecipheriv('aes-256-gcm', key, frame.subarray(0, 12));
        decipher.setAuthTag(frame.subarray(12, 28));
        const plaintext = Buffer.concat([decipher.update(frame.subarray(28)), decipher.final()]);
        const span = prompt.attach(unpack(plaintext, config));
        if (span) processor.onEnd(span);
        offset += 4 + size;
      }
    } catch { broken = true; processor.recordDropped(1, 'CAPTURE_CHANNEL_INVALID'); }
  };
  const timer = setInterval(readAvailable, 100);
  timer.unref();
  return {
    fd, key: key.toString('hex'), processor,
    finish(exitCode = 0): Promise<void> {
      if (finished) return finished;
      clearInterval(timer);
      readAvailable();
      const root = prompt.end(exitCode);
      if (root) processor.onEnd(root);
      finished = processor.shutdown().finally(() => { closeSync(fd); key.fill(0); });
      return finished;
    },
  };
}
