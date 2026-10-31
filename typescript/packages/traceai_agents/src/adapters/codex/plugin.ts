import { createHash } from "node:crypto";
import { existsSync, unlinkSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { homedir } from "node:os";
import type {
  AdapterContext,
  Activation,
  OwnedChange,
} from "../../adapters.js";
import {
  AgentError,
  atomicProtected,
  protectedDirectory,
  readProtected,
} from "../../security/files.js";

export const EVENTS = [
  "SessionStart",
  "UserPromptSubmit",
  "PreModelRequest",
  "PostModelRequest",
  "PostToolUse",
  "PostCompact",
  "Stop",
  "SubagentStop",
  "Interrupt",
  "SessionEnd",
];
export interface Registration {
  contract: 1;
  home: string;
  bundle: string;
  route: string;
  enabled: boolean;
}
const quote = (s: string) => "'" + s.replace(/'/g, "'\\''") + "'";
export function registrationPath(socket: string): string {
  return join(dirname(socket), "codex", "registration.json");
}
export function registration(socket: string): Registration {
  const r = JSON.parse(readProtected(registrationPath(socket)).toString());
  if (
    r.contract !== 1 ||
    typeof r.home !== "string" ||
    typeof r.bundle !== "string" ||
    r.route !== "codex.cli" ||
    typeof r.enabled !== "boolean"
  )
    throw new AgentError("INVALID_REGISTRATION", 3);
  return r;
}
export function activation(socket: string): Activation {
  let installed = false,
    activated = false;
  try {
    installed = registration(socket).enabled;
  } catch {
    /* not registered */
  }
  try {
    const a = JSON.parse(
      readProtected(
        join(dirname(socket), "codex", "activation.json"),
      ).toString(),
    );
    activated = installed && a.activated === true;
  } catch {
    /* no actual callback receipt */
  }
  return {
    installed,
    trusted: activated ? true : "unknown",
    activated,
    restartRequired: !activated,
  };
}
/** Stage an immutable LOCAL repository marketplace. Codex itself installs it and
 * asks for hook trust. We never rewrite config.toml, features, model settings,
 * credentials, hook trust or unrelated plugin registrations. */
export function preflightPlugin(
  action: "enable" | "disable" | "update" | "uninstall",
  c: AdapterContext,
  hookEntry: string,
  version: string,
): OwnedChange {
  if (!c.transcriptConsent)
    throw new AgentError("TRANSCRIPT_CONSENT_REQUIRED", 3);
  const file = registrationPath(c.socketPath),
    directory = dirname(file);
  const home = resolve(process.env.CODEX_HOME ?? join(homedir(), ".codex"));
  // Read only explicit metadata we own. Do not inspect the host's auth/config files.
  const before = existsSync(file) ? readProtected(file).toString() : undefined;
  const old: Registration | undefined = before ? JSON.parse(before) : undefined;
  const client = readFileSync(hookEntry, "utf8");
  // Host-supplied absolute plugin root points at its trusted immutable copy.
  // Never reference mutable code outside the bundle fingerprint.
  const command =
    quote(process.execPath) +
    ' "${PLUGIN_ROOT}/bin/observer.cjs" ' +
    ["--socket", c.socketPath, "--route", c.routeRef].map(quote).join(" ");
  const hooks = JSON.stringify({
    hooks: Object.fromEntries(
      EVENTS.map((event) => [
        event,
        [{ hooks: [{ type: "command", command, timeout: 1 }] }],
      ]),
    ),
  });
  const hash = createHash("sha256")
    .update(hooks)
    .update(client)
    .update(version)
    .digest("hex");
  const bundle = join(directory, "marketplace-" + hash.slice(0, 16));
  const expected: Registration = {
    contract: 1,
    home: old?.home ?? home,
    bundle: old?.bundle ?? bundle,
    route: "codex.cli",
    enabled:
      action === "enable" || (action === "update" && old?.enabled === true),
  };
  if (action === "update" && old?.bundle !== bundle)
    throw new AgentError("BUNDLE_REINSTALL_REQUIRED", 3);
  let committed = false;
  function compare(value: string | undefined): void {
    const current = existsSync(file)
      ? readProtected(file).toString()
      : undefined;
    if (current !== value) throw new AgentError("CONFIGURATION_CONFLICT", 6);
  }
  return {
    async commit() {
      compare(before);
      protectedDirectory(directory, true);
      if (action === "enable") {
        const contents = new Map([
          [
            ".agents/plugins/marketplace.json",
            JSON.stringify({
              name: "traceai-observers",
              plugins: [
                {
                  name: "traceai-codex",
                  source: { source: "local", path: "./codex" },
                },
              ],
            }),
          ],
          [
            "codex/.codex-plugin/plugin.json",
            JSON.stringify({
              name: "traceai-codex",
              version,
              description:
                "Structure-only local observability for Codex. No tools or model changes.",
            }),
          ],
          ["codex/hooks/hooks.json", hooks],
          ["codex/bin/observer.cjs", client],
        ]);
        for (const [path, text] of contents) {
          const target = join(bundle, path);
          if (existsSync(target)) {
            if (readProtected(target).toString() !== text)
              throw new AgentError("BUNDLE_CONFLICT", 6);
          } else {
            protectedDirectory(dirname(target), true);
            atomicProtected(target, text);
          }
        }
        expected.bundle = bundle;
      }
      // Disabled hooks remain host-neutral. Removing Codex's own installed copy
      // is deliberately a separate official `codex plugin remove` action.
      atomicProtected(file, JSON.stringify(expected));
      committed = true;
      try {
        unlinkSync(join(directory, "activation.json"));
      } catch {
        /* absent */
      }
      return activation(c.socketPath);
    },
    async rollback() {
      if (!committed) return;
      compare(JSON.stringify(expected));
      if (before !== undefined) atomicProtected(file, before);
      else unlinkSync(file);
    },
  };
}
