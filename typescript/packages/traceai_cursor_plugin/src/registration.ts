import { constants, promises as fs } from "node:fs";
import { dirname, join, resolve, isAbsolute, delimiter } from "node:path";
import { randomBytes } from "node:crypto";
import {
  applyEdits,
  modify,
  parse,
  parseTree,
  type Node,
  type ParseError,
} from "jsonc-parser";
import {
  contentHash,
  safeParents,
  withFileLock,
  writeProtectedUnderLock,
  readProtected,
  readJson,
  updateJson,
  ensurePrivateDirectory,
  type AdapterContext,
  type AdapterInspection,
} from "@traceai/agent-core";
import { EVENTS, VERSION } from "./events.js";
import { finisherStatus } from "./state.js";

export interface Hook {
  command: string;
  timeout: number;
  failClosed?: boolean;
}
export type Hooks = Record<string, Hook>;
interface Receipt {
  schemaVersion: 1;
  hooksFile: string;
  owned: Hooks[];
  runtime?: string;
  interpreter?: string;
  /** Present only while the complete host file is still our unchanged creation. */
  ownedFileHash?: string;
}
const marker = "fi-cursor-standard-v1";
const quote = (s: string) => `'${s.replace(/'/g, `'\\''`)}'`;
function unique(node: Node, budget = { count: 0 }, depth = 0): void {
  if (++budget.count > 4096 || depth > 32)
    throw new Error("CURSOR_CONFIG_TOO_COMPLEX");
  if (node.type === "object") {
    const names = (node.children ?? []).map((p) => p.children?.[0].value);
    if (new Set(names).size !== names.length)
      throw new Error("CURSOR_DUPLICATE_JSON_KEY");
  }
  for (const child of node.children ?? []) unique(child, budget, depth + 1);
}
function document(text: string): {
  version: number;
  hooks: Record<string, Hook[]>;
} {
  const errors: ParseError[] = [];
  const tree = parseTree(text, errors, { allowTrailingComma: true });
  if (tree) unique(tree);
  const doc = parse(text, errors, { allowTrailingComma: true });
  if (
    errors.length ||
    !doc ||
    Array.isArray(doc) ||
    doc.version !== 1 ||
    (doc.hooks !== undefined &&
      (!doc.hooks || typeof doc.hooks !== "object" || Array.isArray(doc.hooks)))
  )
    throw new Error("CURSOR_INVALID_HOOK_CONFIG");
  doc.hooks ??= {};
  for (const entries of Object.values(doc.hooks))
    if (!Array.isArray(entries)) throw new Error("CURSOR_INVALID_HOOK_CONFIG");
  return doc;
}
const same = (a: unknown, b: unknown) =>
  JSON.stringify(a) === JSON.stringify(b);
export function mergeHooks(
  before: string | undefined,
  known: Hooks[],
  desired?: Hooks,
  events: readonly string[] = EVENTS,
): string {
  let text = before ?? '{\n  "version": 1,\n  "hooks": {}\n}\n';
  const doc = document(text);
  for (const [event, entries] of Object.entries(doc.hooks)) {
    for (const entry of entries) {
      const h = entry as Hook;
      if (typeof h?.command !== "string") continue;
      const owned = known.some((k) => k[event]?.command === h.command);
      if (owned && !known.some((k) => same(k[event], h)))
        throw new Error("CURSOR_OWNED_HOOK_MODIFIED");
      // Never adopt, remove or silently disable registrations owned by the preview, legacy emitter or another plugin.
      if (
        !owned &&
        /fi-cursor-(?:standard|cloud)-v1|fi-cursor-hook|fi-hook-to-otlp|@traceai\/cursor-plugin/.test(
          h.command,
        )
      )
        throw new Error("CURSOR_DUPLICATE_REGISTRATION");
    }
  }
  for (const event of events) {
    const existing = doc.hooks[event] ?? [];
    const next = existing.filter(
      (h) => !known.some((k) => k[event]?.command === h?.command),
    );
    if (desired) next.push(desired[event]);
    if (same(existing, next)) continue;
    // Append/delete individual array entries rather than replacing unrelated objects and their comments.
    const remove = existing
      .map((h, i) =>
        known.some((k) => k[event]?.command === h?.command) ? i : -1,
      )
      .filter((i) => i >= 0)
      .reverse();
    for (const i of remove)
      text = applyEdits(
        text,
        modify(text, ["hooks", event, i], undefined, {
          formattingOptions: { insertSpaces: true, tabSize: 2 },
        }),
      );
    if (desired) {
      const current = document(text).hooks[event];
      text = applyEdits(
        text,
        modify(
          text,
          current ? ["hooks", event, -1] : ["hooks", event],
          current ? desired[event] : [desired[event]],
          {
            isArrayInsertion: true,
            formattingOptions: { insertSpaces: true, tabSize: 2, eol: "\n" },
          },
        ),
      );
    }
  }
  return text;
}
/** Bounded optimistic reconciliation. Does not alter the separately held #349 installer.
 * Cooperative writers lock; uncooperative editor saves are checked immediately before rename,
 * not advertised as a kernel compare-and-swap guarantee.
 */
export async function editHostFile(
  path: string,
  edit: (text: string | undefined) => string | undefined,
): Promise<void> {
  await safeParents(dirname(path), true);
  const parent = await fs.lstat(dirname(path));
  if (process.getuid && parent.uid !== process.getuid())
    throw new Error("UNSAFE_FILE_OWNER");
  await withFileLock(path, async (assertHeld) => {
    for (let attempt = 0; attempt < 3; attempt++) {
      const before = await readProtected(path, false);
      if (before !== undefined && (await fs.lstat(path)).mode & 0o022)
        throw new Error("UNSAFE_HOOK_FILE_MODE");
      const next = edit(before);
      if (next === before) return;
      await assertHeld();
      if (next === undefined) {
        if ((await readProtected(path, false)) !== before) continue;
        await fs.unlink(path);
        return;
      }
      if (Buffer.byteLength(next) > 65536) throw new Error("OVERSIZE_HOOK_CONFIG");
      try {
        await writeProtectedUnderLock(path, next,
          before === undefined ? null : contentHash(before), false);
        return;
      } catch (error) {
        if ((error as Error).message !== "CONCURRENT_SAVE" || attempt === 2) throw error;
      }
    }
    throw new Error("CONCURRENT_SAVE");
  }, { lockWaitMs: 6000 });
}
export function hooksFile(c: AdapterContext): string {
  // The reliable route is always a user hook. Project scope changes only the config binding,
  // avoiding double invocation when Cursor loads user AND workspace hook lists.
  return join(dirname(c.config.paths.user), "hooks.json");
}
const receiptFile = (c: AdapterContext) =>
  join(c.config.paths.configDir, "cursor-registration.json");
export function commands(
  runtime: string,
  configDir: string,
  interpreter = process.execPath,
): Hooks {
  for (const value of [runtime, configDir, interpreter])
    if (!isAbsolute(value) || /[\r\n\0]/.test(value))
      throw new Error("INVALID_HOOK_PATH");
  return Object.fromEntries(
    EVENTS.map((event) => [
      event,
      {
        command: [
          interpreter,
          join(runtime, "hook.cjs"),
          "--marker",
          marker,
          "--config-dir",
          configDir,
          "--event",
          event,
        ]
          .map(quote)
          .join(" "),
        timeout: 10,
        failClosed: false,
      },
    ]),
  );
}
function runtimeRoot(c: AdapterContext, home = dirname(dirname(c.config.paths.user))): string {
  return join(
    process.env.XDG_DATA_HOME || join(home, ".local", "share"),
    "futureagi",
    "agents",
    "runtime",
  );
}
/** Only generated, private Cursor runtimes are eligible; never follow links. */
export async function pruneRuntimes(
  c: AdapterContext,
  keep?: string,
): Promise<void> {
  const root = runtimeRoot(c);
  const entries = await fs.readdir(root).catch((error: NodeJS.ErrnoException) => {
    if (error.code !== "ENOENT") throw error;
    return [];
  });
  if (!entries.length) return;
  await safeParents(root);
  for (const name of entries) {
    if (!/^cursor-[0-9.]+-[a-f0-9]{20}$/.test(name)) continue;
    const file = join(root, name);
    if (file === keep) continue;
    const stat = await fs.lstat(file);
    if (
      !stat.isDirectory() ||
      stat.isSymbolicLink() ||
      stat.mode & 0o077 ||
      (process.getuid && stat.uid !== process.getuid())
    )
      continue;
    await fs.rm(file, { recursive: true });
  }
}
export async function installRuntime(
  c: AdapterContext,
  options: { entry?: "hook" | "cloud-hook"; prefix?: string; home?: string } = {},
): Promise<string> {
  const source = resolve(__dirname, "..", "runtime");
  const entry = options.entry ?? "hook";
  const names = entry === "cloud-hook" ? ["cloud-hook", "cloud-runtime"] : ["hook", "observer"];
  const prefix = options.prefix ?? "cursor";
  if (!["cursor", "cursor-cloud"].includes(prefix)) throw new Error("INVALID_RUNTIME_PREFIX");
  const assets = await Promise.all(
    names.map(async (name) => ({
      name: `${name === "cloud-hook" ? "hook" : name}.cjs`,
      sourceName: `${name}.cjs`,
      bytes: await fs.readFile(join(source, `${name}.cjs`)),
    })),
  );
  if (entry === "cloud-hook") {
    assets.push({ name: "node", sourceName: "node",
      bytes: Buffer.from(`#!/bin/sh\nexec ${quote(await pinnedInterpreter())} "$@"\n`) });
  }
  // The trusted interpreter that ran setup already works on this machine. Copying
  // Node causes a multi-second macOS security scan on the very first prompt.
  const digest = contentHash(
    assets.map((a) => a.bytes.toString()).join("\n") + process.execPath,
  ).slice(0, 20);
  const root = runtimeRoot(c, options.home);
  await ensurePrivateDirectory(root);
  const target = join(root, `${prefix}-${VERSION}-${digest}`);
  try {
    const stat = await fs.lstat(target);
    if (
      !stat.isDirectory() ||
      stat.isSymbolicLink() ||
      stat.mode & 0o077 ||
      (process.getuid && stat.uid !== process.getuid())
    )
      throw new Error("UNSAFE_RUNTIME");
    for (const { name, bytes } of assets) {
      const file = join(target, name),
        stat = await fs.lstat(file);
      if (
        !stat.isFile() ||
        stat.isSymbolicLink() ||
        stat.nlink !== 1 ||
        stat.mode & 0o077 ||
        stat.size !== bytes.length ||
        (process.getuid && stat.uid !== process.getuid()) ||
        !(await fs.readFile(file)).equals(bytes)
      )
        throw new Error("RUNTIME_CHANGED");
    }
    return target;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
  const temporary = target + "." + randomBytes(8).toString("hex");
  await fs.mkdir(temporary, { mode: 0o700 });
  try {
    for (const { name, sourceName, bytes } of assets) {
      await fs.writeFile(join(temporary, name), bytes, {
        flag: "wx",
        mode: name === "node" ? 0o700 : 0o600,
      });
      const license = await fs
        .readFile(join(source, `${sourceName}.LEGAL.txt`))
        .catch(() => undefined);
      if (license)
        await fs.writeFile(join(temporary, `${name}.LEGAL.txt`), license, {
          mode: 0o600,
        });
    }
    await fs.rename(temporary, target);
    return target;
  } finally {
    await fs.rm(temporary, { recursive: true, force: true });
  }
}
/** Keep the invoked stable symlink (PATH, Homebrew, nvm), not its versioned/volume realpath.
 * Only candidates resolving to the Node already executing setup are accepted; never execute a shim to probe it.
 */
