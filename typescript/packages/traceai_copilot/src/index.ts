import { spawn, execFile } from "node:child_process";
import { promisify } from "node:util";
import { constants, openSync, closeSync, readSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { join } from "node:path";
import {
  BoundedSpanProcessor,
  collectionEnabled,
  controlPaths,
  deliveryStatePath,
  ensurePrivateDirectory,
  readJson,
  updateJson,
  type AdapterContext,
  type AdapterInspection,
  type CoreAdapter,
  type DeliveryState,
} from "@traceai/agent-core";
import { NativeLines, NativeSpans } from "./native.js";
export { nativeSpan, NativeLines, NativeSpans } from "./native.js";
export { createVscodeAdapter, vscodeAdapter } from "./vscode/index.js";

export const VERSION = "0.1.0"; // x-release-please-version
export const HOST_VERSION = "1.0.106";
/** Qualified major line; every record is independently shape-checked before export. */
export const supportedHostVersion = (version: string | undefined): boolean =>
  typeof version === "string" && /^1\.\d+\.\d+$/.test(version);
export const hostVersionFromOutput = (output: string): string | undefined =>
  /GitHub Copilot CLI (\S+)/.exec(output)?.[1].replace(/\.$/, "");
const exec = promisify(execFile);
const secretNames = [
  "FI_API_KEY",
  "FI_SECRET_KEY",
  "FI_API_SECRET",
  "FUTUREAGI_API_KEY",
  "FUTUREAGI_SECRET_KEY",
  "OTEL_EXPORTER_OTLP_HEADERS",
  "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
  "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
  "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
];
/** Do not forward FI credentials or an inherited second telemetry destination to the host or its tools. */
export function childEnvironment(
  source: NodeJS.ProcessEnv,
  capture = false,
  pipe?: string,
): NodeJS.ProcessEnv {
  const env = { ...source };
  for (const key of Object.keys(env))
    if (
      key.startsWith("OTEL_") ||
      key.startsWith("COPILOT_OTEL_") ||
      secretNames.includes(key)
    )
      delete env[key];
  env.COPILOT_OTEL_ENABLED = pipe ? "true" : "false";
  env.OTEL_SDK_DISABLED = pipe ? "false" : "true";
  env.OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT = capture
    ? "true"
    : "false";
  env.OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT = capture
    ? "SPAN_ONLY"
    : "NO_CONTENT";
  if (pipe) {
    env.COPILOT_OTEL_EXPORTER_TYPE = "file";
    env.COPILOT_OTEL_FILE_EXPORTER_PATH = pipe;
  }
  return env;
}
const registrationPath = (c: AdapterContext): string =>
  join(c.config.paths.configDir, "copilot-launcher.json");
async function detect(): Promise<{
  supported: boolean;
  version?: string;
  reason?: string;
}> {
  if (!["linux", "darwin"].includes(process.platform))
    return { supported: false, reason: "POSIX_REQUIRED" };
  try {
    const result = await exec("copilot", ["--no-auto-update", "--version"], {
      env: childEnvironment(process.env),
      timeout: 3000,
      maxBuffer: 4096,
    });
    const version = hostVersionFromOutput(result.stdout);
    return {
      supported: supportedHostVersion(version),
      version,
      ...(supportedHostVersion(version)
        ? {}
        : { reason: "UNQUALIFIED_COPILOT_VERSION" }),
    };
  } catch {
    return { supported: false, reason: "COPILOT_NOT_FOUND" };
  }
}
async function inspection(c: AdapterContext): Promise<AdapterInspection> {
  const saved = await readJson<{ version: string; mechanism: string }>(
    registrationPath(c),
  );
  const host = await detect();
  return {
    registered:
      saved?.version === VERSION &&
      saved?.mechanism === "native-pipe" &&
      host.supported,
    hostVersion: host.version,
    trust: "unknown",
    detail: host.supported ? undefined : host.reason,
  };
}
async function note(
  c: AdapterContext,
  change: Partial<DeliveryState>,
): Promise<void> {
  try {
    await updateJson<DeliveryState>(
      deliveryStatePath(c.config),
      (old) => ({ pending: 0, exported: 0, dropped: 0, ...old, ...change,
        ...(c.config.deliveryBinding ? { binding: c.config.deliveryBinding } : {}),
      }),
    );
  } catch {
    /* Diagnostics cannot block the host. */
  }
}
function launch(
  args: string[],
  env: NodeJS.ProcessEnv,
  cwd?: string,
): Promise<number> {
  return new Promise((done) => {
    const child = spawn(
      "copilot",
      [
        "--no-auto-update",
        `--secret-env-vars=${secretNames.join(",")}`,
        ...args,
      ],
      { stdio: "inherit", env, cwd },
    );
    const interrupt = () => child.kill("SIGINT"),
      terminate = () => child.kill("SIGTERM");
    process.on("SIGINT", interrupt);
    process.on("SIGTERM", terminate);
    const finish = (code: number) => {
      process.off("SIGINT", interrupt);
      process.off("SIGTERM", terminate);
      done(code);
    };
    child.once("error", () => finish(127));
    child.once("close", (code, signal) =>
      finish(
        code ?? (signal === "SIGINT" ? 130 : signal === "SIGTERM" ? 143 : 1),
      ),
    );
  });
}
/** Own the pipe for exactly the lifetime of this foreground launch. Never persist native content. */
export async function runCopilot(
  c: AdapterContext,
  args: string[],
): Promise<number> {
  // -C changes Copilot's settings scope after resolution; require the unified CLI's --project-dir instead.
  if (
    args.some(
      (a) =>
        a.startsWith("-C") ||
        a === "--secret-env-vars" ||
        a.startsWith("--secret-env-vars="),
    )
  ) {
    process.stderr.write(
      "Use fi-agent run copilot --project-dir <directory>; launcher secret isolation cannot be overridden.\n",
    );
    return 2;
  }
  const baseEnv = childEnvironment(process.env),
    config = c.config;
  const host = await detect();
  const active =
    host.supported &&
    collectionEnabled(config) &&
    config.destinationApproved &&
    !!config.credentials &&
    process.env.COPILOT_OFFLINE !== "true";
  if (!active) {
    await note(c, {
      lastError: !host.supported
        ? "UNQUALIFIED_HOST"
        : process.env.COPILOT_OFFLINE === "true"
          ? "COPILOT_OFFLINE"
          : "DISABLED_OR_UNCONFIGURED",
    });
    if (config.enabled)
      process.stderr.write(
        "Future AGI tracing is inactive; Copilot will continue without Future AGI export. Run fi-agent doctor copilot.\n",
      );
    return launch(args, baseEnv, c.projectDir);
  }
  let directory: string | undefined,
    descriptor: number | undefined,
    timer: NodeJS.Timeout | undefined;
  let processor: BoundedSpanProcessor | undefined;
  let hostResult: number | undefined;
  try {
    await ensurePrivateDirectory(config.paths.stateDir);
    directory = await mkdtemp(join(config.paths.stateDir, "copilot-run-"));
    const pipe = join(directory, "native.fifo");
    await exec("mkfifo", ["-m", "600", pipe], { timeout: 2000 });
    descriptor = openSync(
      pipe,
      constants.O_RDWR | constants.O_NONBLOCK | constants.O_NOFOLLOW,
    );
    const protocol =
      process.env.OTEL_EXPORTER_OTLP_TRACES_PROTOCOL ??
      process.env.OTEL_EXPORTER_OTLP_PROTOCOL ??
      "http/json";
    if (!["http/json", "http/protobuf"].includes(protocol))
      throw new Error("UNSUPPORTED_OTLP_PROTOCOL");
    processor = new BoundedSpanProcessor(
      config,
      undefined,
      protocol as "http/json" | "http/protobuf",
    );
    let rejected = 0,
      seen = 0;
    const spans = new NativeSpans(config);
    const lines = new NativeLines(
      (record) => {
        if (!collectionEnabled(config)) return;
        const span = spans.accept(record);
        if (span) {
          seen++;
          processor!.onEnd(span);
        } else if (
          record &&
          typeof record === "object" &&
          "type" in record &&
          record.type === "span"
        ) {
          rejected++;
        }
      },
      () => {
        rejected++;
      },
    );
    const buffer = Buffer.alloc(64 * 1024);
    const drain = () => {
      // Bound work per tick; always drain even after disabling so the agent is never backpressured by export.
      for (let i = 0; i < 32; i++) {
        try {
          const count = readSync(descriptor!, buffer, 0, buffer.length, null);
          if (!count) break;
          lines.push(buffer.subarray(0, count));
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== "EAGAIN") rejected++;
          break;
        }
      }
    };
    timer = setInterval(drain, 10);
    const env = childEnvironment(
      process.env,
      config.captureContent && !(config.hideInputs && config.hideOutputs),
      pipe,
    );
    // An input/output hide policy is applied again to every native field before enqueueing.
    const code = (hostResult = await launch(args, env, c.projectDir));
    clearInterval(timer);
    timer = undefined;
    drain();
    lines.end();
    // Native EOF is the only reliable missing-terminal boundary. Ordinary
    // exporter flushes must not race roots still being written by Copilot.
    if (collectionEnabled(config))
      for (const span of spans.end()) {
        seen++;
        processor.onEnd(span);
      }
    await processor.shutdown();
    processor = undefined;
    if (!seen || rejected)
      await note(c, {
        lastError: rejected ? "NATIVE_RECORD_REJECTED" : "NO_NATIVE_SPANS",
      });
    return code;
  } catch {
    // Preparation failures are telemetry failures, not a reason to prevent work.
    if (timer) clearInterval(timer);
    if (processor) await processor.shutdown().catch(() => {});
    await note(c, { lastError: "NATIVE_CAPTURE_UNAVAILABLE" });
    return hostResult ?? launch(args, baseEnv, c.projectDir);
  } finally {
    if (timer) clearInterval(timer);
    if (descriptor !== undefined) closeSync(descriptor);
    if (directory)
      await rm(directory, { recursive: true, force: true }).catch(() => {});
  }
}
async function removeRegistration(path: string): Promise<AdapterInspection> {
  const old = await readJson<{ version: string; mechanism: string }>(path);
  if (old?.version === VERSION && old?.mechanism === "native-pipe")
    await rm(path);
  return {
    registered: false,
    detail:
      "Future AGI launch registration removed. Copilot settings were not changed.",
  };
}
export const copilotAdapter: CoreAdapter = {
  id: "copilot",
  version: VERSION,
  requiresWorker: false,
  diagnostics: {
    COPILOT_NOT_FOUND: "Install Copilot CLI and ensure it is on PATH.",
    UNQUALIFIED_COPILOT_VERSION: "Use a supported Copilot CLI major version.",
    POSIX_REQUIRED: "The native pipe launcher requires Linux or macOS.",
  },
  detect,
  async install(c) {
    await updateJson(registrationPath(c), () => ({
      version: VERSION,
      mechanism: "native-pipe",
    }));
    return inspection(c);
  },
  inspect: inspection,
  uninstall(c) {
    return removeRegistration(registrationPath(c));
  },
  uninstallUnconfigured(options) {
    return removeRegistration(
      join(controlPaths(options).configDir, "copilot-launcher.json"),
    );
  },
  run: runCopilot,
  async runUnconfigured(args, projectDir) {
    process.stderr.write(
      "Future AGI configuration is unavailable; Copilot will continue without Future AGI export.\n",
    );
    return launch(args, childEnvironment(process.env), projectDir);
  },
};
