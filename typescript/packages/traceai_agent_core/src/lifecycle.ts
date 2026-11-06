import { randomBytes, randomUUID } from "node:crypto";
import { join } from "node:path";
import {
  controlPaths,
  configPaths,
  credentialPair,
  DEFAULT_ENDPOINT,
  destinationId,
  endpoint,
  endpointFromBase,
  readSettings,
  readStore,
  resolveConfig,
  type AgentSettings,
  type Credentials,
  type CredentialStore,
  type Manifest,
  type ResolveOptions,
  type ResolvedConfig,
} from "./config.js";
import { readJson, updateJson, ensurePrivateDirectory } from "./files.js";
import { validateCredentials, type DeliveryResult } from "./validation.js";

export interface AdapterInspection {
  registered: boolean;
  activated?: boolean;
  hostVersion?: string;
  trust?: "required" | "confirmed" | "unknown";
  detail?: string;
  duplicate?: boolean;
}
export interface AdapterContext {
  config: ResolvedConfig;
  scope: "user" | "project";
  projectDir?: string;
}
/** Host installers own host-specific configuration. Core never edits arbitrary host settings. */
export interface CoreAdapter {
  id: string;
  version: string;
  requiresWorker?: boolean;
  detect(
    context: AdapterContext,
  ): Promise<{ supported: boolean; version?: string; reason?: string }>;
  install(context: AdapterContext): Promise<AdapterInspection>;
  inspect(context: AdapterContext): Promise<AdapterInspection>;
  uninstall(context: AdapterContext): Promise<AdapterInspection>;
  run?(context: AdapterContext, args: string[]): Promise<number>;
}
export interface SetupOptions extends ResolveOptions {
  projectName?: string;
  baseUrl?: string;
  otlpEndpoint?: string;
  captureContent?: boolean;
  scope?: "user" | "project";
  configureOnly?: boolean;
  credentials?: Credentials;
  /** Explicit local dialog approval; never accepted from a project file. */
  approveDestination?: boolean;
  adapter?: CoreAdapter;
}
export interface SetupResult {
  configured: boolean;
  registered: boolean;
  code: string;
  validation: DeliveryResult;
  restartRequired?: boolean;
}
const emptyManifest = (): Manifest => ({
  schemaVersion: 1,
  disabled: {},
  registrations: {},
});
export async function setup(options: SetupOptions): Promise<SetupResult> {
  const paths = await configPaths(options),
    env = options.env ?? process.env;
  if (options.scope === "project" && !paths.project)
    throw new Error("PROJECT_DIRECTORY_REQUIRED");
  const store = await readStore(paths.credentials),
    existing = await readSettings(paths.user);
  const old = store.profiles[existing?.profile ?? store.defaultProfile ?? ""];
  const creds =
    options.credentials ??
    credentialPair(env) ??
    (old ? { apiKey: old.apiKey, secretKey: old.secretKey } : undefined);
  if (!creds)
    return {
      configured: false,
      registered: false,
      code: "CREDENTIALS_REQUIRED",
      validation: { code: "UNVERIFIED" },
    };
  const projectName =
    options.projectName ??
    env.FI_PROJECT_NAME ??
    existing?.projectName ??
    old?.projectName ??
    "coding-agents";
  const url = options.otlpEndpoint
    ? endpoint(options.otlpEndpoint)
    : options.baseUrl
      ? endpointFromBase(options.baseUrl)
      : env.FI_OTLP_ENDPOINT
        ? endpoint(env.FI_OTLP_ENDPOINT)
        : env.FI_BASE_URL
          ? endpointFromBase(env.FI_BASE_URL)
          : (old?.endpoint ?? DEFAULT_ENDPOINT);
  if (
    !projectName ||
    projectName.length > 256 ||
    /[\x00-\x1f]/.test(projectName)
  )
    throw new Error("INVALID_PROJECT_NAME");
  if (
    old &&
    !options.credentials &&
    !credentialPair(env) &&
    (url !== old.endpoint || projectName !== old.projectName) &&
    !options.approveDestination
  )
    throw new Error("DESTINATION_APPROVAL_REQUIRED");
  const envCapture = env.FI_AGENT_CAPTURE_CONTENT;
  if (
    envCapture !== undefined &&
    !["true", "false", "1", "0"].includes(envCapture)
  )
    throw new Error("INVALID_BOOLEAN_ENV");
  const captureContent =
    options.captureContent ??
    (envCapture === undefined
      ? undefined
      : envCapture === "true" || envCapture === "1") ??
    old?.captureContent ??
    true;
  const validation = await validateCredentials({
    endpoint: url,
    projectName,
    credentials: creds,
  });
  if (validation.code !== "ACCEPTED")
    return {
      configured: false,
      registered: false,
      code: validation.code,
      validation,
    };
  const profileId =
    destinationId(url, projectName) +
    (options.scope === "project"
      ? `-${paths.projectBinding!.slice(0, 16)}`
      : "");
  await ensurePrivateDirectory(paths.configDir);
  await updateJson<CredentialStore>(paths.credentials, (previous) => {
    const next = previous ?? { schemaVersion: 1, profiles: {} };
    if (next.schemaVersion !== 1 || !next.profiles)
      throw new Error("INVALID_CREDENTIAL_STORE");
    const previousProfile = next.profiles[profileId];
    return {
      ...next,
      defaultProfile: next.defaultProfile ?? profileId,
      profiles: {
        ...next.profiles,
        [profileId]: {
          ...creds,
          endpoint: url,
          projectName,
          validatedAt: validation.acceptedAt!,
          captureContent,
          identityKey:
            previousProfile?.identityKey ?? randomBytes(32).toString("hex"),
          userId: previousProfile?.userId ?? randomUUID(),
          ...(options.scope === "project"
            ? { projectBinding: paths.projectBinding }
            : {}),
        },
      },
    };
  });
  const target = options.scope === "project" ? paths.project! : paths.user;
  const config: AgentSettings = {
    schemaVersion: 1,
    enabled: true,
    profile: profileId,
    projectName,
    captureContent,
  };
  if (target.endsWith("/.futureagi/agents.json"))
    await updateJson<Record<string, AgentSettings>>(target, (old) => ({
      ...old,
      [options.agent]: { ...old?.[options.agent], ...config },
    }));
  else
    await updateJson<AgentSettings>(target, (old) => ({ ...old, ...config }));
  // Setup is an explicit enable action, but FI_AGENT_ENABLED=false still overrides it.
  await updateJson<Manifest>(paths.manifest, (m) => ({
    ...(m ?? emptyManifest()),
    disabled: { ...m?.disabled, [options.agent]: false },
  }));
  if (options.configureOnly)
    return {
      configured: true,
      registered: false,
      code: "CONFIGURED_NOT_REGISTERED",
      validation,
    };
  if (!options.adapter || options.adapter.id !== options.agent)
    return {
      configured: true,
      registered: false,
      code: "ADAPTER_NOT_AVAILABLE",
      validation,
    };
  const resolved = await resolveConfig(options);
  const context: AdapterContext = {
    config: resolved,
    scope: options.scope ?? "user",
    projectDir: options.projectDir,
  };
  try {
    const detected = await options.adapter.detect(context);
    if (!detected.supported)
      return {
        configured: true,
        registered: false,
        code: "UNSUPPORTED_HOST_VERSION",
        validation,
      };
    // Managed hook/native workers are a distinct release gate, not the held preview daemon.
    if (options.adapter.requiresWorker)
      return {
        configured: true,
        registered: false,
        code: "MANAGED_WORKER_NOT_QUALIFIED",
        validation,
      };
    const installation = await options.adapter.install(context);
    const observed = await options.adapter.inspect(context);
    const registered =
      installation.registered && observed.registered && !observed.duplicate;
    await updateJson<Manifest>(paths.manifest, (m) => ({
      ...(m ?? emptyManifest()),
      registrations: {
        ...m?.registrations,
        [options.agent]: {
          version: options.adapter!.version,
          scope: context.scope,
          status: registered ? "REGISTERED" : "NOT_REGISTERED",
        },
      },
    }));
    return {
      configured: true,
      registered,
      code: registered ? "REGISTERED" : "NOT_REGISTERED",
      validation,
      restartRequired: observed.activated !== true,
    };
  } catch {
    return {
      configured: true,
      registered: false,
      code: "INSTALLATION_FAILED",
      validation,
    };
  }
}
export async function disable(options: ResolveOptions): Promise<void> {
  const paths = controlPaths(options);
  // Fence first. No profile lookup, credentials, registration or network needed.
  await updateJson<Manifest>(paths.manifest, (m) => ({
    ...(m ?? emptyManifest()),
    disabled: { ...m?.disabled, [options.agent]: true },
    generations: {
      ...m?.generations,
      [options.agent]: (m?.generations?.[options.agent] ?? 0) + 1,
    },
  }));
}
export async function enable(options: ResolveOptions): Promise<void> {
  const config = await resolveConfig(options);
  if (!config.destinationApproved || !config.credentials)
    throw new Error("SETUP_REQUIRED");
  await updateJson<Manifest>(config.paths.manifest, (m) => ({
    ...(m ?? emptyManifest()),
    disabled: { ...m?.disabled, [options.agent]: false },
  }));
}
export interface DeliveryState {
  pending: number;
  exported: number;
  dropped: number;
  lastAcceptedAt?: string;
  lastError?: string;
  activatedAt?: string;
}
export interface AgentStatus {
  agent: string;
  enabled: boolean;
  destination: string;
  projectName: string;
  privacy: string;
  provenance: Record<string, string>;
  credentials: "present" | "missing";
  credentialCheck: string;
  validatedAt?: string;
  registration: string;
  activation: string;
  trust: string;
  adapterVersion?: string;
  hostVersion?: string;
  productReadback: "unverified";
  productUrl?: string;
  delivery: Omit<DeliveryState, "pending"> & { pending: number | null };
  issues: string[];
  exitCode: number;
}
export async function inspectStatus(
  options: ResolveOptions & { check?: boolean; adapter?: CoreAdapter },
): Promise<AgentStatus> {
  const config = await resolveConfig(options),
    manifest = await readJson<Manifest>(config.paths.manifest);
  const saved = manifest?.registrations?.[options.agent];
  const delivery = (await readJson<DeliveryState>(
    join(config.paths.stateDir, `${options.agent}.json`),
  )) ?? { pending: 0, exported: 0, dropped: 0 };
  let inspection: AdapterInspection | undefined;
  if (options.adapter) {
    try {
      inspection = await options.adapter.inspect({
        config,
        scope: saved?.scope ?? "user",
        projectDir: options.projectDir,
      });
    } catch {
      /* Unknown, never connected by inference. */
    }
  }
  let check = config.validatedAt ? "previously-accepted" : "unverified";
  if (options.check && config.credentials && config.destinationApproved)
    check = (
      await validateCredentials({
        endpoint: config.endpoint,
        projectName: config.projectName,
        credentials: config.credentials,
      })
    ).code;
  const registration = inspection
    ? inspection.registered && !inspection.duplicate
      ? "REGISTERED"
      : "NOT_REGISTERED"
    : saved
      ? "UNVERIFIED_REGISTRATION"
      : "NOT_REGISTERED";
  const issues = [...config.issues];
  if (registration !== "REGISTERED") issues.push(registration);
  if (!config.enabled) issues.push("DISABLED");
  if (inspection?.duplicate) issues.push("DUPLICATE_REGISTRATION");
  if (!inspection?.activated && !delivery.activatedAt)
    issues.push("ACTIVATION_NOT_OBSERVED");
  if (delivery.lastError) issues.push(`EXPORT_${delivery.lastError}`);
  if (inspection?.trust === "required") issues.push("TRUST_REQUIRED");
  if (options.check && check !== "ACCEPTED")
    issues.push("CREDENTIAL_CHECK_NOT_ACCEPTED");
  return {
    agent: options.agent,
    enabled: config.enabled,
    destination: config.endpoint,
    projectName: config.projectName,
    privacy: config.captureContent ? "content-opt-in" : "structure-only",
    provenance: config.provenance,
    credentials: config.credentials ? "present" : "missing",
    credentialCheck: check,
    validatedAt: config.validatedAt,
    registration,
    activation:
      inspection?.activated || delivery.activatedAt
        ? "OBSERVED"
        : "NOT_OBSERVED",
    trust: inspection?.trust ?? "unknown",
    adapterVersion: saved?.version,
    hostVersion: inspection?.hostVersion,
    productReadback: "unverified",
    delivery: { ...delivery, pending: null },
    issues,
    exitCode: issues.length ? 2 : 0,
  };
}
/** Ownership-aware registration reconciliation; adapter supplies syntax-preserving host edits. */
export async function reconcileRegistration<T>(
  path: string,
  edit: (current: T | undefined) => T,
): Promise<void> {
  await updateJson(path, edit);
}
