import { readFileSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { homedir } from "node:os";
import type { SpanExporter, SpanProcessor } from "@opentelemetry/sdk-trace-base";
import type { TracerProvider } from "@opentelemetry/api";
import type { PrivacyOptions as TraceConfigOptions } from "./export-privacy";
import { compileOutgoingPolicy, type OutgoingPolicy, type OutgoingPropagationMode } from "./outgoing-policy";

export interface Options {
  /** Experimental node:http/node:https propagation; off until explicitly selected. */
  propagateOutgoingAdapters?: boolean;
  /** default/all retain mandatory vendor-host, provider-port and redirect vetoes. */
  propagateOutgoing?: OutgoingPropagationMode;
  /** Positive rules only narrow admission; no rule can waive the safety vetoes. */
  propagateHosts?: string[];
  apiKey?: string; secretKey?: string; baseUrl?: string; projectName?: string;
  projectType?: "observe" | "experiment"; instrumentations?: string[];
  disabledInstrumentations?: string[]; wireCapture?: boolean; wireMaxBodyBytes?: number;
  sampleRate?: number; debug?: boolean; configFile?: string; profile?: string; traceConfig?: TraceConfigOptions;
  /** Override the collector exporter, useful for embedding and offline tests. */
  exporter?: SpanExporter;
  /** Use an application-owned destination without requiring FI credentials. */
  provider?: TracerProvider;
  spanProcessor?: SpanProcessor;
  mastraSpanProducer?: "exporter" | "bridge";
  /** Also send the application's own spans to Future AGI (off by default; FI_FORWARD_APP_SPANS). */
  forwardApplicationSpans?: boolean;
}
export interface Config {
  forwardApplicationSpans: boolean;
  propagateOutgoingAdapters: boolean;
  propagateOutgoing: OutgoingPropagationMode;
  apiKey?: string; secretKey?: string; baseUrl: string; projectName: string;
  projectType: "observe" | "experiment"; instrumentations: string[];
  disabledInstrumentations: string[]; wireCapture: boolean; wireMaxBodyBytes: number;
  sampleRate: number; debug: boolean; configFile: string; sdkDisabled: boolean;
  traceConfig: TraceConfigOptions;
  mastraSpanProducer: "exporter" | "bridge";
}
export function parseConfigFile(text: string, profile?: string): Record<string, unknown> {
  const values: Record<string, unknown> = {};
  let active = profile === undefined;
  for (const line of text.split(/\r?\n/)) {
    // Do not treat # inside a JSON/TOML quoted string as a comment.
    const clean = line.replace(/"(?:\\.|[^"\\])*"|'[^']*'|#[^\n]*/g, token => token.startsWith("#") ? "" : token).trim();
    if (clean.startsWith("[")) {
      const section = /^\[profiles\.(?:([A-Za-z0-9_-]+)|"([^"\\]+)"|'([^']+)')\]$/.exec(clean);
      active = profile !== undefined && !!section && (section[1] ?? section[2] ?? section[3]) === profile;
      continue;
    }
    if (!clean || !active) continue;
    const outgoingKey = /^(propagate_outgoing_adapters|propagate_outgoing|propagate_hosts)\s*=/.exec(clean)?.[1];
    const found = /^([a-zA-Z_][\w]*)\s*=\s*(.+)$/.exec(clean);
    if (!found) { if (outgoingKey) values[outgoingKey] = false; continue; }
    try {
      const value = /^'[^']*'$/.test(found[2]) ? found[2].slice(1, -1) : JSON.parse(found[2]);
      if (typeof value === "string" || typeof value === "boolean" || typeof value === "number" || (Array.isArray(value) && value.every(v => typeof v === "string"))) values[found[1]] = value;
      else if (outgoingKey) values[outgoingKey] = false;
    } catch {
      // Do not silently drop a malformed deny list and fall back to a wider policy.
      // Other unsupported TOML retains the existing ignored/not-executed behavior.
      if (outgoingKey) values[outgoingKey] = false;
    }
  }
  return values;
}
function bool(v: unknown, fallback: boolean): boolean {
  return v === undefined ? fallback : typeof v === "boolean" ? v : String(v).toLowerCase() === "true";
}
const SWITCH_ON = ["true", "1", "yes", "on"], SWITCH_OFF = ["false", "0", "no", "off"];
const warnedSwitches = new Set<string>();
/**
 * A privacy switch someone set must never silently do nothing: true, 1, yes and on turn it on,
 * false, 0, no and off turn it off (any case, surrounding spaces ignored), and an empty value is
 * unset. Any other value turns it on, with one warning per switch that never repeats the value.
 */
