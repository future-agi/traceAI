import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { realpath } from "node:fs/promises";
import { contentHash, readJson } from "./files.js";

export const DEFAULT_ENDPOINT = "https://api.futureagi.com/tracer/v1/traces";
export const TRANSPORT_CONTRACT = "finalized-standard-otlp-v1" as const;
export interface Credentials {
  apiKey: string;
  secretKey: string;
}
export interface AgentSettings {
  schemaVersion: 1;
  enabled?: boolean;
  profile?: string;
  projectName?: string;
  captureContent?: boolean;
  userId?: string;
  environment?: string;
  tags?: string[];
  transport?: { baseUrl?: string; endpoint?: string };
}
export interface Profile extends Credentials {
  endpoint: string;
  projectName: string;
  validatedAt: string;
  captureContent: boolean;
  identityKey: string;
  userId: string;
  projectBinding?: string;
}
export interface CredentialStore {
  schemaVersion: 1;
  defaultProfile?: string;
  profiles: Record<string, Profile>;
}
export interface Manifest {
  schemaVersion: 1;
  disabled: Record<string, boolean>;
  generations?: Record<string, number>;
  registrations: Record<
    string,
    {
      version: string;
      scope: "user" | "project";
      status: string;
      ownership?: Record<string, string>;
    }
  >;
}
export interface ResolveOptions {
  agent: string;
  home?: string;
  env?: NodeJS.ProcessEnv;
  projectDir?: string;
  /** Already discovered host directory (required for profile-based/unknown hosts). */
  agentDir?: string;
  flags?: Partial<Omit<AgentSettings, "schemaVersion">>;
}
export interface ResolvedConfig {
  agent: string;
  enabled: boolean;
  captureContent: boolean;
  projectName: string;
  endpoint: string;
  userId?: string;
  environment?: string;
  tags: string[];
  profile?: string;
  credentials?: Credentials;
  identityKey?: string;
  validatedAt?: string;
  provenance: Record<string, string>;
  paths: ConfigPaths;
  hideInputs: boolean;
  hideOutputs: boolean;
  hideErrorDetails: boolean;
  debug: boolean;
  destinationApproved: boolean;
  issues: string[];
  generation: number;
}
export interface ConfigPaths {
  configDir: string;
  credentials: string;
  manifest: string;
  user: string;
  project?: string;
  projectBinding?: string;
  stateDir: string;
}
export function assertAgent(agent: string): void {
  if (!/^[a-z][a-z0-9-]{0,47}$/.test(agent)) throw new Error("INVALID_AGENT");
}
export function endpoint(value: string): string {
  let u: URL;
  try {
    u = new URL(value);
  } catch {
    throw new Error("INVALID_ENDPOINT");
  }
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(u.hostname);
  if (
    (u.protocol !== "https:" && !(u.protocol === "http:" && local)) ||
    u.username ||
    u.password ||
    u.search ||
    u.hash
  )
    throw new Error("UNSAFE_ENDPOINT");
  return u.toString();
}
export function endpointFromBase(base: string): string {
  const url = new URL(endpoint(base));
  if (url.pathname !== "/") throw new Error("BASE_URL_MUST_BE_ORIGIN");
  return new URL("/tracer/v1/traces", url).toString();
}
export function destinationId(url: string, project: string): string {
  return contentHash(JSON.stringify([endpoint(url), project])).slice(0, 32);
}
export function hostDirectory(
  agent: string,
  home: string,
  env: NodeJS.ProcessEnv,
  discovered?: string,
): string {
  if (discovered) return resolve(discovered);
  const xdg = env.XDG_CONFIG_HOME || join(home, ".config");
  const known: Record<string, string> = {
    "claude-code": env.CLAUDE_CONFIG_DIR || join(home, ".claude"),
    claude: env.CLAUDE_CONFIG_DIR || join(home, ".claude"),
    codex: env.CODEX_HOME || join(home, ".codex"),
    cursor: join(home, ".cursor"),
    pi: env.PI_CODING_AGENT_DIR || join(home, ".pi", "agent"),
    opencode: join(xdg, "opencode"),
    grok: join(home, ".grok"),
    antigravity: join(home, ".gemini", "config"),
    gemini: join(home, ".gemini"),
    copilot: env.COPILOT_HOME || join(home, ".copilot"),
    muse: join(xdg, "muse"),
    kiro: join(home, ".kiro"),
    augment: join(home, ".augment"),
    "oh-my-pi": env.PI_CODING_AGENT_DIR || join(home, ".omp"),
    openclaw: join(home, ".openclaw"),
  };
  const aliases: Record<string, string> = {
    "cursor-cli": "cursor",
    "opencode-v1": "opencode",
    "kiro-cli": "kiro",
    "kiro-ide": "kiro",
  };
  const directory = known[aliases[agent] ?? agent];
  if (!directory) throw new Error("HOST_DIRECTORY_DISCOVERY_REQUIRED");
  return resolve(directory);
}
export function controlPaths(
  options: Pick<ResolveOptions, "agent" | "env" | "home">,
): Omit<ConfigPaths, "user" | "project" | "projectBinding"> {
  assertAgent(options.agent);
  const env = options.env ?? process.env,
    home = options.home ?? homedir();
  const configDir = resolve(
    env.FI_AGENT_CONFIG_DIR ||
      join(env.XDG_CONFIG_HOME || join(home, ".config"), "futureagi", "agents"),
  );
  return {
    configDir,
    credentials: join(configDir, "credentials.json"),
    manifest: join(configDir, "installation.json"),
    stateDir: resolve(
      env.XDG_STATE_HOME || join(home, ".local", "state"),
      "futureagi",
      "agents",
    ),
  };
}
export async function configPaths(
  options: ResolveOptions,
): Promise<ConfigPaths> {
  const env = options.env ?? process.env,
    home = options.home ?? homedir();
  const shared = controlPaths(options);
  const host = hostDirectory(options.agent, home, env, options.agentDir);
  let project: string | undefined, projectBinding: string | undefined;
  if (options.projectDir) {
    const root = await realpath(resolve(options.projectDir));
    projectBinding = contentHash(root);
    const hostNames: Record<string, string> = {
      "claude-code": ".claude",
      claude: ".claude",
      codex: ".codex",
      cursor: ".cursor",
      "cursor-cli": ".cursor",
      "opencode-v1": ".opencode",
      "kiro-cli": ".kiro",
      "kiro-ide": ".kiro",
      pi: ".pi",
      opencode: ".opencode",
      grok: ".grok",
      copilot: ".copilot",
      kiro: ".kiro",
      augment: ".augment",
    };
    project = hostNames[options.agent]
      ? join(root, hostNames[options.agent], "futureagi.json")
      : join(root, ".futureagi", "agents.json");
  }
  return {
    ...shared,
    user: join(host, "futureagi.json"),
    project,
    projectBinding,
  };
}
function bool(value: string | undefined): boolean | undefined {
  if (value === undefined) return undefined;
  if (value === "true" || value === "1") return true;
  if (value === "false" || value === "0") return false;
  throw new Error("INVALID_BOOLEAN_ENV");
}
function settings(value: unknown): AgentSettings {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("INVALID_AGENT_CONFIG");
  const v = value as Record<string, unknown>;
  const allowed = [
    "schemaVersion",
    "enabled",
    "profile",
    "projectName",
    "captureContent",
    "userId",
    "environment",
    "tags",
    "transport",
  ];
  if (Object.keys(v).some((k) => !allowed.includes(k)) || v.schemaVersion !== 1)
    throw new Error("INVALID_AGENT_CONFIG");
  for (const k of ["enabled", "captureContent"])
    if (v[k] !== undefined && typeof v[k] !== "boolean")
      throw new Error("INVALID_AGENT_CONFIG");
  for (const k of ["profile", "projectName", "userId", "environment"])
    if (
      v[k] !== undefined &&
      (typeof v[k] !== "string" ||
        !(v[k] as string).length ||
        (v[k] as string).length > 256 ||
        /[\x00-\x1f]/.test(v[k] as string))
    )
      throw new Error("INVALID_AGENT_CONFIG");
  if (
    v.tags !== undefined &&
    (!Array.isArray(v.tags) ||
      v.tags.length > 32 ||
      v.tags.some((t) => typeof t !== "string" || t.length > 128))
  )
    throw new Error("INVALID_AGENT_CONFIG");
  if (v.transport !== undefined) {
    if (
      !v.transport ||
      typeof v.transport !== "object" ||
      Array.isArray(v.transport)
    )
      throw new Error("INVALID_AGENT_CONFIG");
    for (const [k, val] of Object.entries(v.transport))
      if (!["baseUrl", "endpoint"].includes(k) || typeof val !== "string")
        throw new Error("INVALID_AGENT_CONFIG");
  }
  return v as unknown as AgentSettings;
}
export async function readSettings(
  path: string,
  agent?: string,
): Promise<AgentSettings | undefined> {
  let raw = await readJson<unknown>(path, false);
  if (raw === undefined) return undefined;
  if (path.endsWith("/.futureagi/agents.json")) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw))
      throw new Error("INVALID_AGENT_CONFIG");
    raw = (raw as Record<string, unknown>)[agent!];
    if (raw === undefined) return undefined;
  }
  return settings(raw);
}
export async function readStore(path: string): Promise<CredentialStore> {
  const store = (await readJson<CredentialStore>(path)) ?? {
    schemaVersion: 1,
    profiles: {},
  };
  if (
    store.schemaVersion !== 1 ||
    !store.profiles ||
    typeof store.profiles !== "object" ||
    Array.isArray(store.profiles)
  )
    throw new Error("INVALID_CREDENTIAL_STORE");
  return store;
}
export function credentialPair(
  env: NodeJS.ProcessEnv,
): Credentials | undefined {
  if (env.FI_API_KEY === undefined && env.FI_SECRET_KEY === undefined)
    return undefined;
  if (!env.FI_API_KEY || !env.FI_SECRET_KEY)
    throw new Error("INCOMPLETE_CREDENTIAL_OVERRIDE");
  return { apiKey: env.FI_API_KEY, secretKey: env.FI_SECRET_KEY };
}
export async function resolveConfig(
  options: ResolveOptions,
): Promise<ResolvedConfig> {
  const env = options.env ?? process.env,
    paths = await configPaths(options),
    issues: string[] = [];
  const user = (await readSettings(paths.user)) ?? { schemaVersion: 1 };
  const project = paths.project
    ? await readSettings(paths.project, options.agent)
    : undefined;
  const store = await readStore(paths.credentials);
  const provenance: Record<string, string> = {};
  const defaultProfile =
    store.profiles[user.profile ?? store.defaultProfile ?? ""];
  let merged: AgentSettings = {
    schemaVersion: 1,
    enabled: !!defaultProfile,
    captureContent: defaultProfile?.captureContent ?? true,
    projectName: defaultProfile?.projectName ?? "coding-agents",
    tags: [],
  };
  const envConfig: Partial<AgentSettings> = {};
  const mapping = {
    FI_PROJECT_NAME: "projectName",
    FI_USER_ID: "userId",
  } as const;
  for (const [key, field] of Object.entries(mapping))
    if (env[key] !== undefined)
      (envConfig as Record<string, unknown>)[field] = env[key];
  if (env.FI_AGENT_ENABLED !== undefined)
    envConfig.enabled = bool(env.FI_AGENT_ENABLED);
  if (env.FI_AGENT_CAPTURE_CONTENT !== undefined)
    envConfig.captureContent = bool(env.FI_AGENT_CAPTURE_CONTENT);
  if (env.FI_OTLP_ENDPOINT !== undefined || env.FI_BASE_URL !== undefined)
    envConfig.transport =
      env.FI_OTLP_ENDPOINT !== undefined
        ? { endpoint: env.FI_OTLP_ENDPOINT }
        : { baseUrl: env.FI_BASE_URL };
  for (const [source, layer] of [
    ["user", user],
    ["project", project],
    ["environment", envConfig],
    ["flags", options.flags],
  ] as const) {
    if (!layer) continue;
    settings({ ...layer, schemaVersion: 1 });
    for (const [key, value] of Object.entries(layer)) {
      if (value !== undefined) {
        (merged as unknown as Record<string, unknown>)[key] = value;
        provenance[key] = source;
      }
    }
  }
  const profileId = merged.profile ?? store.defaultProfile;
  const saved = profileId ? store.profiles[profileId] : undefined;
  const creds =
    credentialPair(env) ??
    (saved ? { apiKey: saved.apiKey, secretKey: saved.secretKey } : undefined);
  const url = merged.transport?.endpoint
    ? endpoint(merged.transport.endpoint)
    : merged.transport?.baseUrl
      ? endpointFromBase(merged.transport.baseUrl)
      : saved?.endpoint
        ? endpoint(saved.endpoint)
        : DEFAULT_ENDPOINT;
  const projectName =
    merged.projectName ?? saved?.projectName ?? "coding-agents";
  const externalCredentials = credentialPair(env) !== undefined;
  const repositorySelectedProfile =
    provenance.profile === "project" &&
    profileId !== (user.profile ?? store.defaultProfile);
  const destinationApproved =
    !!externalCredentials ||
    !!(
      saved &&
      (!repositorySelectedProfile ||
        saved.projectBinding === paths.projectBinding) &&
      saved.endpoint === url &&
      saved.projectName === projectName &&
      (!saved.projectBinding || saved.projectBinding === paths.projectBinding)
    );
  if (creds && !destinationApproved) issues.push("DESTINATION_NOT_APPROVED");
  if (!creds) issues.push("NOT_CONFIGURED");
  const manifest = await readJson<Manifest>(paths.manifest);
  const disabled =
    env.FI_AGENT_ENABLED === "false" ||
    env.FI_AGENT_ENABLED === "0" ||
    manifest?.disabled?.[options.agent] === true;
  // Only the user's protected profile or explicit environment/flags can approve content. A repository cannot raise it.
  const ceiling =
    options.flags?.captureContent ??
    bool(env.FI_AGENT_CAPTURE_CONTENT) ??
    user.captureContent ??
    saved?.captureContent ??
    true;
  const captureContent = !!merged.captureContent && ceiling;
  // Serialized message/image content cannot be selectively recovered safely: preserve stricter switches by omitting the field.
  const hideInputs =
    !captureContent ||
    [
      "FI_HIDE_INPUTS",
      "FI_HIDE_INPUT_TEXT",
      "FI_HIDE_INPUT_MESSAGES",
      "FI_HIDE_INPUT_IMAGES",
    ].some((k) => bool(env[k]) === true);
  const hideOutputs =
    !captureContent ||
    ["FI_HIDE_OUTPUTS", "FI_HIDE_OUTPUT_TEXT", "FI_HIDE_OUTPUT_MESSAGES"].some(
      (k) => bool(env[k]) === true,
    );
  const errorPreference = bool(env.FI_HIDE_ERROR_DETAILS);
  const config: ResolvedConfig = {
    agent: options.agent,
    enabled: !!merged.enabled && !disabled && destinationApproved && !!creds,
    captureContent,
    endpoint: url,
    projectName,
    userId: merged.userId ?? saved?.userId,
    tags: merged.tags ?? [],
    environment: merged.environment,
    profile: profileId,
    validatedAt: saved?.validatedAt,
    provenance,
    paths,
    destinationApproved,
    issues,
    generation: manifest?.generations?.[options.agent] ?? 0,
    hideInputs,
    hideOutputs,
    hideErrorDetails:
      !captureContent ||
      errorPreference === true ||
      (errorPreference === undefined && (hideInputs || hideOutputs)),
    debug: bool(env.FI_AGENT_DEBUG) ?? false,
  };
  // Defense against accidental JSON logging: keys and identity material are not enumerable.
  Object.defineProperty(config, "credentials", {
    value: creds,
    enumerable: false,
  });
  Object.defineProperty(config, "identityKey", {
    value: saved?.identityKey,
    enumerable: false,
  });
  if (disabled) config.provenance.enabled = "kill-switch";
  return config;
}
