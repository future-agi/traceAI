import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { lstat } from "node:fs/promises";
import { dirname, join, isAbsolute } from "node:path";
import { PassThrough, type Readable, type Writable } from "node:stream";
import { createTurnTracer, readJson, readProtected, type AdapterContext,
  type Manifest, type TurnTracer } from "@traceai/agent-core";
import { createCliStreamObserver, type CliStreamDiagnostics } from "./stream.js";
import { CLI_IMPORT_HOLD } from "./capabilities.js";

/** No imports/backfills and no second observer for the same source. The preview
 * import source-alias gate remains untouched. A fresh managed run must be the
 * sole Future AGI observer, including user/project and local-plugin hooks.
 */
export async function assertExclusiveCliStream(context: AdapterContext): Promise<void> {
  const homeCursor = dirname(context.config.paths.user);
  const manifest = await readJson<Manifest>(context.config.paths.manifest);
  if (Object.entries(manifest?.registrations ?? {}).some(([id, registration]) =>
    ["cursor", "cursor-cli"].includes(id) &&
    !["NOT_REGISTERED", "CONFIGURED_NOT_REGISTERED"].includes(registration.status)))
    throw new Error(CLI_IMPORT_HOLD);
  const files = [join(homeCursor, "hooks.json")];
  if (context.projectDir) files.push(join(context.projectDir, ".cursor", "hooks.json"));
  for (const file of files) {
    const value = await readProtected(file, false);
    if (value && /fi-cursor|fi-hook-to-otlp|@traceai\/(cursor-plugin|agents)|fi-agent[^\n]*cursor/.test(value))
      throw new Error(CLI_IMPORT_HOLD);
  }
  // Plugin discovery is not a qualified API yet. Fail closed instead of
  // scanning caches/transcripts or guessing that a marketplace hook is absent.
  try {
    await lstat(join(homeCursor, "plugins"));
    throw new Error("CURSOR_PLUGIN_CAPTURE_OWNERSHIP_UNVERIFIED");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
}
export function validatePrintArguments(args: readonly string[]): void {
  if (!args.some(arg => arg === "--print" || arg === "-p"))
    throw new Error("CURSOR_EXPLICIT_PRINT_REQUIRED");
  const values: string[] = [];
  args.forEach((arg, i) => {
    if (arg === "--output-format") values.push(args[i + 1]);
    if (arg.startsWith("--output-format=")) values.push(arg.slice("--output-format=".length));
  });
  if (values.length !== 1 || values[0] !== "stream-json")
    throw new Error("CURSOR_STREAM_JSON_REQUIRED");
  if (args.some(arg => arg === "--plugin-dir" || arg.startsWith("--plugin-dir=")))
    throw new Error("CURSOR_PLUGIN_CAPTURE_OWNERSHIP_UNVERIFIED");
  if (args.some(arg => ["--workspace", "--add-dir", "--worktree", "-w"].some(option =>
    arg === option || arg.startsWith(`${option}=`))))
    throw new Error("CURSOR_PROJECT_CONTEXT_OVERRIDE_UNQUALIFIED");
}

/** Internal qualification harness for a freshly launched, exclusive print run.
 * Not wired into the public adapter until real-host qualification; cannot import
 * old streams or reuse IDE success. stdout/stderr/exit status remain the host's.
 */
export async function runCursorCliStream(options: {
  context: AdapterContext;
  executable: string;
  args: string[];
  env?: NodeJS.ProcessEnv;
  stdin?: Readable;
  stdout?: Writable;
  stderr?: Writable;
  tracer?: TurnTracer;
}): Promise<{ code: number; diagnostics: CliStreamDiagnostics }> {
  if (!isAbsolute(options.executable)) throw new Error("CURSOR_ABSOLUTE_EXECUTABLE_REQUIRED");
  validatePrintArguments(options.args);
  await assertExclusiveCliStream(options.context);
  const env = { ...(options.env ?? process.env) };
  // Cursor's model tools must not inherit Future AGI destination credentials.
  delete env.FI_API_KEY; delete env.FI_SECRET_KEY;
  let tracer = options.tracer;
  if (!tracer) {
    try { tracer = createTurnTracer({ config: options.context.config }); }
    catch {
      const inert: TurnTracer = { startTurn: () => ({ end() {}, event() {}, gap() {}, child: () => ({ end() {} }) }),
        flush: async () => {}, shutdown: async () => {}, discard: async () => {},
        state: { pending: 0, exported: 0, dropped: 0 } };
      tracer = inert;
    }
  }
  const observer = createCliStreamObserver({ tracer, runId: randomUUID(), ownership: "exclusive-stream" });
  const stdout = options.stdout ?? process.stdout;
  const stderr = options.stderr ?? process.stderr;
  let code = 127;
  try {
    const child = spawn(options.executable, options.args, {
      cwd: options.context.projectDir,
      env, stdio: ["pipe", "pipe", "pipe"],
    });
    // Forward the bytes unchanged. A bounded observation tap never takes
    // ownership of stdout or consumes it in place of the caller.
    const tap = new PassThrough();
    tap.on("data", (chunk: Buffer) => {
      try { observer.push(chunk); } catch { /* observation fails open */ }
    });
    child.stdout.pipe(tap).pipe(stdout, { end: false });
    child.stderr.pipe(stderr, { end: false });
    const input = options.stdin ?? process.stdin;
    input.pipe(child.stdin);
    child.stdin.on("error", () => {});
    const forward = (signal: "SIGINT" | "SIGTERM") => () => { child.kill(signal); };
    const interrupt = forward("SIGINT"), terminate = forward("SIGTERM");
    process.on("SIGINT", interrupt); process.on("SIGTERM", terminate);
    const result = await new Promise<{ code: number | null; signal: NodeJS.Signals | null }>(resolve => {
      child.once("error", () => resolve({ code: 127, signal: null }));
      child.once("close", (code, signal) => resolve({ code, signal }));
    });
    process.off("SIGINT", interrupt); process.off("SIGTERM", terminate);
    input.unpipe(child.stdin);
    code = result.code ?? (result.signal === "SIGINT" ? 130 : result.signal === "SIGTERM" ? 143 : 1);
    try { observer.finish(result.code, result.signal); } catch { /* preserve host exit */ }
  } catch {
    try { observer.finish(127); } catch { /* preserve launch failure */ }
  } finally {
    // Core flush/shutdown are bounded; exporter errors cannot change host exit.
    try { await tracer.shutdown(); } catch { /* fail open */ }
  }
  return { code, diagnostics: observer.diagnostics };
}
