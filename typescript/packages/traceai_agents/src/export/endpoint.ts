import { AgentError } from "../security/files.js";
export const DEFAULT_ENDPOINT = "https://api.futureagi.com/tracer/v1/traces";
export interface Binding {
  id: string;
  endpoint: string;
  approvedOrigin: string;
  project: string;
  loopbackTest?: boolean;
  backendContract?: "immutable-agent-v1";
}
export function validateBinding(binding: Binding): URL {
  let url: URL;
  try {
    url = new URL(binding.endpoint);
  } catch {
    throw new AgentError("INVALID_DESTINATION", 2);
  }
  if (
    !/^[a-f0-9-]{36}$/.test(binding.id) ||
    !/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$/.test(binding.project) ||
    url.origin !== binding.approvedOrigin ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    url.pathname === "/" ||
    url.href !== binding.endpoint
  )
    throw new AgentError("INVALID_DESTINATION", 2);
  const loopback = ["127.0.0.1", "[::1]"].includes(url.hostname);
  if (
    url.protocol !== "https:" &&
    !(binding.loopbackTest === true && loopback && url.protocol === "http:")
  )
    throw new AgentError("TLS_REQUIRED", 3);
  return url;
}
