import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { constants, promises as fs } from "node:fs";
import { dirname, join } from "node:path";
import { parse, modify, applyEdits, type ParseError } from "jsonc-parser";
import {
  readProtected,
  writeProtected,
  contentHash,
  readJson,
  updateJson,
  ensurePrivateDirectory,
  type CoreAdapter,
  type AdapterContext,
  type AdapterInspection,
} from "@traceai/agent-core";
import { EVENTS } from "./mapper.js";
import { systemPolicyConflict } from "./host-policy.js";

export const VERSION = "0.1.0"; // x-release-please-version
export const TESTED_HOST = "0.67.0";
const prefix = "futureagi-gemini-";
const exec = promisify(execFile);
interface Owned {
  schema: 1;
  command: string;
  hash: string;
  runtime: string;
}
const ledger = (c: AdapterContext) =>
  join(c.config.paths.configDir, "gemini-registration.json");
const target = (c: AdapterContext) =>
  join(dirname(c.config.paths.user), "settings.json");
const quote = (s: string) => "'" + s.replace(/'/g, "'\\''") + "'";
function parseSettings(text: string): Record<string, any> {
  const errors: ParseError[] = [];
  const parsed = parse(text, errors, { allowTrailingComma: true });
  if (
    errors.length ||
    !parsed ||
    typeof parsed !== "object" ||
    Array.isArray(parsed)
  )
    throw new Error("INVALID_GEMINI_SETTINGS");
  return parsed;
}
function ownedHook(h: any): boolean {
  return typeof h?.name === "string" && h.name.startsWith(prefix);
}
function check(settings: Record<string, any>): string | undefined {
  if (
    settings.telemetry?.enabled === true ||
    ["1", "true"].includes(process.env.GEMINI_TELEMETRY_ENABLED ?? "")
  )
    return "NATIVE_TELEMETRY_CONFLICT";
  if (settings.hooksConfig?.enabled === false) return "HOST_HOOKS_DISABLED";
  if (
    settings.hooksConfig?.disabled?.some(
      (n: unknown) => typeof n === "string" && n.startsWith(prefix),
    )
  )
    return "HOST_HOOKS_DISABLED";
  return;
}
async function conflicts(
  context: AdapterContext,
  settings: Record<string, any>,
): Promise<string | undefined> {
  const error = (await systemPolicyConflict()) ?? check(settings);
  if (error) return error;
  if (
    context.projectDir &&
    join(context.projectDir, ".gemini", "settings.json") !== target(context)
  ) {
    const text = await readProtected(
      join(context.projectDir, ".gemini", "settings.json"),
      false,
    );
    if (text) {
      const project = parseSettings(text);
      if (check(project)) return check(project);
      if (
        Object.values(project.hooks ?? {}).some(
          (groups: any) =>
            Array.isArray(groups) &&
            groups.some((g) => g.hooks?.some(ownedHook)),
        )
      )
        return "DUPLICATE_REGISTRATION";
    }
  }
  return;
}
export function reconcile(
  text: string,
  command: string,
  previous?: Owned,
  remove = false,
): string {
  let result = text;
  const settings = parseSettings(text);
  for (const event of EVENTS) {
    const groups = settings.hooks?.[event] ?? [];
    if (!Array.isArray(groups)) throw new Error("INVALID_GEMINI_HOOKS");
    const formattingOptions = { insertSpaces: true, tabSize: 2 };
    // Edit just owned array members; never serialize unrelated hooks and their comments.
    for (let gi = groups.length - 1; gi >= 0; gi--) {
      const group = groups[gi];
      if (!group || !Array.isArray(group.hooks))
        throw new Error("INVALID_GEMINI_HOOKS");
      const owned = group.hooks
        .map((h: any, i: number) => (ownedHook(h) ? i : -1))
        .filter((i: number) => i >= 0);
      for (const hi of owned) {
        const h = group.hooks[hi];
        if (
          !previous ||
          h.command !== previous.command ||
          h.type !== "command" ||
          h.name !== prefix + event ||
          h.timeout !== 6000 ||
          Object.keys(h).length !== 4
        )
          throw new Error("REGISTRATION_CHANGED");
      }
      if (owned.length && owned.length === group.hooks.length) {
        result = applyEdits(
          result,
          modify(result, ["hooks", event, gi], undefined, {
            formattingOptions,
          }),
        );
      } else {
        for (const hi of owned.reverse())
          result = applyEdits(
            result,
            modify(result, ["hooks", event, gi, "hooks", hi], undefined, {
              formattingOptions,
            }),
          );
      }
    }
    if (!remove) {
      const group = {
        matcher: "*",
        hooks: [
          { name: prefix + event, type: "command", command, timeout: 6000 },
        ],
      };
      const exists = parseSettings(result).hooks?.[event] !== undefined;
      result = applyEdits(
        result,
        modify(
          result,
          exists ? ["hooks", event, -1] : ["hooks", event],
          exists ? group : [group],
          { formattingOptions, isArrayInsertion: exists },
        ),
      );
    }
  }
  return result;
}
async function persistentRuntime(
  context: AdapterContext,
): Promise<{ runtime: string; hash: string }> {
  const source = await fs.readFile(join(__dirname, "hook.cjs"));
  if (source.length > 4 * 1024 * 1024) throw new Error("RUNTIME_TOO_LARGE");
  const hash = contentHash(source.toString("utf8"));
  const directory = join(
    context.config.paths.configDir,
    "runtime",
    "gemini",
    VERSION,
  );
  await ensurePrivateDirectory(directory);
  const runtime = join(directory, `${hash}.cjs`);
  try {
    const file = await fs.open(
      runtime,
      constants.O_CREAT |
        constants.O_EXCL |
        constants.O_WRONLY |
        constants.O_NOFOLLOW,
      0o600,
    );
    try {
      await file.writeFile(source);
      await file.sync();
    } finally {
      await file.close();
    }
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== "EEXIST") throw e;
  }
  const file = await fs.open(
    runtime,
    constants.O_RDONLY | constants.O_NOFOLLOW,
  );
  try {
    const st = await file.stat();
    if (
      !st.isFile() ||
      st.nlink !== 1 ||
      st.mode & 0o077 ||
      st.size !== source.length ||
      st.uid !== process.getuid?.() ||
      contentHash(await file.readFile("utf8")) !== hash
    )
      throw new Error("RUNTIME_MODIFIED");
  } finally {
    await file.close();
  }
  return { runtime, hash };
}
async function inspect(context: AdapterContext): Promise<AdapterInspection> {
  const old = await readJson<Owned>(ledger(context));
  const settings = parseSettings(
    (await readProtected(target(context), false)) ?? "{}",
  );
  const conflict = await conflicts(context, settings);
  if (conflict)
    return {
      registered: false,
      detail: conflict,
      duplicate: conflict === "DUPLICATE_REGISTRATION",
    };
  if (!old) return { registered: false, detail: "NOT_REGISTERED" };
  let count = 0;
  for (const event of EVENTS) {
    const hooks = (settings.hooks?.[event] ?? [])
      .flatMap((g: any) => g.hooks ?? [])
      .filter(ownedHook);
    if (
      hooks.length !== 1 ||
      hooks[0].command !== old.command ||
      hooks[0].name !== prefix + event
    )
      return {
        registered: false,
        duplicate: hooks.length > 1,
        detail: "REGISTRATION_CHANGED",
      };
    count++;
  }
  const stat = await fs.lstat(old.runtime);
  if (
    stat.isSymbolicLink() ||
    !stat.isFile() ||
    stat.size > 4 * 1024 * 1024 ||
    stat.mode & 0o077 ||
    contentHash(await fs.readFile(old.runtime, "utf8")) !== old.hash
  )
    return { registered: false, detail: "RUNTIME_MODIFIED" };
  let trust: AdapterInspection["trust"] = "unknown";
  const trustPath =
    process.env.GEMINI_CLI_TRUSTED_FOLDERS_PATH ??
    join(dirname(context.config.paths.user), "trustedFolders.json");
  const trusts = parseSettings((await readProtected(trustPath, false)) ?? "{}");
  const project = await fs.realpath(context.projectDir ?? process.cwd());
  if (
    process.env.GEMINI_RESTRICTED_MODE === "true" ||
    process.env.GEMINI_CLI_TRUST_WORKSPACE === "false" ||
    trusts[project] === "DO_NOT_TRUST"
  )
    trust = "required";
  else if (
    settings.security?.folderTrust?.enabled === false ||
    process.env.GEMINI_CLI_TRUST_WORKSPACE === "true" ||
    trusts[project] === "TRUST_FOLDER"
  )
    trust = "confirmed";
  else if (!Object.keys(trusts).length) trust = "required";
  const version = await exec("gemini", ["--version"], {
    timeout: 10000,
    maxBuffer: 8192,
  })
    .then((r) => r.stdout.trim())
    .catch(() => undefined);
  return {
    registered:
      count === EVENTS.length && (!version || version === TESTED_HOST),
    hostVersion: version,
    trust,
    detail:
      version && version !== TESTED_HOST
        ? "UNTESTED_HOST_VERSION"
        : trust === "required"
          ? "TRUST_WORKSPACE_IN_GEMINI"
          : "EXCLUSIVE_HOOKS_TEXT_ONLY",
  };
}
export const geminiAdapter: CoreAdapter = {
  id: "gemini",
  version: VERSION,
  requiresWorker: false,
  async detect(context) {
    if (process.platform === "win32")
      return { supported: false, reason: "WINDOWS_ACL_UNSUPPORTED" };
    const { stdout } = await exec("gemini", ["--version"], {
      timeout: 10000,
      maxBuffer: 8192,
    });
    const version = stdout.trim();
    const settings = parseSettings(
      (await readProtected(target(context), false)) ?? "{}",
    );
    const reason = await conflicts(context, settings);
    return {
      supported: version === TESTED_HOST && !reason,
      version,
      reason:
        reason ??
        (version !== TESTED_HOST ? "UNTESTED_HOST_VERSION" : undefined),
    };
  },
  async install(context) {
    // Project policy is resolved by core at hook execution. Registration stays user-scoped to avoid executable project hooks and trust bypass.
    const saved = await readJson<Owned>(ledger(context));
    const { runtime, hash } = await persistentRuntime(context);
    const command = `${quote(process.execPath)} ${quote(runtime)} --config-dir ${quote(context.config.paths.configDir)}`;
    for (let attempt = 0; attempt < 3; attempt++) {
      const old = await readProtected(target(context), false),
        text = old ?? "{}\n";
      const conflict = await conflicts(context, parseSettings(text));
      if (conflict) return { registered: false, detail: conflict };
      const next = reconcile(text, command, saved);
      // Save ownership before host write so interruption is recoverable on the next setup.
      await updateJson<Owned>(ledger(context), () => ({
        schema: 1,
        command,
        hash,
        runtime,
      }));
      try {
        await writeProtected(
          target(context),
          next,
          old === undefined ? null : contentHash(old),
          false,
        );
        break;
      } catch (e) {
        if ((e as Error).message !== "CONCURRENT_SAVE" || attempt === 2)
          throw e;
      }
    }
    return inspect(context);
  },
  inspect,
  async uninstall(context) {
    const old = await readProtected(target(context), false),
      saved = await readJson<Owned>(ledger(context));
    if (old && saved)
      await writeProtected(
        target(context),
        reconcile(old, saved.command, saved, true),
        contentHash(old),
        false,
      );
    await fs.unlink(ledger(context)).catch(() => {});
    return { registered: false, detail: "UNINSTALLED" };
  },
};
