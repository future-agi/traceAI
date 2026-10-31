import { homedir } from "node:os";
import { dirname, isAbsolute, join } from "node:path";
import { mkdirSync, rmdirSync } from "node:fs";
import {
  applyEdits,
  modify,
  parse,
  parseTree,
  type Node,
  type ParseError,
} from "jsonc-parser";
import type {
  Activation,
  AdapterContext,
  OwnedChange,
} from "../../adapters.js";
import {
  AgentError,
  atomicProtected,
  protectedDirectory,
  protectedSnapshot,
  type ProtectedSnapshot,
  readProtected,
} from "../../security/files.js";
import { EVENTS, type CursorSurface } from "./codec.js";

interface Hook {
  command: string;
  type: "command";
  timeout: number;
}
export interface CursorInstallOptions {
  node: string;
  hookEntry: string;
  hooksFile?: string;
}
const quote = (s: string): string => "'" + s.replace(/'/g, "'\\''") + "'";
const activation = (installed: boolean): Activation => ({
  installed,
  trusted: "unknown",
  activated: false,
  restartRequired: installed,
});
function location(o: CursorInstallOptions): string {
  return o.hooksFile ?? join(homedir(), ".cursor", "hooks.json");
}
function commands(
  surface: CursorSurface,
  c: AdapterContext,
  o: CursorInstallOptions,
): Record<string, Hook> {
  if (
    ![o.node, o.hookEntry, c.socketPath].every(
      (p) => p.length <= 4096 && isAbsolute(p) && !/[\r\n\0]/.test(p),
    )
  )
    throw new AgentError("INVALID_HOOK_PATH", 2);
  return Object.fromEntries(
    EVENTS.map((event) => [
      event,
      {
        command: [
          o.node,
          o.hookEntry,
          "--surface",
          surface,
          "--event",
          event,
          "--route",
          c.routeRef,
          "--socket",
          c.socketPath,
        ]
          .map(quote)
          .join(" "),
        type: "command",
        timeout: 1,
      },
    ]),
  );
}
interface Owner {
  node: string;
  hookEntry: string;
  socketPath: string;
  routeRef: string;
}
type Owners = Partial<Record<CursorSurface, Owner>>;
function owners(path: string, text = read(path)): Owners {
  if (!text) return {};
  try {
    const d = JSON.parse(text);
    if (
      d.version !== 1 ||
      !d.owners ||
      typeof d.owners !== "object" ||
      Array.isArray(d.owners)
    )
      throw new Error();
    for (const [surface, value] of Object.entries(d.owners)) {
      const o = value as Owner;
      if (
        !["ide", "cli"].includes(surface) ||
        !o ||
        Object.keys(o).sort().join() !== "hookEntry,node,routeRef,socketPath" ||
        ![o.node, o.hookEntry, o.socketPath].every(
          (p) =>
            typeof p === "string" &&
            p.length <= 4096 &&
            isAbsolute(p) &&
            !/[\r\n\0]/.test(p),
        ) ||
        typeof o.routeRef !== "string" ||
        !/^[a-zA-Z0-9_.-]{1,128}$/.test(o.routeRef)
      )
        throw new Error();
    }
    return d.owners;
  } catch {
    throw new AgentError("CURSOR_OWNERSHIP_CONFLICT", 6);
  }
}
function ownerCommands(
  surface: CursorSurface,
  c: AdapterContext,
  o: CursorInstallOptions,
  owner?: Owner,
): Record<string, Hook> {
  return owner
    ? commands(
        surface,
        { ...c, socketPath: owner.socketPath, routeRef: owner.routeRef },
        owner,
      )
    : commands(surface, c, o);
}
function read(path: string): string {
  try {
    return readProtected(path, 65_536).toString();
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "ENOENT") return "";
    throw e;
  }
}
function document(text: string): {
  version: number;
  hooks: Record<string, Hook[]>;
} {
  const errors: ParseError[] = [];
  let nodes = 0;
  function unique(node: Node, depth = 0): void {
    if (++nodes > 4096 || depth > 32)
      throw new AgentError("CURSOR_CONFIG_CONFLICT", 6);
    if (node.type === "object") {
      const keys = node.children?.map((p) => p.children?.[0].value) ?? [];
      if (new Set(keys).size !== keys.length)
        throw new AgentError("CURSOR_CONFIG_CONFLICT", 6);
    }
    for (const child of node.children ?? []) unique(child, depth + 1);
  }
  if (text) {
    const tree = parseTree(text);
    if (tree) unique(tree);
  }
  const doc = text
    ? parse(text, errors, {
        allowTrailingComma: false,
        disallowComments: false,
      })
    : { version: 1, hooks: {} };
  if (
    errors.length ||
    !doc ||
    typeof doc !== "object" ||
    Array.isArray(doc) ||
    doc.version !== 1 ||
    (doc.hooks !== undefined &&
      (!doc.hooks || typeof doc.hooks !== "object" || Array.isArray(doc.hooks)))
  )
    throw new AgentError("CURSOR_CONFIG_CONFLICT", 6);
  doc.hooks ??= {};
  for (const event of EVENTS)
    if (doc.hooks[event] !== undefined && !Array.isArray(doc.hooks[event]))
      throw new AgentError("CURSOR_CONFIG_CONFLICT", 6);
  return doc;
}
function patch(
  text: string,
  owned: Record<string, Hook>,
  enabled: boolean,
): string {
  let result = text || '{\n  "version": 1,\n  "hooks": {}\n}\n';
  const d = document(result);
  if (
    enabled &&
    Object.values(d.hooks).some(
      (entries) =>
        Array.isArray(entries) &&
        entries.some(
          (entry) =>
            typeof entry?.command === "string" &&
            entry.command.includes("fi-hook-to-otlp"),
        ),
    )
  )
    throw new AgentError("CURSOR_LEGACY_EMITTER_CONFLICT", 6);
  for (const [event, hook] of Object.entries(owned)) {
    const existing = d.hooks[event] ?? [];
    const prefix = hook.command.split(" '--event'")[0];
    if (
      existing.some(
        (h) =>
          typeof h?.command === "string" &&
          h.command.startsWith(prefix) &&
          h.command !== hook.command,
      )
    )
      throw new AgentError("CURSOR_CONFIG_CONFLICT", 6);
    const matching = existing.filter((h) => h?.command === hook.command);
    if (
      matching.length > 1 ||
      matching.some((h) => JSON.stringify(h) !== JSON.stringify(hook))
    )
      throw new AgentError("CURSOR_CONFIG_CONFLICT", 6);
    const next = existing.filter((h) => h?.command !== hook.command);
    if (enabled) next.push(hook);
    if (JSON.stringify(existing) !== JSON.stringify(next))
      result = applyEdits(
        result,
        modify(result, ["hooks", event], next.length ? next : undefined, {
          formattingOptions: { insertSpaces: true, tabSize: 2, eol: "\n" },
        }),
      );
  }
  return result;
}
export async function cursorChange(
  action: "enable" | "disable" | "update" | "uninstall",
  surface: CursorSurface,
  c: AdapterContext,
  o: CursorInstallOptions,
): Promise<OwnedChange> {
  if (action === "update") throw new AgentError("HELD_BUNDLE_UPDATE", 3);
  if (action === "enable" && !c.preview)
    throw new AgentError("CURSOR_PREVIEW_REQUIRED", 3);
  if (c.captureMode === "import-only") {
    if (surface !== "cli" || !c.transcriptConsent)
      throw new AgentError("TRANSCRIPT_CONSENT_REQUIRED", 3);
    return {
      async commit() {
        return activation(action === "enable");
      },
      async rollback() {},
    };
  }
  if (c.transcriptConsent)
    throw new AgentError("HELD_CURSOR_TRANSCRIPT_SCHEMA", 5);
  const path = location(o),
    ownerPath = path + ".fi-agent-owned.json",
    ownerBefore = protectedSnapshot(ownerPath),
    priorOwners = owners(ownerPath, ownerBefore.bytes.toString()),
    snapshot = protectedSnapshot(path),
    before = snapshot.bytes.toString();
  const enable = action === "enable";
  const prior = ownerCommands(surface, c, o, priorOwners[surface]);
  const owned = enable ? commands(surface, c, o) : prior;
  // A changed executable must not abandon registrations from the previous install.
  // Disable them first; never guess ownership from an arbitrary command substring.
  if (
    enable &&
    JSON.stringify(prior) !== JSON.stringify(owned) &&
    patch(before, prior, false) !== before
  )
    throw new AgentError("CURSOR_CONFIG_CONFLICT", 6);
  const after = patch(before, owned, enable);
  const ownerAfter = JSON.stringify({
    version: 1,
    owners: {
      ...priorOwners,
      [surface]: enable
        ? {
            node: o.node,
            hookEntry: o.hookEntry,
            socketPath: c.socketPath,
            routeRef: c.routeRef,
          }
        : priorOwners[surface],
    },
  });
  let committed = false;
  // Never journal/back up the host config: it can contain credentials and other hooks' payloads.
  function mutate(
    expected: ProtectedSnapshot,
    next: string,
    persistOwner = false,
  ): void {
    protectedDirectory(dirname(path), true);
    const lock = join(dirname(path), ".fi-agent-cursor.lock");
    try {
      mkdirSync(lock, { mode: 0o700 });
    } catch {
      throw new AgentError("CURSOR_CONFIG_CONFLICT", 6);
    }
    try {
      const current = protectedSnapshot(path);
      if (
        current.identity !== expected.identity ||
        !current.bytes.equals(expected.bytes)
      )
        throw new AgentError("CURSOR_CONFIG_CONFLICT", 6);
      // Journal only bounded nonsecret ownership, BEFORE installing hooks. A crash
      // can leave unused metadata, but never installed hooks with no cleanup recipe.
      if (persistOwner) atomicProtected(ownerPath, ownerAfter, ownerBefore);
      if (next !== expected.bytes.toString())
        atomicProtected(path, next, expected);
    } catch (e) {
      if (e instanceof AgentError && e.code === "SOURCE_CHANGED")
        throw new AgentError("CURSOR_CONFIG_CONFLICT", 6);
      throw e;
    } finally {
      rmdirSync(lock);
    }
  }
  return {
    async commit() {
      mutate(snapshot, after, enable);
      committed = true;
      return activation(enable);
    },
    async rollback() {
      if (!committed) return;
      // Restore only our owned registrations, preserving unrelated concurrent edits/comments.
      const current = protectedSnapshot(path);
      const original = document(before);
      let next = patch(current.bytes.toString(), owned, false);
      for (const event of EVENTS)
        if (
          original.hooks[event]?.some((h) => h.command === owned[event].command)
        )
          next = patch(next, { [event]: owned[event] }, true);
      mutate(current, next);
    },
  };
}
export async function cursorProbe(
  surface: CursorSurface,
  c: AdapterContext,
  o: CursorInstallOptions,
): Promise<Activation> {
  if (c.captureMode === "import-only") return activation(false);
  const doc = document(read(location(o))),
    owned = ownerCommands(
      surface,
      c,
      o,
      owners(location(o) + ".fi-agent-owned.json")[surface],
    );
  return activation(
    EVENTS.every((event) =>
      doc.hooks[event]?.some(
        (h) => JSON.stringify(h) === JSON.stringify(owned[event]),
      ),
    ),
  );
}
