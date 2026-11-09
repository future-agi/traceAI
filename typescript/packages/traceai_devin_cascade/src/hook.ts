#!/usr/bin/env node
import { handleHook, recordFailure } from "./runtime";

/** Post hooks run asynchronously in Cascade. Never exit 2 (the host's denial signal), print
 * content, spawn a daemon, or download code. Hard deadline includes stdin and export.
 */
const deadline = setTimeout(() => process.exit(0), 6000);
async function main(): Promise<void> {
  let size = 0;
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += bytes.length;
    if (size > 256 * 1024) throw new Error("HOOK_SIZE_LIMIT");
    chunks.push(bytes);
  }
  let value: unknown;
  try { value = JSON.parse(Buffer.concat(chunks).toString("utf8")); }
  catch { throw new Error("INVALID_HOOK"); }
  // working_directory is the host workspace, not a path from untrusted event JSON.
  const result = await handleHook(value, { projectDir: process.cwd() });
  if (!["PROMPT_OBSERVED", "DUPLICATE", "EXPORTED", "DISABLED"].includes(result)) await recordFailure(result);
}
void main().catch(error => recordFailure((error as Error).message)).finally(() => {
  clearTimeout(deadline);
  process.exit(0);
});