export async function pinnedInterpreter(
  options: {
    argv0?: string;
    execPath?: string;
    path?: string;
    cwd?: string;
  } = {},
): Promise<string> {
  const invoked = options.argv0 ?? process.argv0;
  const executable = options.execPath ?? process.execPath;
  const cwd = options.cwd ?? process.cwd();
  const candidates = invoked.includes("/")
    ? [resolve(cwd, invoked)]
    : (options.path ?? process.env.PATH ?? "")
        .split(delimiter)
        .filter(isAbsolute)
        .map((dir) => join(dir, invoked));
  const real = await fs.realpath(executable);
  for (const candidate of candidates) {
    if (/[\r\n\0]/.test(candidate)) continue;
    try {
      if (
        (await fs.realpath(candidate)) !== real ||
        !(await fs.stat(candidate)).isFile()
      )
        continue;
      await fs.access(candidate, constants.X_OK);
      return candidate;
    } catch {
      /* stale PATH entries are not pins */
    }
  }
  return real;
}
export async function register(
  c: AdapterContext,
  runtime: string,
): Promise<AdapterInspection> {
  const file = hooksFile(c),
    receipt = await readJson<Receipt>(receiptFile(c));
  if (
    receipt &&
    (receipt.schemaVersion !== 1 ||
      receipt.hooksFile !== file ||
      !Array.isArray(receipt.owned))
  )
    throw new Error("CURSOR_OWNERSHIP_CONFLICT");
  const interpreter = await pinnedInterpreter();
  const desired = commands(runtime, c.config.paths.configDir, interpreter),
    known = receipt?.owned ?? [];
  // Verify conflicts before journaling ownership. Only exact owned commands are ever adopted.
  const before = await readProtected(file, false);
  mergeHooks(before, known, desired);
  const owned = [...known.filter((h) => !same(h, desired)), desired];
  if (owned.length > 8) throw new Error("CURSOR_OWNERSHIP_LIMIT");
  await updateJson<Receipt>(receiptFile(c), (old) => {
    if (!same(old, receipt)) throw new Error("CONCURRENT_SAVE");
    return { schemaVersion: 1, hooksFile: file, owned };
  });
  let ownedFileHash: string | undefined;
  await editHostFile(file, (current) => {
    const next = mergeHooks(current, owned, desired);
    if (current === undefined || (receipt?.ownedFileHash && contentHash(current) === receipt.ownedFileHash))
      ownedFileHash = contentHash(next);
    return next;
  });
  await updateJson<Receipt>(receiptFile(c), () => ({
    schemaVersion: 1,
    hooksFile: file,
    owned: [desired],
    runtime,
    interpreter,
    ownedFileHash,
  }));
  await pruneRuntimes(c, runtime);
  return inspect(c);
}
export async function inspect(c: AdapterContext): Promise<AdapterInspection> {
  const receipt = await readJson<Receipt>(receiptFile(c));
  if (!receipt)
    return { registered: false, detail: "NOT_REGISTERED", trust: "unknown" };
  const raw = await readProtected(hooksFile(c), false),
    doc = raw ? document(raw) : undefined;
  if (!doc) return { registered: false };
  let duplicate = false;
  try {
    mergeHooks(raw, receipt.owned);
  } catch {
    duplicate = true;
  }
  const registered = EVENTS.every(
    (event) =>
      doc.hooks[event]?.filter((h) =>
        receipt.owned.some((k) => same(k[event], h)),
      ).length === 1,
  );
  // Marketplace package deliberately has no capture hooks; a foreign/legacy registration is never suppressed by guessing ownership.
  if (c.projectDir) {
    const projectHooks = await readProtected(
      join(c.projectDir, ".cursor", "hooks.json"),
      false,
    ).catch(() => undefined); // unsafe project layer cannot suppress the user registration
    if (
      projectHooks &&
      /fi-cursor-(?:standard|cloud)-v1|fi-cursor-hook|fi-hook-to-otlp|@traceai\/cursor-plugin/.test(
        projectHooks,
      )
    )
      duplicate = true;
  }
  let runtimeIssue: string | undefined;
  if (receipt.interpreter) {
    try {
      await fs.access(receipt.interpreter, constants.X_OK);
    } catch {
      runtimeIssue = "CURSOR_INTERPRETER_MISSING";
    }
  } else runtimeIssue = "CURSOR_RUNTIME_UPDATE_REQUIRED";
  if (!runtimeIssue && receipt.runtime) {
    for (const name of ["hook.cjs", "observer.cjs"]) {
      try {
        await fs.access(join(receipt.runtime, name), constants.R_OK);
      } catch {
        runtimeIssue = "CURSOR_RUNTIME_MISSING";
      }
    }
  }
  const diagnostics = await readJson<{ gaps?: unknown }>(
    join(c.config.paths.stateDir, "cursor-diagnostics.json"),
  );
  const gaps = Array.isArray(diagnostics?.gaps)
    ? diagnostics.gaps
        .filter(
          (g): g is string => typeof g === "string" && /^[A-Z_]{1,64}$/.test(g),
        )
        .slice(0, 32)
    : [];
  return {
    finisher: await finisherStatus(c.config),
    registered: registered && !duplicate && !runtimeIssue,
    duplicate,
    activated: false,
    trust: "unknown",
    detail: runtimeIssue ?? "GUI_ACTIVATION_NOT_OBSERVED",
    issues: [...(runtimeIssue ? [runtimeIssue] : []), ...gaps],
  };
}
export async function unregister(
  c: AdapterContext,
): Promise<AdapterInspection> {
  const receipt = await readJson<Receipt>(receiptFile(c));
  if (!receipt) return { registered: false };
  await editHostFile(hooksFile(c), (before) => {
    if (before === undefined) return undefined;
    const next = mergeHooks(before, receipt.owned);
    return receipt.ownedFileHash === contentHash(before) ? undefined : next;
  });
  await pruneRuntimes(c);
  await fs.unlink(receiptFile(c));
  return { registered: false, activated: false };
}
