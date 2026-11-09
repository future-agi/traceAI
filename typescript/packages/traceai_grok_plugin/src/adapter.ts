import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { createInterface } from "node:readline/promises";
import { dirname, join, resolve } from "node:path";
import { homedir } from "node:os";
import { constants, promises as fs } from "node:fs";
import {
  contentHash,
  ensurePrivateDirectory,
  readJson,
  readProtected,
  updateJson,
  writeProtected,
  type AdapterContext,
  type AdapterInspection,
  type CoreAdapter,
} from "@traceai/agent-core";
import { HOOKS } from "./events.js";

export const VERSION = "0.1.0"; // x-release-please-version
export const HOST_VERSION = "1.0.77";
export const HOST_RANGE = ">=1.0.77 <2.0.0";
export function supportedHostVersion(version?: string): boolean {
  if (!version || !/^1\.\d+\.\d+$/.test(version)) return false;
  const [, minor, patch] = version.split(".").map(Number);
  return minor > 0 || patch >= 77;
}
export const PLUGIN_NAME = "futureagi-grok";
const exec = promisify(execFile);
const quote = (s: string): string => `'${s.replace(/'/g, `'\\''`)}'`;
async function grok(args: string[]): Promise<string> {
  const { stdout } = await exec("grok", ["--no-auto-update", ...args], {
    timeout: 15000,
    maxBuffer: 1024 * 1024,
  });
  return stdout;
}
interface Installation {
  root: string;
  hashes: Record<string, string>;
  interpreter?: string;
  hook?: string;
}
const receiptPath = (c: AdapterContext): string =>
  join(c.config.paths.configDir, "grok-plugin.json");
/** Materialize only our own plugin. The command pins Node and the persistent installed distribution. */
export async function materializePlugin(
  context: AdapterContext,
  installedPackage: string,
): Promise<string> {
  const root = join(
    context.config.paths.configDir,
    "plugins",
    `grok-${VERSION}`,
  );
  await ensurePrivateDirectory(root);
  const hook = resolve(installedPackage, "dist", "hook.js");
  // Reading confirms that setup did not point hooks at a missing or unbuilt distribution.
  if ((await readProtected(hook, false)) === undefined)
    throw new Error("RUNTIME_NOT_INSTALLED");
  const files = {
    ".grok-plugin/plugin.json":
      JSON.stringify(
        {
          name: PLUGIN_NAME,
          version: VERSION,
          description: "Future AGI turn and tool tracing for Grok Build",
        },
        null,
        2,
      ) + "\n",
    "hooks/hooks.json":
      JSON.stringify(
        {
          hooks: Object.fromEntries(
            HOOKS.map((event) => [
              event,
              [
                {
                  hooks: [
                    {
                      type: "command",
                      command: `${quote(process.execPath)} ${quote(hook)} 2>/dev/null || true`,
                      timeout: 10,
                    },
                  ],
                },
              ],
            ]),
          ),
        },
        null,
        2,
      ) + "\n",
  };
  const previous = await readJson<Installation>(receiptPath(context));
  const hashes: Record<string, string> = {};
  for (const [name, data] of Object.entries(files)) {
    const path = join(root, name),
      old = await readProtected(path);
    if (
      old !== undefined &&
      old !== data &&
      previous?.hashes[path] !== contentHash(old)
    )
      throw new Error("PLUGIN_MODIFIED");
    await writeProtected(
      path,
      data,
      old === undefined ? null : contentHash(old),
    );
    hashes[path] = contentHash(data);
  }
  await updateJson<Installation>(receiptPath(context), () => ({
    root,
    hashes,
    interpreter: process.execPath,
    hook,
  }));
  return root;
}
async function inspect(context: AdapterContext): Promise<AdapterInspection> {
  const receipt = await readJson<Installation>(receiptPath(context));
  if (!receipt)
    return { registered: false, trust: "required", detail: "SETUP_REQUIRED" };
  for (const [path, hash] of Object.entries(receipt.hashes)) {
    const value = await readProtected(path);
    if (!value || contentHash(value) !== hash)
      return { registered: false, trust: "unknown", detail: "PLUGIN_MODIFIED" };
  }
  try {
    if (!receipt.interpreter || !receipt.hook) throw new Error("RUNTIME_MISSING");
    await fs.access(receipt.interpreter, constants.X_OK);
    if ((await readProtected(receipt.hook, false)) === undefined)
      throw new Error("RUNTIME_MISSING");
  } catch {
    return { registered: false, trust: "unknown", detail: "RUNTIME_MISSING_OR_CHANGED", issues: ["RUNTIME_MISSING_OR_CHANGED"] };
  }
  // Native JSON is inspected, never modified. Qualification pins its schema in tests.
  const listed = JSON.parse(await grok(["plugin", "list", "--json"]));
  const rows: any[] = Array.isArray(listed) ? listed : (listed.plugins ?? []);
  const matches = rows.filter((p) => p.name === PLUGIN_NAME);
  const plugin = matches[0];
  const inventory = await grok(["inspect"]);
  const owned =
    plugin?.status === "installed" && plugin?.source === receipt.root;
  const registered =
    matches.length === 1 &&
    owned &&
    inventory.includes(`${PLUGIN_NAME} (user, enabled)`) &&
    inventory.includes(`file  plugin: ${PLUGIN_NAME}`);
  const legacy = await readProtected(
    join(dirname(context.config.paths.user), "hooks", "futureagi.json"),
    false,
  );
  const duplicate = matches.length > 1 || !!legacy?.includes("fi-hook-to-otlp");
  return {
    registered: registered && !duplicate,
    hostVersion: /\bgrok\s+v?(\d+\.\d+\.\d+)/.exec(await grok(["--version"]))?.[1],
    duplicate,
    trust: registered ? "confirmed" : "required",
    detail: registered
      ? "RESTART_GROK; if hooks are absent, use /reload-plugins"
      : `Approve the specific native plugin with: grok plugin install ${quote(receipt.root)} --trust`,
  };
}
export const grokAdapter: CoreAdapter = {
  id: "grok",
  version: VERSION,
  // Each native hook owns its bounded export. No daemon, listener or managed worker.
  requiresWorker: false,
  async detect() {
    try {
      const output = await grok(["--version"]),
        version = /\bgrok\s+v?(\d+\.\d+\.\d+)/.exec(output)?.[1];
      return { supported: supportedHostVersion(version), version, reason: supportedHostVersion(version) ? undefined : "GROK_VERSION_UNSUPPORTED" };
    } catch {
      return { supported: false, reason: "GROK_NOT_FOUND" };
    }
  },
  async install(context) {
    if (context.scope !== "user")
      return {
        registered: false,
        detail: "PROJECT_PLUGIN_INSTALL_UNQUALIFIED",
      };
    const runtime = join(
      process.env.XDG_DATA_HOME || join(homedir(), ".local", "share"),
      "futureagi",
      "agents",
      "runtime",
      `grok-${VERSION}`,
    );
    await ensurePrivateDirectory(runtime);
    await exec(
      "npm",
      [
        "install",
        "--prefix",
        runtime,
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        `@traceai/grok-plugin@${VERSION}`,
      ],
      { timeout: 120000, maxBuffer: 1024 * 1024 },
    );
    const root = await materializePlugin(
      context,
      join(runtime, "node_modules", "@traceai", "grok-plugin"),
    );
    // Without --trust the pinned host prints the proposed source and stops. Setup never grants execution trust.
    const existing = await inspect(context);
    if (existing.registered) return existing;
    if (process.stdin.isTTY && process.stderr.isTTY) {
      const rl = createInterface({
        input: process.stdin,
        output: process.stderr,
      });
      try {
        const answer = await rl.question(
          `Allow the Future AGI Grok plugin at ${JSON.stringify(root)} to run tracing hooks? [y/N] `,
        );
        if (answer.trim().toLowerCase() === "y")
          await grok(["plugin", "install", root, "--trust"]);
      } finally {
        rl.close();
      }
    } else await grok(["plugin", "install", root]).catch(() => {});
    return inspect(context);
  },
  inspect,
  async uninstall(context) {
    const status = await inspect(context);
    if (status.detail === "PLUGIN_MODIFIED" || status.duplicate)
      return {
        ...status,
        registered: true,
        detail: "OWNERSHIP_REVIEW_REQUIRED",
      };
    let registered = status.registered;
    if (status.detail === "RUNTIME_MISSING_OR_CHANGED") {
      // Broken runtime files must not prevent removing an otherwise owned native registration.
      const receipt = await readJson<Installation>(receiptPath(context));
      const listed = JSON.parse(await grok(["plugin", "list", "--json"]));
      const rows: any[] = Array.isArray(listed) ? listed : (listed.plugins ?? []);
      const matches = rows.filter((p) => p.name === PLUGIN_NAME);
      if (matches.length > 1 || matches.some((p) => p.source !== receipt?.root))
        return { registered: true, detail: "OWNERSHIP_REVIEW_REQUIRED" };
      registered = matches.length === 1 && matches[0].status === "installed";
    }
    if (registered)
      await grok([
        "plugin",
        "uninstall",
        PLUGIN_NAME,
        "--confirm",
        "--keep-data",
      ]);
    return { registered: false, activated: false, detail: "PROFILE_RETAINED" };
  },
};