function hideSwitch(v: unknown, where: string): boolean | undefined {
  if (typeof v === "boolean") return v;
  const normalized = String(v).trim().toLowerCase();
  if (normalized === "") return undefined;
  if (SWITCH_ON.includes(normalized)) return true;
  if (SWITCH_OFF.includes(normalized)) return false;
  if (!warnedSwitches.has(where)) {
    warnedSwitches.add(where);
    try { console.warn(`[traceai:auto] ${where} is not one of true, 1, yes, on, false, 0, no or off; treating it as on, so the content it controls is hidden.`); } catch { /* Diagnostics cannot change configuration. */ }
  }
  return true;
}
function names(v: unknown): string[] { return (Array.isArray(v) ? v : String(v ?? "").split(",")).map(x => String(x).trim().toLowerCase()).filter(Boolean); }
function readProfile(path: string, profile: string): Record<string, unknown> {
  try { return parseConfigFile(readFileSync(path, "utf8"), profile); } catch { return {}; }
}
function fiProfiles(profile: string, env: NodeJS.ProcessEnv): Record<string, unknown>[] {
  // Project and user files are separate sources. Never merge their credential halves.
  let project: Record<string, unknown> = {};
  for (let dir = process.cwd();;) {
    try { project = parseConfigFile(readFileSync(join(dir, ".fi/config.toml"), "utf8"), profile); break; } catch { /* Search upward. */ }
    const parent = dirname(dir); if (parent === dir) break; dir = parent;
  }
  const user = readProfile(join(env.XDG_CONFIG_HOME || join(env.HOME || homedir(), ".config"), "fi/config.toml"), profile);
  return [project, user];
}
// Ignored FI halves are still secrets. Keep them out of serializable config/report data.
const configCredentials = new WeakMap<Config, readonly unknown[]>();
// Host rules describe private topology: never spread them into config/report/debug.
const outgoingPolicies = new WeakMap<Config, OutgoingPolicy>();
let warnedOutgoingPolicy = false;
export function outgoingPolicyForConfig(config: Config): OutgoingPolicy {
  return outgoingPolicies.get(config) ?? compileOutgoingPolicy("none", undefined);
}
export function credentialsForConfig(config: Config): readonly unknown[] { return configCredentials.get(config) ?? []; }
// Sources that held only half of a pair when no source held a complete one.
// init() folds them into its single "disabled" warning. Names only, never values.
const incompleteSources = new WeakMap<Config, readonly string[]>();
export function incompleteCredentialSources(config: Config): readonly string[] { return incompleteSources.get(config) ?? []; }
function credentialPair(sources: { source: string; apiKey: unknown; secretKey: unknown }[]): { pair: { apiKey?: string; secretKey?: string }; incomplete: string[] } {
  const incomplete: string[] = [];
  for (const { source, apiKey, secretKey } of sources) {
    const key = typeof apiKey === "string" && apiKey.trim() ? apiKey : undefined;
    const secret = typeof secretKey === "string" && secretKey.trim() ? secretKey : undefined;
    if (key && secret) {
      // A complete pair was found: say which earlier halves were ignored.
      for (const note of incomplete) {
        try { console.warn(`[traceai:auto] Ignoring incomplete credentials from ${note}. Supply both in the same source.`); } catch { /* Never change application behaviour. */ }
      }
      return { pair: { apiKey: key, secretKey: secret }, incomplete: [] };
    }
    if (key || secret) incomplete.push(`${source}: missing ${key ? "FI_SECRET_KEY" : "FI_API_KEY"}`);
  }
  return { pair: {}, incomplete };
}
function flag(value: unknown): boolean { return value === true || typeof value === "string" && value.trim().toLowerCase() === "true"; }
function mastraProducer(value: unknown): "exporter" | "bridge" {
  if (value === undefined || value === "exporter") return "exporter";
  if (value === "bridge") return "bridge";
  try { console.warn("[traceai:auto] Invalid FI_MASTRA_SPAN_PRODUCER; using exporter."); } catch { /* Diagnostics cannot change configuration. */ }
  return "exporter";
}
export function resolveConfig(options: Options = {}, env: NodeJS.ProcessEnv = process.env): Config {
  const configFile = resolve(options.configFile ?? env.FI_CONFIG_FILE ?? "futureagi.toml");
  let file: Record<string, unknown> = {};
  try { file = parseConfigFile(readFileSync(configFile, "utf8")); } catch { /* Optional file. */ }
  const profiles = fiProfiles(options.profile ?? env.FI_PROFILE ?? "default", env);
  const credentialSources = [
    { source: "explicit arguments", apiKey: options.apiKey, secretKey: options.secretKey },
    { source: "environment", apiKey: env.FI_API_KEY, secretKey: env.FI_SECRET_KEY },
    { source: "config file", apiKey: file.api_key, secretKey: file.secret_key },
    ...profiles.map((p, i) => ({ source: i ? "fi user profile" : "fi project profile", apiKey: p.api_key, secretKey: p.secret_key })),
  ];
  const { pair, incomplete } = credentialPair(credentialSources);
  const value =(code: keyof Options, key: string, envName = `FI_${key.toUpperCase()}`): any => options[code] ?? env[envName] ?? file[key];
  const sample = Number(value("sampleRate", "sample_rate") ?? 1), max = Number(value("wireMaxBodyBytes", "wire_max_body_bytes") ?? 4194304);
  const traceConfig: TraceConfigOptions = {};
  for (const key of ["hideInputs", "hideOutputs", "hideInputMessages", "hideOutputMessages", "hideInputImages", "hideInputText", "hideOutputText", "hideEmbeddingVectors", "hideLLMInvocationParameters", "base64ImageMaxLength", "piiRedaction", "hideErrorDetails"] as (keyof TraceConfigOptions)[]) {
    const snake = key.replace(/([a-z])([A-Z])/g, "$1_$2").replace(/([A-Z])([A-Z][a-z])/g, "$1_$2").toLowerCase();
    const envName = `FI_${snake.toUpperCase()}`;
    if (key === "base64ImageMaxLength") {
      const raw = options.traceConfig?.[key] ?? env[envName] ?? file[snake];
      if (raw !== undefined) { const n = Number(raw); if (Number.isFinite(n)) traceConfig[key] = n; }
    } else {
      // A privacy switch. The first source that is set wins: the option in code, the variable,
      // then the config file. A null, empty or blank value is not set, so an empty variable never
      // masks the file's value. With none set the key is left out (hideErrorDetails then follows
      // the content switches). An unrecognised value hides, with one warning naming its source.
      const sources: [unknown, string][] = [[options.traceConfig?.[key], `traceConfig.${key}`], [env[envName], envName], [file[snake], `${snake} in the config file`]];
      const source = sources.find(([raw]) => raw != null && String(raw).trim() !== "");
      if (source) {
        const value = hideSwitch(source[0], source[1]);
        if (value !== undefined) traceConfig[key] = value;
      }
    }
  }
  const rawHosts = value("propagateHosts", "propagate_hosts", "TRACEAI_PROPAGATE_HOSTS");
  const envHosts = options.propagateHosts == null && env.TRACEAI_PROPAGATE_HOSTS != null;
  // Only the environment uses CSV; code/file options require a string array.
  const hostInput = rawHosts === undefined || (envHosts ? typeof rawHosts === "string" : Array.isArray(rawHosts)) ? rawHosts : false;
  const outgoing = compileOutgoingPolicy(value("propagateOutgoing", "propagate_outgoing", "TRACEAI_PROPAGATE_OUTGOING"), hostInput);
  if (!outgoing.valid && !warnedOutgoingPolicy) {
    warnedOutgoingPolicy = true;
    try { console.warn("[traceai:auto] Invalid outgoing propagation policy; agent additions are disabled."); } catch { /* No raw values in diagnostics. */ }
  }
  // Strict opt-in, separate from destination policy: "all" is not activation.
  const rawAdapters = value("propagateOutgoingAdapters", "propagate_outgoing_adapters", "TRACEAI_PROPAGATE_OUTGOING_ADAPTERS");
  const config: Config = {
    forwardApplicationSpans: flag(value("forwardApplicationSpans", "forward_app_spans", "FI_FORWARD_APP_SPANS")),
    propagateOutgoingAdapters: rawAdapters === true || rawAdapters === "true",
    propagateOutgoing: outgoing.mode,
    ...pair,
    baseUrl: value("baseUrl", "base_url") ?? profiles.find(p => typeof p.base_url === "string")?.base_url ?? "https://api.futureagi.com",
    projectName: value("projectName", "project_name") ?? env.OTEL_SERVICE_NAME ?? profiles.find(p => typeof p.project === "string")?.project ?? (basename(process.cwd()) || "DEFAULT_PROJECT_NAME"),
    projectType: String(value("projectType", "project_type") ?? "observe").toLowerCase() === "experiment" ? "experiment" : "observe",
    instrumentations: names(value("instrumentations", "instrumentations")), disabledInstrumentations: names(value("disabledInstrumentations", "disabled_instrumentations")),
    wireCapture: bool(value("wireCapture", "wire_capture"), true),
    wireMaxBodyBytes: Number.isFinite(max) && max >= 0 ? Math.floor(max) : 4194304,
    sampleRate: Number.isFinite(sample) ? Math.min(1, Math.max(0, sample)) : 1,
    mastraSpanProducer: mastraProducer(value("mastraSpanProducer", "mastra_span_producer")),
    debug: bool(value("debug", "debug", "FI_AUTO_DEBUG"), false), configFile,
    sdkDisabled: bool(env.OTEL_SDK_DISABLED, false), traceConfig,
  };
  outgoingPolicies.set(config, outgoing);
  configCredentials.set(config, credentialSources.flatMap(source => [source.apiKey, source.secretKey]));
  if (incomplete.length) incompleteSources.set(config, incomplete);
  return config;
}
export function enabled(name: string, config: Config): boolean {
  return !config.sdkDisabled && !config.disabledInstrumentations.includes(name) && (!config.instrumentations.length || config.instrumentations.includes(name));
}
/** Off unless the allowlist names it, for integrations that also see non-AI traffic. */
export function explicitlyEnabled(name: string, config: Config): boolean {
  return !config.sdkDisabled && !config.disabledInstrumentations.includes(name) && config.instrumentations.includes(name);
}
export function maskConfig(config: Config): Record<string, unknown> {
  let baseUrl = config.baseUrl;
  try { const url = new URL(baseUrl); url.username = ""; url.password = ""; url.search = ""; url.hash = ""; baseUrl = url.toString(); } catch { baseUrl = "<invalid URL>"; }
  const outgoing = outgoingPolicyForConfig(config);
  return { ...config, baseUrl, apiKey: config.apiKey ? "***" : undefined, secretKey: config.secretKey ? "***" : undefined,
    outgoingPropagation: { mode: outgoing.mode, valid: outgoing.valid, allowRules: outgoing.allowRules, denyRules: outgoing.denyRules } };
}
