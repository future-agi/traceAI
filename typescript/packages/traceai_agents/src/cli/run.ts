import { spawn } from "node:child_process";
import type { Adapter, AdapterContext } from "../adapters.js";
import { AgentError } from "../security/files.js";
/** Only trace settings may be overlaid. No provider/model/permission/sandbox variables. */
const traceEnvironment = new Set([
  "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
  "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
  "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
  "OTEL_TRACES_EXPORTER",
  "CLAUDE_CODE_ENABLE_TELEMETRY",
  "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA",
  "OTEL_LOGS_EXPORTER",
  "OTEL_METRICS_EXPORTER",
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
): Promise<{ code: number | null; signal: NodeJS.Signals | null }> {
  if (!adapter.launch) throw new AgentError("UNSUPPORTED_RUN", 5);
  adapter.validateArgs?.(args);
  const launch = await adapter.launch(context, { tracing });
  try {
    if (!launch.executable.startsWith("/"))
      throw new AgentError("ABSOLUTE_EXECUTABLE_REQUIRED", 5);
    // Fallback must still validate overlays and run destination/security preflight.
    const env = agentEnvironment(process.env, launch.env);
    const child = spawn(launch.executable, [...launch.args, ...args], {
      env: tracing ? env : agentEnvironment(process.env, {}),
      stdio: "inherit",
      shell: false,
    });
    const signals: NodeJS.Signals[] = ["SIGINT", "SIGTERM", "SIGHUP"];
    const handlers = signals.map((signal) => {
      const handler = () => {
        child.kill(signal);
      };
      process.on(signal, handler);
      return handler;
    });
    return await new Promise<{
      code: number | null;
      signal: NodeJS.Signals | null;
    }>((resolve, reject) => {
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
    // Telemetry cleanup never changes the agent's exit code or signal.
    await launch.close?.().catch(() => undefined);
  }
}
