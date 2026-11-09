import { handleEvent } from './runtime.js';
import { neutral, type Payload } from './mapper.js';
import { nativeHost, recoverOnExit, startExitRecovery } from './recovery.js';

// No prompts, permissions or event-content logging, including on parser/network errors.
const event = process.argv[2] ?? '';
const recovery = event === '--recover-exit';
if (!recovery) process.stdout.write(neutral(event));
// Terminal hooks can settle a late transcript and late child checkpoints as well
// as wait for the event lock and export. The detached exit observer is session-bound.
const timeout = setTimeout(() => process.exit(0), recovery ? 24 * 60 * 60 * 1000 : event === 'Stop' ? 25000 : 8000);
const chunks: Buffer[] = [];
let bytes = 0;
process.stdin.on('data', (chunk: Buffer) => {
  bytes += chunk.length;
  if (bytes > 256 * 1024) process.exit(0);
  chunks.push(chunk);
});
process.stdin.on('error', () => process.exit(0));
process.stdin.on('end', () => {
  let payload: unknown;
  try { payload = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { clearTimeout(timeout); return; }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) { clearTimeout(timeout); return; }
  const p = payload as Payload;
  const capture = async (): Promise<void> => {
    if (recovery) return recoverOnExit({pid: Number(process.argv[3]), start: process.argv[4] ?? ''}, p);
    const host = event === 'PreInvocation' ? await nativeHost() : undefined;
    await handleEvent(event, p, {host});
    if (host && (p.invocationNum === 0 || p.invocationNum === undefined) && p.initialNumSteps !== 0)
      startExitRecovery(process.argv[1], host, p);
  };
  capture().catch(() => {}).finally(() => { clearTimeout(timeout); process.exitCode = 0; });
});
