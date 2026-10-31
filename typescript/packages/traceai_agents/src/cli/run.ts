import { spawn } from "node:child_process";
import type { Adapter, AdapterContext } from "../adapters.js";
import { AgentError } from "../security/files.js";
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
): Promise<{ code: number | null; signal: NodeJS.Signals | null }> {
  if (!adapter.launch) throw new AgentError("UNSUPPORTED_RUN", 5);
  const launch = await adapter.launch(context);
  if (!launch.executable.startsWith("/"))
    throw new AgentError("ABSOLUTE_EXECUTABLE_REQUIRED", 5);
  const child = spawn(launch.executable, [...launch.args, ...args], {
    env: agentEnvironment(process.env, launch.env),
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
  return new Promise((resolve, reject) => {
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
}
