import { randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { cursorAdapters } from "../adapters/cursor/index.js";
import { STRUCTURE_POLICY, token } from "@traceai/agent-core";
import { observe } from "@traceai/agent-core/client";
import { AdapterRegistry, type AdapterContext } from "../adapters.js";
import {
  configTransaction,
  loadConfig,
  paths,
  type Paths,
  type Profile,
} from "../config.js";
import {
  AgentError,
  protectedDirectory,
  readProtected,
} from "../security/files.js";
import {
  FileSecretStore,
  OSSecretStore,
  newCredentials,
  parseAuth,
} from "../security/credentials.js";
import { DEFAULT_ENDPOINT, validateBinding } from "../export/endpoint.js";
import { runtimeRequest, Runtime } from "../runtime/daemon.js";
import { ImportPlanner } from "../import.js";
import { authInput } from "./input.js";
import { runAgent } from "./run.js";
const flags = new Set([
  "json",
  "preview",
  "stdin",
  "tty",
  "loopback-test",
  "check",
  "dry-run",
  "purge-capture",
  "loopback",
  "ingest-check",
]);
const valued = new Set([
  "profile",
  "project",
  "surface",
  "endpoint",
  "storage",
  "credential-file",
  "contract-manifest",
  "capture",
  "transcript",
  "path",
  "commit",
  "deadline",
  "mode",
  "schema",
]);
function parse(args: string[]): {
  pos: string[];
  options: Record<string, string | boolean>;
  child: string[];
} {
  const pos: string[] = [],
    options: Record<string, string | boolean> = {};
  let child: string[] = [];
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "--") {
      child = args.slice(i + 1);
      break;
    }
    if (!arg.startsWith("--")) {
      pos.push(arg);
      continue;
    }
    const key = arg.slice(2);
    if (Object.hasOwn(options, key))
      throw new AgentError("DUPLICATE_OPTION", 2);
    if (flags.has(key)) options[key] = true;
    else if (valued.has(key) && args[i + 1] && !args[i + 1].startsWith("--"))
      options[key] = args[++i];
    else throw new AgentError("INVALID_ARGUMENT", 2);
  }
  return { pos, options, child };
}
function required(
  options: Record<string, string | boolean>,
  key: string,
): string {
  const value = options[key];
  if (typeof value !== "string" || !value)
    throw new AgentError("MISSING_ARGUMENT", 2);
  return value;
}
function store(p: Paths, profile: Pick<Profile, "storage">) {
  return profile.storage === "file"
    ? new FileSecretStore(p.credentials)
    : new OSSecretStore();
}
export interface CLIOptions {
  version: string;
  entry: string;
  storageEntry: string;
  registry?: AdapterRegistry;
}
async function startRuntime(p: Paths, entry: string): Promise<void> {
  try {
    await runtimeRequest(p.socket, { operation: "status" }, 200);
    return;
  } catch {
    /* start outside hooks */
  }
  protectedDirectory(p.state, true);
  const child = spawn(process.execPath, [entry, "__daemon"], {
    detached: true,
    stdio: "ignore",
    env: {
      PATH: "/usr/bin:/bin",
      HOME: process.env.HOME ?? "/",
      FI_AGENT_CONFIG_HOME: p.config,
      FI_AGENT_STATE_HOME: p.state,
    },
  });
  child.unref();
  for (let attempt = 0; attempt < 50; attempt++) {
    await new Promise((r) => setTimeout(r, 50));
    try {
      await runtimeRequest(p.socket, { operation: "status" }, 100);
      return;
    } catch {
      /* bounded readiness */
    }
  }
  throw new AgentError("RUNTIME_UNAVAILABLE");
}
export async function main(
  args: string[],
  settings: CLIOptions,
): Promise<number> {
  const output = (value: object) =>
    process.stdout.write(
      JSON.stringify({ schema_version: 1, ...value }) + "\n",
    );
  try {
    const major = Number(process.versions.node.split(".")[0]),
      minor = Number(process.versions.node.split(".")[1]);
    if (!((major === 22 && minor >= 16) || major === 24))
      throw new AgentError("UNSUPPORTED_NODE", 5);
    if (args[0] === "--version") {
      process.stdout.write(settings.version + "\n");
      return 0;
    }
    if (args.length === 0 || args[0] === "--help") {
      output({
        state: "foundation-preview",
        code: 0,
        commands: [
          "auth add|remove",
          "enable",
          "disable",
          "update",
          "uninstall",
          "status",
          "doctor",
          "run",
          "import",
          "flush",
          "runtime start|stop|status",
        ],
        enabled_by_default: [],
        reasons: ["CURSOR_REQUIRES_EXPLICIT_PREVIEW"],
      });
      return 0;
    }
    const p = paths(),
      registry =
        settings.registry ??
        new AdapterRegistry(
          cursorAdapters({
            node: process.execPath,
            hookEntry: join(dirname(settings.entry), "fi-cursor-hook.cjs"),
          }),
        );
    if (args[0] === "__daemon") {
      const runtime = new Runtime(
        p,
        settings.storageEntry,
        settings.version,
        registry,
      );
      await runtime.start();
      process.once("SIGTERM", () => void runtime.stop());
      process.once("SIGINT", () => void runtime.stop());
      return 0;
    }
    const { pos, options, child } = parse(args),
      command = pos[0];
    if (command === "auth") {
      const name = required(options, "profile");
      if (!token(name)) throw new AgentError("INVALID_PROFILE", 2);
      if (pos[1] === "add") {
        const auth = parseAuth(await authInput(options));
        await configTransaction(p, async (config) => {
          const existing = config.profiles[name];
          const storage = options.storage ?? existing?.storage ?? "os";
          if (storage !== "file" && storage !== "os")
            throw new AgentError("INVALID_STORAGE", 2);
          if (existing && storage !== existing.storage)
            throw new AgentError("STORAGE_MIGRATION_REQUIRED", 3);
          const endpoint = String(
            options.endpoint ?? existing?.binding.endpoint ?? DEFAULT_ENDPOINT,
          );
          const project = required(options, "project");
          const binding = {
            id: existing?.binding.id ?? randomUUID(),
            endpoint,
            approvedOrigin: new URL(endpoint).origin,
            project,
            loopbackTest: options["loopback-test"] === true,
          };
          validateBinding(binding);
          if (
            existing &&
            (existing.binding.endpoint !== endpoint ||
              existing.binding.project !== project)
          )
            throw new AgentError("HELD_DESTINATION_CHANGED", 3);
          const profile: Profile = existing ?? {
            binding,
            storage,
            credentialRef: randomUUID(),
          };
          if (options["contract-manifest"]) {
            const manifest = JSON.parse(
              readProtected(required(options, "contract-manifest")).toString(),
            );
            if (
              manifest.contract !== "immutable-agent-v1" ||
              manifest.endpoint !== endpoint ||
              manifest.project !== project ||
              manifest.immutableAdmission !== true ||
              manifest.uniqueReads !== true ||
              manifest.usagePlans !== true
            )
              throw new AgentError("HELD_BACKEND_CONTRACT", 3);
            profile.binding.backendContract = "immutable-agent-v1";
          }
          const secrets = store(p, profile);
          const previous = existing
            ? await secrets.get(profile.credentialRef)
            : undefined;
          if (existing && !previous)
            throw new AgentError("HELD_IDENTITY_KEY", 3);
          await secrets.set(
            profile.credentialRef,
            newCredentials(auth, previous),
          );
          profile.removed = false;
          config.profiles[name] = profile;
        });
        // Rotation is explicit approval to release only authentication holds.
        // An unavailable runtime remains held until this action is repeated while running.
        try {
          await runtimeRequest(p.socket, {
            operation: "auth-rotated",
            profile: name,
          });
        } catch {
          /* safe hold */
        }
        output({
          state: "profile-approved",
          code: 0,
          reasons: ["BACKEND_PERSISTENCE_NOT_VERIFIED"],
        });
        return 0;
      }
      if (pos[1] === "remove") {
        await configTransaction(p, async (config) => {
          const profile = config.profiles[name];
          if (!profile) throw new AgentError("UNKNOWN_PROFILE", 2);
          for (const [id, route] of Object.entries(config.routes))
            if (route.profile === name) {
              await runtimeRequest(p.socket, {
                operation: "disable",
                route: id,
                purge: options["purge-capture"] === true,
              });
              route.route.enabled = false;
              route.route.epoch++;
            }
          await store(p, profile).remove(profile.credentialRef);
          profile.removed = true;
        });
        output({
          state: "auth-removed",
          code: 0,
          reasons: [
            "QUEUED_DATA_HELD",
            "IDENTITY_RECOVERY_REQUIRED_BEFORE_REUSE",
          ],
        });
        return 0;
      }
      throw new AgentError("INVALID_ARGUMENT", 2);
    }
    if (command === "runtime") {
      if (pos[1] === "start") {
        await startRuntime(p, settings.entry);
        output({ state: "running", code: 0 });
        return 0;
      }
      if (!["stop", "status"].includes(pos[1]))
        throw new AgentError("INVALID_ARGUMENT", 2);
      output(await runtimeRequest(p.socket, { operation: pos[1] }));
      return 0;
    }
    if (command === "status") {
      output(await runtimeRequest(p.socket, { operation: "status" }));
      return 0;
    }
    if (command === "flush") {
      const deadline = Number(options.deadline ?? 5000);
      if (!Number.isFinite(deadline) || deadline <= 0 || deadline > 60_000)
        throw new AgentError("INVALID_DEADLINE", 2);
      const result = await runtimeRequest(
        p.socket,
        { operation: "flush", deadlineMs: deadline },
        deadline + 500,
      );
      output(result);
      return result.code;
    }
    if (
      ![
        "enable",
        "disable",
        "update",
        "uninstall",
        "doctor",
        "run",
        "import",
      ].includes(command)
    )
      throw new AgentError("UNKNOWN_COMMAND", 2);
    const agent = pos[1];
    if (!agent) throw new AgentError("MISSING_AGENT", 2);
    const surface = required(options, "surface");
    const adapter = registry.get(agent, surface);
    const routeId = agent + "." + surface,
      config = loadConfig(p),
      installed = config.routes[routeId];
    const profileName = String(options.profile ?? installed?.profile ?? "");
    const profile = config.profiles[profileName];
    if (!profile || profile.removed) throw new AgentError("HELD_AUTH", 3);
    if (
      options.project !== undefined &&
      options.project !== profile.binding.project
    )
      throw new AgentError("PROJECT_BINDING_MISMATCH", 3);
    if (options.capture !== undefined && options.capture !== "structure")
      throw new AgentError("HELD_CONTENT_QUALIFICATION", 3);
    if (
      options.transcript !== undefined &&
      !["off", "structure"].includes(String(options.transcript))
    )
      throw new AgentError("HELD_CONTENT_QUALIFICATION", 3);
    const captureMode = options.mode ?? installed?.captureMode ?? "hooks";
    if (
      !["hooks", "import-only"].includes(String(captureMode)) ||
      (options.mode && agent !== "cursor")
    )
      throw new AgentError("INVALID_CAPTURE_MODE", 2);
    if (installed && options.mode && installed.captureMode !== captureMode)
      throw new AgentError("UNINSTALL_BEFORE_MODE_CHANGE", 6);
    const context: AdapterContext = {
      routeRef: routeId,
      socketPath: p.socket,
      surface,
      profile: profileName,
      binding: profile.binding,
      transcriptConsent:
        options.transcript === undefined
          ? installed?.transcriptConsent === true
          : options.transcript === "structure",
      preview: options.preview === true,
      captureMode: captureMode as "hooks" | "import-only",
    };
    if (command === "run") {
      if (!installed?.route.enabled) throw new AgentError("ENABLE_REQUIRED", 3);
      // Generic destinations are never overwritten. Agent-specific effective state is adapter preflight's job.
      if (
        process.env.OTEL_EXPORTER_OTLP_ENDPOINT ||
        process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
      )
        throw new AgentError("NATIVE_DESTINATION_CONFLICT", 6);
      await startRuntime(p, settings.entry);
      const result = await runAgent(adapter, context, child);
      if (result.signal) {
        process.kill(process.pid, result.signal);
        return 128;
      }
      return result.code ?? 1;
    }
    if (command === "doctor") {
      if (options["ingest-check"] || options.loopback)
        throw new AgentError("UNQUALIFIED_DIAGNOSTIC", 5);
      const activation = await adapter.probe(context);
      let runtime = "unknown";
      try {
        const status = await runtimeRequest(
          p.socket,
          { operation: "status" },
          200,
        );
        const receipt = status.agents?.find(
          (r: { agent: string; surface: string }) =>
            r.agent === agent && r.surface === surface,
        );
        runtime = "running";
        if (
          activation.installed &&
          receipt?.enabled &&
          receipt.activation?.activated
        ) {
          activation.activated = true;
          activation.restartRequired = false;
        }
      } catch {
        /* unavailable is unknown, never success */
      }
      output({
        state: activation.activated ? "activated" : "waiting-for-activation",
        code: 0,
        activation,
        runtime,
        reasons:
          agent === "cursor"
            ? ["FIXTURE_QUALIFIED_PREVIEW", "PERSISTED_TRANSCRIPT_SCHEMA_HELD"]
            : [],
        acceptance: "unknown",
        persistence: "unknown",
      });
      return 0;
    }
    if (command === "import") {
      if (!context.transcriptConsent)
        throw new AgentError("TRANSCRIPT_CONSENT_REQUIRED", 3);
      const secret = await store(p, profile).get(profile.credentialRef);
      if (!secret) throw new AgentError("HELD_IDENTITY_KEY", 3);
      if (agent === "cursor" && installed?.captureMode !== "import-only")
        throw new AgentError("HELD_UNPROVEN_SOURCE_ALIAS", 3);
      const schema =
        options.schema ??
        (adapter.capability.schemas.length === 1
          ? adapter.capability.schemas[0]
          : undefined);
      if (
        typeof schema !== "string" ||
        !adapter.capability.schemas.includes(schema)
      )
        throw new AgentError("EXPLICIT_SCHEMA_REQUIRED", 5);
      const planner = new ImportPlanner(join(p.state, "import"));
      const normalization = {
        identityKey: Buffer.from(secret.identity_key, "hex"),
        policyRevision: "1",
        sourceSchema: schema,
      };
      const source = required(options, "path");
      if (!options.commit) {
        const plan = planner.plan(adapter, source, normalization);
        output({
          state: "dry-run",
          code: 0,
          plan_id: plan.id,
          counts: { selected: plan.workIds.length },
          reasons: ["LOCAL_DEDUP_ONLY_REMOTE_COVERAGE_UNKNOWN"],
        });
        return 0;
      }
      if (agent === "cursor")
        throw new AgentError("HELD_UNPROVEN_SOURCE_ALIAS", 3);
      await startRuntime(p, settings.entry);
      const counts = await planner.commit(
        required(options, "commit"),
        adapter,
        source,
        normalization,
        async (fact) => {
          const ack = await observe(p.socket, routeId, fact);
          if (!ack.durable) throw new AgentError(ack.reason_code);
        },
      );
      const code = counts.pending ? 4 : 0;
      output({ state: code ? "partial" : "durable-local", code, counts });
      return code;
    }
    if (command !== "enable" && !installed)
      throw new AgentError("NOT_INSTALLED", 5);
    if (command === "update" && options.check) {
      output({
        state: "compatibility-checked",
        code: 0,
        enabled: installed.route.enabled,
      });
      return 0;
    }
    if (command === "enable" && installed?.route.enabled) {
      output({
        state: "already-enabled",
        code: 0,
        activation: installed.activation,
      });
      return 0;
    }
    if (
      command === "enable" &&
      adapter.capability.transcript &&
      !context.transcriptConsent
    )
      throw new AgentError("TRANSCRIPT_CONSENT_REQUIRED", 3);
    const change = await adapter.preflight(
      command as "enable" | "disable" | "update" | "uninstall",
      context,
    );
    if (command === "enable") await startRuntime(p, settings.entry);
    if (command === "disable" || command === "uninstall")
      await runtimeRequest(p.socket, {
        operation: "disable",
        route: routeId,
        purge: options["purge-capture"] === true,
      });
    try {
      await configTransaction(p, async (c) => {
        const activation = await change.commit();
        if (command === "uninstall") {
          delete c.routes[routeId];
          return;
        }
        c.routes[routeId] = {
          route: {
            id: routeId,
            binding: profile.binding,
            policy: STRUCTURE_POLICY,
            enabled:
              command === "enable" ||
              (command === "update" && installed.route.enabled),
            epoch: (installed?.route.epoch ?? 0) + 1,
          },
          agent,
          surface,
          profile: profileName,
          transcriptConsent: context.transcriptConsent,
          captureMode: context.captureMode,
          activation,
        };
      });
    } catch (e) {
      await change.rollback();
      throw e;
    }
    await runtimeRequest(p.socket, { operation: "refresh" });
    output({
      state:
        command === "enable"
          ? "waiting-for-activation"
          : command === "disable"
            ? "held"
            : command + "-complete",
      code: 0,
    });
    return 0;
  } catch (e) {
    const error =
      e instanceof AgentError ? e : new AgentError("LOCAL_UNAVAILABLE");
    output({
      state: "held",
      code: error.exitCode,
      counts: {},
      reasons: [error.code],
    });
    return error.exitCode;
  }
}
