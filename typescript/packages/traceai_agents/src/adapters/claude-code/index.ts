import {
  accessSync,
  constants,
  existsSync,
  lstatSync,
  realpathSync,
  openSync,
  closeSync,
  fstatSync,
  readSync,
} from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { homedir } from "node:os";
import type { Adapter, AdapterContext, Activation } from "../../adapters.js";
import { AgentError } from "../../security/files.js";
import { runtimeRequest } from "../../runtime/daemon.js";
import { claudeCapability, normalizeClaudeSpan } from "./native.js";

const telemetryKey =
  /^(?:OTEL_|CLAUDE_CODE_(?:ENABLE_TELEMETRY|ENHANCED_TELEMETRY_BETA)|ENABLE_(?:ENHANCED_TELEMETRY_BETA|BETA_TRACING_DETAILED)|BETA_TRACING_ENDPOINT)/;
const conflictKeys = [
  "OTEL_EXPORTER_OTLP_ENDPOINT",
  "OTEL_EXPORTER_OTLP_HEADERS",
  "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
  "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
  "OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE",
  "OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY",
  "OTEL_EXPORTER_OTLP_TRACES_CLIENT_CERTIFICATE",
  "OTEL_EXPORTER_OTLP_CERTIFICATE",
  "OTEL_EXPORTER_OTLP_CLIENT_KEY",
  "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE",
  "BETA_TRACING_ENDPOINT",
  "ENABLE_BETA_TRACING_DETAILED",
];

