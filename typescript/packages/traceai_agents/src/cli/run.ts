import { execFileSync, spawn } from "node:child_process";
import type { Adapter, AdapterContext } from "../adapters.js";
import { AgentError } from "../security/files.js";
import { runtimeRequest } from "../runtime/daemon.js";
import type { NativeLease } from "../runtime/native.js";
/** Only trace settings may be overlaid. No provider/model/permission/sandbox variables. */
const traceEnvironment = new Set([
  "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
  "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
  "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
  "OTEL_TRACES_EXPORTER",
]);
export function agentEnvironment(
  env: NodeJS.ProcessEnv,
  overlay: Record<string, string>,
): NodeJS.ProcessEnv {
  const result = { ...env };
  for (const key of Object.keys(result))
    if (/^FI_(API_KEY|SECRET_KEY|AGENT_.*)$/.test(key)) delete result[key];
  for (const [key, value] of Object.entries(overlay)) {
    if (!traceEnvironment.has(key))
      throw new AgentError("UNSAFE_LAUNCH_OVERLAY", 6);
    result[key] = value;
  }
  return result;
}
export async function runAgent(
  adapter: Adapter,
  context: AdapterContext,
  args: readonly string[],
  tracing = true,
  options: { signalDelivery?: "terminal" | "dedicated-wrapper-group" } = {},
): Promise<{ code: number | null; signal: NodeJS.Signals | null }> {
  if (!adapter.launch) throw new AgentError("UNSUPPORTED_RUN", 5);
  if (options.signalDelivery === "dedicated-wrapper-group") {
    // Never signal a group owned by a shell, IDE or supervisor. ps is used
    // only for this explicit mode; unavailable/ambiguous group ID fails closed.
    let group: number;
    try {
      group = Number(
        execFileSync("/bin/ps", ["-o", "pgid=", "-p", String(process.pid)], {
          encoding: "utf8",
          timeout: 1000,
        }).trim(),
      );
    } catch {
      throw new AgentError("UNSUPPORTED_SIGNAL_DELIVERY", 5);
    }
    if (process.platform === "win32" || group !== process.pid)
      throw new AgentError("UNSUPPORTED_SIGNAL_DELIVERY", 5);
  }
  let launch = await adapter.launch(context, { tracing });
  // Validate before opening ingress, including the no-tracing fallback.
  const validate = () => {
    if (!launch.executable.startsWith("/"))
      throw new AgentError("ABSOLUTE_EXECUTABLE_REQUIRED", 5);
    agentEnvironment(process.env, launch.env);
  };
  validate();
  let lease: NativeLease | undefined;
  let heartbeat: NodeJS.Timeout | undefined;
  let renewing = false;
  let closed = false;
  const unavailable = () =>
    process.stderr.write(
      "fi-agent: tracing unavailable; coding continues without managed capture\n",
    );
  try {
    if (tracing && launch.native) {
      try {
        lease = await runtimeRequest(
          context.socketPath,
          { operation: "native-open", route: context.routeRef },
          500,
        );
      } catch (e) {
        // Local telemetry failure must not turn into a coding failure. Policy,
        // destination and unsupported-profile refusals remain hard preflight errors.
        if (!(e instanceof AgentError) || ![3, 7].includes(e.exitCode)) throw e;
        unavailable();
        tracing = false;
        launch = await adapter.launch(context, { tracing: false });
        validate();
      }
    }
    if (lease) {
      const current = lease;
      heartbeat = setInterval(() => {
        if (renewing || closed) return;
        renewing = true;
        void runtimeRequest(
          context.socketPath,
          { operation: "native-renew", id: current.id },
          500,
        )
          .catch((e) => {
            // A slow/unavailable runtime is retried within the lease lifetime;
            // the runtime expires the lease if it really is gone. Never reopen
            // capture after a policy fence or silently switch bindings.
            if (e instanceof AgentError && e.exitCode === 3) {
              if (heartbeat) clearInterval(heartbeat);
              if (!closed) unavailable();
            }
          })
          .finally(() => {
            renewing = false;
          });
      }, current.renewAfterMs);
      heartbeat.unref();
    }
    const overlay = tracing
      ? {
          ...launch.env,
          ...(lease
            ? {
                OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: lease.endpoint,
                OTEL_EXPORTER_OTLP_TRACES_PROTOCOL: "http/json",
                OTEL_EXPORTER_OTLP_TRACES_HEADERS:
                  "Authorization=" +
                  encodeURIComponent("Bearer " + lease.token),
              }
            : {}),
        }
      : {};
    // A terminal sends SIGINT to its foreground group. Interactive agents
    // remain in that group for terminal access, so never duplicate that SIGINT.
    // Noninteractive agents get their own group, including their shell/tools;
    // forward to the group, not just the direct child PID. A supervisor that
    // signals only the wrapper of an interactive agent must explicitly opt in
    // to dedicated-wrapper-group AND start the wrapper as its own group leader:
    // signaling a shared foreground group would interrupt unrelated processes.
    // Windows console signal delivery is not qualified for group forwarding.
    const interactive = process.platform !== "win32" && Boolean(process.stdin.isTTY);
    const child = spawn(launch.executable, [...launch.args, ...args], {
      env: agentEnvironment(process.env, overlay),
      stdio: "inherit",
      shell: false,
      detached: process.platform !== "win32" && !interactive,
    });
    const signals: NodeJS.Signals[] = ["SIGINT", "SIGTERM", "SIGHUP"];
    let forwardingGroup = false;
    const handlers = signals.map((signal) => {
      const handler = () => {
        // Ignore our own notification when forwarding to a dedicated wrapper
        // group. Only callers that own that group may opt in to this mode.
        if (forwardingGroup && signal === "SIGINT") {
          forwardingGroup = false;
          return;
        }
        if (interactive && signal === "SIGINT") {
          if (options.signalDelivery !== "dedicated-wrapper-group") return;
          forwardingGroup = true;
          try {
            process.kill(-process.pid, signal);
          } catch (error) {
            forwardingGroup = false;
            if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error;
          }
          return;
        }
        try {
          if (process.platform !== "win32" && !interactive)
            process.kill(-child.pid!, signal);
          else child.kill(signal);
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error;
        }
      };
      process.on(signal, handler);
      return handler;
    });
    return await new Promise((resolve, reject) => {
      const cleanup = () =>
        signals.forEach((s, i) => process.removeListener(s, handlers[i]));
      child.once("error", () => {
        cleanup();
        reject(new AgentError("AGENT_LAUNCH_FAILED", 7));
      });
      child.once("exit", (code, signal) => {
        cleanup();
        resolve({ code, signal });
      });
    });
  } finally {
    closed = true;
    if (heartbeat) clearInterval(heartbeat);
    if (lease) {
      // Only local revocation, not an upstream flush. A dead daemon cannot alter
      // the child's status; an unanswered close is bounded and lease expiry wins.
      await runtimeRequest(
        context.socketPath,
        { operation: "native-close", id: lease.id },
        100,
      ).catch(() => undefined);
    }
  }
}
