import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { constants, promises as fs } from "node:fs";
import { createHash } from "node:crypto";
import { dirname, join, resolve } from "node:path";
import { homedir } from "node:os";
import {
  ensurePrivateDirectory,
  readJson,
  writeProtected,
  type AdapterContext,
  type AdapterInspection,
  type CoreAdapter,
} from "@traceai/agent-core";

export const VERSION = "0.1.0"; // x-release-please-version
export const PLUGIN_ID = "futureagi-tracing";
export const EVENTS = [
  "SessionStart",
  "UserPromptSubmit",
  "PreLLMCall",
  "PostLLMCall",
  "PreToolUse",
  "PostToolUse",
  "PostToolUseFailure",
  "PreCompact",
  "PostCompact",
  "SubagentStart",
  "SubagentStop",
  "Stop",
  "StopFailure",
  "SessionEnd",
] as const;
const exec = promisify(execFile);
interface HostPlugin {
  error?: { code: string };
  record?: {
    id: string;
    source: { path: string };
    package_sha256: string;
    version: string;
    enabled: boolean;
  };
  valid?: boolean;
  active?: boolean;
  runtime_capabilities?: {
    candidate: { kind: string; capability_id: string };
    status: string;
  }[];
}
interface Owned {
  source: string;
  digest: string;
}
export interface Binding {
  home: string;
  agentDir: string;
  configDir: string;
  stateHome: string;
  hideInputs: boolean;
  hideOutputs: boolean;
  hideErrorDetails: boolean;
}
/** No shell, no secret argv, no host trust mutations. Muse alone owns its plugin store. */
export function createMuseAdapter(binary = "muse"): CoreAdapter {
  async function command(
    args: string[],
    context: AdapterContext,
  ): Promise<string> {
    try {
      const result = await exec(binary, args, {
        cwd: context.projectDir,
        timeout: 15000,
        maxBuffer: 2 * 1024 * 1024,
        env: process.env,
      });
      return result.stdout;
    } catch (e) {
      const stdout = (e as { stdout?: string }).stdout;
      if (stdout) {
        try {
          const result = JSON.parse(stdout);
          if (result?.error?.code === "unknown-plugin") return stdout;
        } catch {
          /* no raw host errors */
        }
      }
      throw new Error("MUSE_COMMAND_FAILED");
    }
  }
  const marker = (c: AdapterContext) =>
    join(c.config.paths.configDir, "muse-registration.json");
  async function inspectRaw(c: AdapterContext): Promise<HostPlugin> {
    return JSON.parse(
      await command(["plugins", "inspect", PLUGIN_ID, "--json"], c),
    );
  }
  async function inspect(c: AdapterContext): Promise<AdapterInspection> {
    const host = await inspectRaw(c);
    if (host.error?.code === "unknown-plugin")
      return { registered: false, trust: "unknown" };
    const owned = await readJson<Owned>(marker(c));
    const ours =
      !!owned &&
      !!host.record &&
      resolve(host.record.source.path) === owned.source &&
      host.record.package_sha256 === owned.digest;
    const hooks =
      host.runtime_capabilities?.filter((x) => x.candidate.kind === "hook") ??
      [];
    const approved = EVENTS.every((event) =>
      hooks.some(
        (x) =>
          x.candidate.capability_id === event.toLowerCase() &&
          x.status === "trusted_enabled",
      ),
    );
    const gap = await readJson<{ lastGap?: string }>(
      join(c.config.paths.stateDir, "muse-gaps.json"),
    );
    return {
      hostVersion: (await command(["--version"], c)).trim(),
      registered: ours && host.valid === true && host.active === true,
      duplicate: !ours && !!host.record,
      trust: approved ? "confirmed" : "required",
      // Approval is not activation. The core checks observed delivery metadata separately.
      activated: false,
      issues: ["MUSE_TRANSCRIPT_AND_CHILD_ANCESTRY_UNQUALIFIED"],
      detail: !ours
        ? "MUSE_REGISTRATION_NOT_OWNED"
        : !approved
          ? "Review hooks, then run muse plugins approve futureagi-tracing; restart Muse."
          : `MUSE_PREVIEW_ONLY; host payloads over 256 KiB are never delivered. ${gap?.lastGap ?? "Full transcript enrichment is not qualified."}`,
    };
  }
  return {
    id: "muse",
    version: VERSION,
    requiresWorker: false,
    async detect(c) {
      if (process.platform !== "linux")
        return { supported: false, reason: "LINUX_QUALIFICATION_ONLY" };
      try {
        const version = (await command(["--version"], c)).trim();
        return { supported: /\b1\.4\.14-R7309\.1\b/.test(version), version };
      } catch {
        return { supported: false, reason: "MUSE_NOT_FOUND" };
      }
    },
    async install(c) {
      const existing = await inspectRaw(c),
        previous = await readJson<Owned>(marker(c));
      if (
        existing.record &&
        (!previous ||
          resolve(existing.record.source.path) !== previous.source ||
          existing.record.package_sha256 !== previous.digest)
      )
        throw new Error("MUSE_REGISTRATION_NOT_OWNED");
      const source = join(c.config.paths.configDir, "muse-plugin");
      await ensurePrivateDirectory(source);
      await ensurePrivateDirectory(join(source, ".muse-plugin"));
      await ensurePrivateDirectory(join(source, "hooks"));
      const bytes = await fs.readFile(join(__dirname, "observer.cjs"));
      const digest = createHash("sha256").update(bytes).digest("hex");
      const bundle = `observer-${digest}.cjs`;
      const target = join(source, bundle);
      // A content-addressed regular file survives an npx cache eviction and needs no node_modules.
      try {
        const file = await fs.open(
          target,
          constants.O_CREAT |
            constants.O_EXCL |
            constants.O_WRONLY |
            constants.O_NOFOLLOW,
          0o600,
        );
        try {
          await file.writeFile(bytes);
          await file.sync();
        } finally {
          await file.close();
        }
      } catch (e) {
        if ((e as NodeJS.ErrnoException).code !== "EEXIST") throw e;
        const st = await fs.lstat(target);
        if (
          !st.isFile() ||
          st.isSymbolicLink() ||
          st.nlink !== 1 ||
          st.mode & 0o077 ||
          (process.getuid && st.uid !== process.getuid()) ||
          createHash("sha256")
            .update(await fs.readFile(target))
            .digest("hex") !== digest
        )
          throw new Error("MUSE_RUNTIME_CHANGED");
      }
      const binding: Binding = {
        home: process.env.HOME || homedir(),
        agentDir: dirname(c.config.paths.user),
        configDir: c.config.paths.configDir,
        stateHome: resolve(c.config.paths.stateDir, "..", ".."),
        hideInputs: [
          "FI_HIDE_INPUTS",
          "FI_HIDE_INPUT_TEXT",
          "FI_HIDE_INPUT_MESSAGES",
          "FI_HIDE_INPUT_IMAGES",
        ].some((k) => ["true", "1"].includes(process.env[k] ?? "")),
        hideOutputs: [
          "FI_HIDE_OUTPUTS",
          "FI_HIDE_OUTPUT_TEXT",
          "FI_HIDE_OUTPUT_MESSAGES",
        ].some((k) => ["true", "1"].includes(process.env[k] ?? "")),
        hideErrorDetails: ["true", "1"].includes(
          process.env.FI_HIDE_ERROR_DETAILS ?? "",
        ),
      };
      for (const event of EVENTS)
        await writeProtected(
          join(source, "hooks", `${event.toLowerCase()}.cjs`),
          `require('../${bundle}').main(${JSON.stringify(binding)}).catch(() => {});\n`,
        );
      await writeProtected(
        join(source, ".muse-plugin", "plugin.json"),
        JSON.stringify({
          schemaVersion: 1,
          name: PLUGIN_ID,
          displayName: "Future AGI tracing",
          version: VERSION,
          description:
            "Observational turn tracing. Captures content unless disabled in Future AGI settings.",
          compat: { source: "native", manifestDir: ".muse-plugin" },
          capabilities: {
            hooks: EVENTS.map((event) => ({
              id: event.toLowerCase(),
              event,
              command: [process.execPath, `hooks/${event.toLowerCase()}.cjs`],
              timeoutMs: 7000,
            })),
            skills: [],
            commands: [],
            mcpServers: [],
            reminders: [],
          },
        }),
      );
      await command(["plugins", "validate", source, "--json"], c);
      // Project-scoped Future AGI profiles share this one user plugin; no workspace-trust bypass.
      const installed: HostPlugin = JSON.parse(
        await command(
          ["plugins", "install", source, "--scope", "user", "--json"],
          c,
        ),
      );
      const record = installed.record ?? (await inspectRaw(c)).record;
      if (!record || resolve(record.source.path) !== source)
        throw new Error("MUSE_INSTALL_UNVERIFIED");
      await writeProtected(
        marker(c),
        JSON.stringify({ source, digest: record.package_sha256 }),
      );
      return inspect(c);
    },
    inspect,
    async uninstall(c) {
      const host = await inspectRaw(c),
        owned = await readJson<Owned>(marker(c));
      if (host.error?.code === "unknown-plugin") return { registered: false };
      if (
        !owned ||
        !host.record ||
        resolve(host.record.source.path) !== owned.source ||
        host.record.package_sha256 !== owned.digest
      )
        throw new Error("MUSE_REGISTRATION_NOT_OWNED");
      await command(["plugins", "remove", PLUGIN_ID, "--json"], c);
      return { registered: false };
    },
  };
}
export const museAdapter = createMuseAdapter();