/** Read only the documented settings surfaces; never auth files, transcripts, or shell startup files. */
export function preflightClaude(
  env: NodeJS.ProcessEnv = process.env,
  cwd = process.cwd(),
): void {
  if (
    conflictKeys.some((k) => env[k] !== undefined) ||
    (env.OTEL_TRACES_EXPORTER && env.OTEL_TRACES_EXPORTER !== "none")
  )
    throw new AgentError("NATIVE_DESTINATION_CONFLICT", 6);
  // User opt-outs must not be silently overridden.
  for (const key of [
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA",
    "ENABLE_ENHANCED_TELEMETRY_BETA",
  ]) {
    if (env[key] !== undefined && !["1", "true"].includes(env[key]!))
      throw new AgentError("NATIVE_POLICY_CONFLICT", 6);
  }
  const home = env.HOME ?? homedir();
  const config = env.CLAUDE_CONFIG_DIR ?? join(home, ".claude");
  const files = [
    join(config, "settings.json"),
    join(config, "settings.local.json"),
  ];
  // Managed policy may lock/remove process-scoped endpoints; refuse without mutating or reading it.
  for (const managed of [
    join(config, "managed-settings.json"),
    join(config, "managed-settings.d"),
    "/Library/Application Support/ClaudeCode/managed-settings.json",
    "/Library/Application Support/ClaudeCode/managed-settings.d",
  ]) {
    if (existsSync(managed))
      throw new AgentError("MANAGED_TELEMETRY_UNQUALIFIED", 6);
  }
  for (let dir = resolve(cwd); ; dir = dirname(dir)) {
    files.push(
      join(dir, ".claude", "settings.json"),
      join(dir, ".claude", "settings.local.json"),
    );
    if (dir === dirname(dir)) break;
  }
  for (const file of new Set(files)) {
    let stat;
    try {
      stat = lstatSync(file);
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code === "ENOENT") continue;
      throw new AgentError("EFFECTIVE_CONFIG_UNAVAILABLE", 6);
    }
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 1_048_576)
      throw new AgentError("EFFECTIVE_CONFIG_UNAVAILABLE", 6);
    // Detect symlinked parents too. No secret-bearing backup or echoed diff is ever made.
    if (realpathSync(file) !== resolve(file))
      throw new AgentError("EFFECTIVE_CONFIG_UNAVAILABLE", 6);
    let settings;
    let fd: number | undefined;
    try {
      fd = openSync(file, constants.O_RDONLY | constants.O_NOFOLLOW);
      const opened = fstatSync(fd);
      if (
        !opened.isFile() ||
        opened.dev !== stat.dev ||
        opened.ino !== stat.ino
      )
        throw new Error();
      const buffer = Buffer.alloc(1_048_577);
      let size = 0;
      while (size < buffer.length) {
        const read = readSync(fd, buffer, size, buffer.length - size, null);
        if (!read) break;
        size += read;
      }
      const after = fstatSync(fd),
        current = lstatSync(file);
      if (
        size > 1_048_576 ||
        after.mtimeMs !== opened.mtimeMs ||
        after.size !== opened.size ||
        current.ino !== opened.ino ||
        current.dev !== opened.dev ||
        realpathSync(file) !== resolve(file)
      )
        throw new Error();
      settings = JSON.parse(buffer.subarray(0, size).toString());
    } catch {
      throw new AgentError("EFFECTIVE_CONFIG_UNAVAILABLE", 6);
    } finally {
      if (fd !== undefined) closeSync(fd);
    }
    if (!settings || typeof settings !== "object" || Array.isArray(settings))
      throw new AgentError("EFFECTIVE_CONFIG_UNAVAILABLE", 6);
    if (
      settings.otelHeadersHelper !== undefined ||
      Object.keys(settings.env ?? {}).some((key) => telemetryKey.test(key))
    )
      throw new AgentError("NATIVE_DESTINATION_CONFLICT", 6);
  }
}
function executable(): string {
  for (const dir of (process.env.PATH ?? "").split(":")) {
    if (!isAbsolute(dir)) continue;
    const candidate = join(dir, "claude");
    try {
      accessSync(candidate, constants.X_OK);
      if (lstatSync(realpathSync(candidate)).isFile()) return candidate;
    } catch {
      /* next explicit PATH entry */
    }
  }
  throw new AgentError("CLAUDE_NOT_FOUND", 5);
}
const pending: Activation = {
  installed: true,
  trusted: "unknown",
  activated: false,
  restartRequired: false,
};
export const claudeCodeAdapter: Adapter = {
  capability: claudeCapability,
  nativeNormalize: normalizeClaudeSpan,
  async preflight(action, context) {
    if (context.transcriptConsent)
      throw new AgentError("HELD_NATIVE_TRANSCRIPT_JOIN", 3);
    if (action === "enable" || action === "update") {
      preflightClaude();
      executable();
    }
    // fi-agent's own route is the registration. No persistent Claude settings/hook/plugin mutations.
    return {
      commit: async () => ({ ...pending, installed: action !== "uninstall" }),
      rollback: async () => undefined,
    };
  },
  async probe(context) {
    preflightClaude();
    executable();
    const status = await runtimeRequest(context.socketPath, {
      operation: "status",
    });
    const route = status.agents?.find(
      (r: any) => r.agent === "claude-code" && r.surface === "cli",
    );
    return {
      ...pending,
      installed: Boolean(route),
      activated: route?.native_durable_ack === true,
    };
  },
  normalize() {
    throw new AgentError("NATIVE_OTLP_REQUIRED", 5);
  },
  async launch(context: AdapterContext, { tracing }) {
    preflightClaude();
    const binary = executable();
    // Runtime loss must not prevent the agent running, and must not open a
    // receiver or inject telemetry settings on the untraced fallback path.
    if (!tracing) return { executable: binary, args: [], env: {} };
    const receiver = await runtimeRequest(context.socketPath, {
      operation: "native-open",
      route: context.routeRef,
    });
    const heartbeat = setInterval(() => {
      void runtimeRequest(context.socketPath, {
        operation: "native-renew",
        route: context.routeRef,
        lease: receiver.lease,
      }).catch(() => undefined);
    }, 20_000);
    heartbeat.unref();
    return {
      executable: binary,
      args: [],
      env: {
        CLAUDE_CODE_ENABLE_TELEMETRY: "1",
        CLAUDE_CODE_ENHANCED_TELEMETRY_BETA: "1",
        OTEL_TRACES_EXPORTER: "otlp",
        OTEL_EXPORTER_OTLP_TRACES_PROTOCOL: "http/protobuf",
        OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: receiver.endpoint,
        OTEL_EXPORTER_OTLP_TRACES_HEADERS:
          "Authorization=Bearer " + receiver.token,
        // These settings affect only newly enabled signals, never existing exporters.
        ...(!process.env.OTEL_LOGS_EXPORTER
          ? { OTEL_LOGS_EXPORTER: "none" }
          : {}),
        ...(!process.env.OTEL_METRICS_EXPORTER
          ? { OTEL_METRICS_EXPORTER: "none" }
          : {}),
      },
      close: async () => {
        clearInterval(heartbeat);
        await runtimeRequest(context.socketPath, {
          operation: "native-close",
          route: context.routeRef,
          lease: receiver.lease,
        });
      },
    };
  },
  validateArgs(args) {
    if (
      args.some((arg) =>
        /^(--settings|--setting-sources|--worktree|--remote|--teleport)(=|$)/.test(
          arg,
        ),
      )
    )
      throw new AgentError("EFFECTIVE_CONFIG_OVERRIDE_UNQUALIFIED", 6);
    if (!args.includes("--print") && !args.includes("-p"))
      throw new AgentError("CLAUDE_PRINT_SURFACE_REQUIRED", 5);
  },
};
