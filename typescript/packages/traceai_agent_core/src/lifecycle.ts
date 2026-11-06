import { randomBytes, randomUUID } from "node:crypto";
import { dirname } from "node:path";
import { lstat, rmdir } from "node:fs/promises";
import {
  controlPaths,
  deliveryStatePath,
  environmentCredentialsOnly,
  selectSetupProfile,
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
import {
  contentHash,
  readJson,
  readProtected,
  removeProtected,
  restoreJsonEntry,
  updateJson,
  ensurePrivateDirectory,
} from "./files.js";
import { validateCredentials, type DeliveryResult } from "./validation.js";

export interface FinisherStatus {
  pending: {
    turn: string;
    startedAt: string;
    deadline: string;
    recoverable: boolean;
  }[];
  last?: { turn: string; result: string; finishedAt: string };
}
export interface AdapterInspection {
  finisher?: FinisherStatus;
  registered: boolean;
  pendingTurns?: number;
  recovery?: string;
  activated?: boolean;
  hostVersion?: string;
  trust?: "required" | "confirmed" | "unknown";
  detail?: string;
  issues?: string[];
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
  diagnostics?: Readonly<Record<string, string>>;
  setupErrors?: Readonly<Record<string, string>>;
  detect(
    context: AdapterContext,
  ): Promise<{ supported: boolean; version?: string; reason?: string }>;
  install(context: AdapterContext): Promise<AdapterInspection>;
  update?(context: AdapterContext): Promise<AdapterInspection>;
  inspect(context: AdapterContext): Promise<AdapterInspection>;
  uninstall(context: AdapterContext): Promise<AdapterInspection>;
  uninstallUnconfigured?(options: ResolveOptions): Promise<AdapterInspection>;
  runUnconfigured?(args: string[], projectDir?: string): Promise<number>;
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
export interface AdapterDiagnostic {
  code: string;
  action: string;
}
const knownDiagnostics: Readonly<Record<string, string>> = {
  NATIVE_TRACING_CONFLICT:
    "Disable conflicting native tracing in the host settings and launching environment, then retry. Existing settings were not changed.",
  INVALID_HOST_HOOKS:
    "The host hooks structure is unsupported; correct it before retrying.",
  RUNTIME_MISSING_OR_CHANGED:
    "Restore the Node.js installation and update the owned hook runtime.",
  DUPLICATE_REGISTRATION:
    "Remove the duplicate tracing registration before retrying.",
  NODE_INTERPRETER_MISSING:
    "Restore the Node.js installation and update this integration.",
  UNKNOWN_HOOK_SHAPE: "Update the integration for this host hook schema.",
  HOOK_RUNTIME_MISSING: "Run update to restore the owned hook runtime.",
  HOOK_RUNTIME_CHANGED:
    "Review the runtime changes and run update to repair the integration.",
};
function adapterDiagnostics(
  adapter: CoreAdapter | undefined,
  ...codes: unknown[]
): AdapterDiagnostic[] {
  const allowed = {
    ...knownDiagnostics,
    ...adapter?.setupErrors,
    ...adapter?.diagnostics,
  };
  return [...new Set(codes)].flatMap((code) =>
    typeof code === "string" &&
    /^[A-Z][A-Z0-9_]{0,80}$/.test(code) &&
    Object.hasOwn(allowed, code)
      ? [{ code, action: allowed[code] }]
      : [],
  );
}
const inspectionCodes = new Set(
  "AUGMENT_REGISTRATION_NEEDS_ATTENTION CLOUD_ENVIRONMENT_NOT_PREPARED CURSOR_IDE_SHARED_HOOKS_RETAINED DEVIN_SIGN_IN_AND_NATIVE_PLUGIN_TRUST_REQUIRED DEVIN_SIGN_IN_TRUST_OR_POLICY_REQUIRED ENABLE_PLUGIN_IN_AGY EXISTING_PLUGIN_REQUIRES_MANUAL_UPDATE_OR_ENABLE GUI_ACTIVATION_NOT_QUALIFIED HOOK_MODIFIED_PRESERVED HOOK_REMOVED_SHARED_PROFILE_RETAINED INSTALLED_CONTENT_MISMATCH KIRO_IDE_PROJECT_DIRECTORY_REQUIRED NATIVE_PLUGIN_NOT_VERIFIED NATIVE_SOURCE_VERIFICATION_REQUIRED_BEFORE_REMOVAL NODE_INTERPRETER_MISSING NOT_INSTALLED NOT_OWNED_OR_CHANGED_REMOVE_WITH_OMP NOT_OWNED_REMOVE_WITH_PI NOT_REGISTERED NO_OWNED_GUI_REGISTRATION NO_OWNED_REGISTRATION OWNERSHIP_CHANGED_NOT_REMOVED OWNERSHIP_REVIEW_REQUIRED PLUGIN_MODIFIED PROFILE_RETAINED PROJECT_HOOKS_MISSING PROJECT_PLUGIN_INSTALL_UNQUALIFIED REGISTRATION_CHANGED REMOVE_DUPLICATE_FUTUREAGI_HOOKS REMOVE_LEGACY_FUTUREAGI_HOOKS RUNTIME_MODIFIED_OR_OUTDATED SETUP_REQUIRED UNEXPECTED_INSTALL_LOCATION UNINSTALLED UNOWNED_PLUGIN_CONFLICT UNOWNED_PLUGIN_PRESERVED USER_PLUGIN_REQUIRED_USE_PROJECT_CONFIG_OVERRIDE USE_USER_HOOKS_AND_PROJECT_CONFIG_OVERRIDE".split(
    " ",
  ),
);
function safeInspection(
  adapter: CoreAdapter | undefined,
  inspection: AdapterInspection,
): AdapterInspection {
  const allowed = adapterDiagnostics(adapter, inspection.detail);
  const detail =
    allowed.length ||
    (!adapter?.diagnostics &&
      !adapter?.setupErrors &&
      inspectionCodes.has(inspection.detail ?? ""))
      ? inspection.detail
      : undefined;
  return { ...inspection, detail };
}
export interface SetupResult {
  configured: boolean;
  registered: boolean;
  code: string;
  validation: DeliveryResult;
  restartRequired?: boolean;
  trust?: AdapterInspection["trust"];
  detail?: string;
  diagnostics?: AdapterDiagnostic[];
  inspection?: AdapterInspection;
  /** Compatibility alias for adapters that call this registration. */
  registration?: AdapterInspection;
}
const emptyManifest = (): Manifest => ({
  schemaVersion: 1,
  disabled: {},
  registrations: {},
});
/** All legacy setup preview APIs share the same scope-aware defaults. */
export async function setupBaseline(options: SetupOptions) {
  const selected = await selectSetupProfile(options);
  return { ...selected, old: selected.saved };
}
export async function readSetupDefaults(options: SetupOptions) {
  return { ...(await setupBaseline(options)), repositoryProfileTrusted: true };
}
export async function setupDefaults(options: SetupOptions) {
  const selected = await setupBaseline(options);
  return {
    ...selected,
    profile: selected.old,
    projectName:
      selected.existing.projectName ??
      selected.old?.projectName ??
      "coding-agents",
    captureContent:
      selected.existing.captureContent !== false &&
      selected.old?.captureContent !== false,
  };
}
export async function setup(options: SetupOptions): Promise<SetupResult> {
  const { paths, existing, old, project } = await setupBaseline(options);
  const env = options.env ?? process.env;
  const environmentOnly = environmentCredentialsOnly(options.agent);
  if (environmentOnly && (options.scope !== "project" || !paths.project))
    throw new Error("CLOUD_PROJECT_SCOPE_REQUIRED");
  const creds = environmentOnly
    ? credentialPair(env)
    : (options.credentials ??
      credentialPair(env) ??
      (old ? { apiKey: old.apiKey, secretKey: old.secretKey } : undefined));
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
  if (environmentOnly) {
    const destination = env.FI_OTLP_ENDPOINT
      ? endpoint(env.FI_OTLP_ENDPOINT)
      : env.FI_BASE_URL
        ? endpointFromBase(env.FI_BASE_URL)
        : DEFAULT_ENDPOINT;
    if (url !== destination) throw new Error("CLOUD_DESTINATION_ENV_REQUIRED");
  }
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
  const explicitTransport = !!(
    options.otlpEndpoint ||
    options.baseUrl ||
    env.FI_OTLP_ENDPOINT ||
    env.FI_BASE_URL
  );
  const explicitProject =
    options.projectName !== undefined || env.FI_PROJECT_NAME !== undefined;
  if (
    !options.approveDestination &&
    !environmentOnly &&
    project &&
    ((project.transport &&
      !explicitTransport &&
      (project.transport.endpoint
        ? endpoint(project.transport.endpoint)
        : project.transport.baseUrl
          ? endpointFromBase(project.transport.baseUrl)
          : url) !== old?.endpoint) ||
      (project.projectName &&
        !explicitProject &&
        project.projectName !== old?.projectName))
  )
    throw new Error("DESTINATION_APPROVAL_REQUIRED");
  const envCapture =
    env.FI_AGENT_CAPTURE_CONTENT ??
    (options.agent === "hermes" ? env.FI_HERMES_CAPTURE_CONTENT : undefined);
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
    existing?.captureContent ??
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
    (paths.hostBinding ? `-h${paths.hostBinding}` : "") +
    (options.scope === "project"
      ? `-${paths.projectBinding!.slice(0, 16)}`
      : "");
  await ensurePrivateDirectory(paths.configDir);
  if (!environmentOnly)
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
            ...(paths.hostBinding ? { hostBinding: paths.hostBinding } : {}),
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
    ...(environmentOnly ? {} : { profile: profileId }),
    projectName,
    captureContent,
    transport: { endpoint: url },
  };
  const previousText = await readProtected(target, false);
  const previousCloudPolicy = environmentOnly
    ? await readProtected(paths.user, false)
    : undefined;
  const createdDirectories: string[] = [];
  for (const file of environmentOnly ? [target, paths.user] : [target]) {
    for (
      let directory = dirname(file);
      dirname(directory) !== directory;
      directory = dirname(directory)
    ) {
      const exists = await lstat(directory).then(
        () => true,
        (error: NodeJS.ErrnoException) => {
          if (error.code === "ENOENT") return false;
          throw error;
        },
      );
      if (exists) break;
      createdDirectories.push(directory);
    }
  }
  const sharedSettings = target.endsWith("/.futureagi/agents.json");
  let entryOwnership: { hash: string; previous?: AgentSettings } | undefined;
  if (sharedSettings)
    await updateJson<Record<string, AgentSettings>>(
      target,
      (old) => {
        const entry = { ...old?.[options.agent], ...config };
        entryOwnership = { hash: contentHash(JSON.stringify(entry)), previous: old?.[options.agent] };
        return { ...old, [options.agent]: entry };
      },
      false,
    );
  else
    await updateJson<AgentSettings>(
      target,
      (old) => ({ ...old, ...config }),
      false,
    );
  if (environmentOnly)
    await updateJson<AgentSettings>(paths.user, () => ({
      schemaVersion: 1,
      captureContent,
      projectName,
      transport: { endpoint: url },
    }));
  const targetHash = contentHash((await readProtected(target, false))!);
  const cloudPolicyHash = environmentOnly
    ? contentHash((await readProtected(paths.user, false))!)
    : undefined;
  // Setup is an explicit enable action, but FI_AGENT_ENABLED=false still overrides it.
  await updateJson<Manifest>(paths.manifest, (m) => ({
    ...(m ?? emptyManifest()),
    disabled: { ...m?.disabled, [options.agent]: false },
    // Reconfiguration fences every pending snapshot, including same-route privacy changes.
    generations: {
      ...m?.generations,
      [options.agent]: (m?.generations?.[options.agent] ?? 0) + 1,
    },
    registrations: {
      ...m?.registrations,
      [options.agent]: {
        version:
          options.adapter?.version ??
          m?.registrations?.[options.agent]?.version ??
          "unregistered",
        scope: options.scope ?? "user",
        status: "CONFIGURED_NOT_REGISTERED",
        ownership: {
          ...m?.registrations?.[options.agent]?.ownership,
          ...(!sharedSettings && (previousText === undefined ||
          m?.registrations?.[options.agent]?.ownership?.[target])
            ? { [target]: targetHash }
            : {}),
          ...(cloudPolicyHash &&
          (previousCloudPolicy === undefined ||
            m?.registrations?.[options.agent]?.ownership?.[paths.user])
            ? { [paths.user]: cloudPolicyHash }
            : {}),
        },
        settingsOwnership: {
          ...m?.registrations?.[options.agent]?.settingsOwnership,
          ...(entryOwnership ? { [target]: {
            hash: entryOwnership.hash,
            previous: m?.registrations?.[options.agent]?.settingsOwnership?.[target]
              ? m.registrations[options.agent].settingsOwnership![target].previous
              : entryOwnership.previous,
          } } : {}),
        },
        createdDirectories: [
          ...new Set([
            ...(m?.registrations?.[options.agent]?.createdDirectories ?? []),
            ...createdDirectories,
          ]),
        ],
      },
    },
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
        diagnostics: adapterDiagnostics(options.adapter, detected.reason),
        inspection: safeInspection(options.adapter, {
          registered: false,
          hostVersion: detected.version,
          detail: detected.reason,
        }),
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
    const installation = safeInspection(
      options.adapter,
      await options.adapter.install(context),
    );
    const observed = installation.registered
      ? safeInspection(options.adapter, await options.adapter.inspect(context))
      : installation;
    const registered =
      installation.registered && observed.registered && !observed.duplicate;
    await updateJson<Manifest>(paths.manifest, (m) => ({
      ...(m ?? emptyManifest()),
      registrations: {
        ...m?.registrations,
        [options.agent]: {
          ...m?.registrations?.[options.agent],
          version: options.adapter!.version,
          scope: context.scope,
          status: registered ? "REGISTERED" : "NOT_REGISTERED",
        },
      },
    }));
    return {
      configured: true,
      registered,
      code:
        observed.trust === "required"
          ? "TRUST_REQUIRED"
          : registered
            ? "REGISTERED"
            : "NOT_REGISTERED",
      validation,
      restartRequired: observed.activated !== true,
      trust: observed.trust,
      detail: observed.detail,
      inspection: observed,
      registration: observed,
      diagnostics: adapterDiagnostics(
        options.adapter,
        installation.detail,
        observed.detail,
      ),
    };
  } catch (error) {
    const diagnostics = adapterDiagnostics(
      options.adapter,
      error instanceof Error ? error.message : undefined,
    );
    return {
      configured: true,
      registered: false,
      code: diagnostics[0]?.code ?? "INSTALLATION_FAILED",
      detail: diagnostics[0]?.action,
      diagnostics,
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
  binding?: string;
}
export interface AgentStatus {
  agent: string;
  pendingTurns?: number;
  registrationDetail?: string;
  adapterDetail?: string;
  detail?: string;
  recovery?: string;
  diagnostics: AdapterDiagnostic[];
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
  finisher?: FinisherStatus;
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
  const recorded = await readJson<DeliveryState>(deliveryStatePath(config));
  const delivery = (recorded &&
  (!config.deliveryBinding || recorded.binding === config.deliveryBinding)
    ? recorded
    : undefined) ?? { pending: 0, exported: 0, dropped: 0 };
  let inspection: AdapterInspection | undefined;
  let diagnostics: AdapterDiagnostic[] = [];
  if (options.adapter) {
    try {
      inspection = await options.adapter.inspect({
        config,
        scope: saved?.scope ?? "user",
        projectDir: options.projectDir,
      });
      inspection = safeInspection(options.adapter, inspection);
      diagnostics = adapterDiagnostics(options.adapter, inspection.detail);
    } catch (error) {
      diagnostics = adapterDiagnostics(
        options.adapter,
        error instanceof Error ? error.message : undefined,
      );
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
    : saved &&
        !["NOT_REGISTERED", "CONFIGURED_NOT_REGISTERED"].includes(saved.status)
      ? "UNVERIFIED_REGISTRATION"
      : "NOT_REGISTERED";
  const issues = [
    ...config.issues,
    ...(inspection?.issues ?? []),
    ...diagnostics.map((d) => d.code),
  ];
  if (inspection?.pendingTurns) issues.push("PENDING_TURNS");
  const activated =
    !!delivery.activatedAt ||
    (options.agent !== "hermes" && inspection?.activated === true);
  if (registration !== "REGISTERED") issues.push(registration);
  if (!config.enabled) issues.push("DISABLED");
  if (inspection?.duplicate) issues.push("DUPLICATE_REGISTRATION");
  if (!activated) issues.push("ACTIVATION_NOT_OBSERVED");
  if (delivery.lastError) issues.push(`EXPORT_${delivery.lastError}`);
  if (inspection?.trust === "required") issues.push("TRUST_REQUIRED");
  if (options.check && check !== "ACCEPTED")
    issues.push("CREDENTIAL_CHECK_NOT_ACCEPTED");
  return {
    agent: options.agent,
    enabled: config.enabled,
    destination: config.endpoint,
    projectName: config.projectName,
    privacy: config.captureContent ? "content-on-by-default" : "structure-only",
    provenance: config.provenance,
    credentials: config.credentials ? "present" : "missing",
    credentialCheck: check,
    validatedAt: config.validatedAt,
    registration,
    activation: activated ? "OBSERVED" : "NOT_OBSERVED",
    registrationDetail: inspection?.detail,
    adapterDetail: inspection?.detail,
    detail: inspection?.detail,
    recovery: inspection?.recovery,
    pendingTurns: inspection?.pendingTurns,
    diagnostics,
    trust: inspection?.trust ?? "unknown",
    adapterVersion: saved?.version,
    hostVersion: inspection?.hostVersion,
    finisher: inspection?.finisher,
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

/** Fence first, then remove only unchanged files and directories created by this setup. */
export async function uninstall(
  options: ResolveOptions & { adapter?: CoreAdapter },
): Promise<AdapterInspection> {
  await disable(options);
  const paths = await configPaths(options);
  const manifest = await readJson<Manifest>(paths.manifest);
  const registration = manifest?.registrations?.[options.agent];
  let result: AdapterInspection = { registered: false };
  if (options.adapter) {
    let config: ResolvedConfig | undefined;
    try {
      config = await resolveConfig(options);
    } catch (error) {
      if (!options.adapter.uninstallUnconfigured) throw error;
    }
    result = config
      ? await options.adapter.uninstall({
          config,
          scope: registration?.scope ?? "user",
          projectDir: options.projectDir,
        })
      : await options.adapter.uninstallUnconfigured!(options);
  }
  if (result.registered) return result;
  const allowedDirectories = new Set<string>();
  for (const file of [paths.user, paths.project]) {
    if (!file) continue;
    for (
      let directory = dirname(file);
      dirname(directory) !== directory;
      directory = dirname(directory)
    )
      allowedDirectories.add(directory);
  }
  for (const [path, hash] of Object.entries(registration?.ownership ?? {})) {
    // A manifest cannot authorize deleting arbitrary host files or credentials.
    if (path !== paths.user && path !== paths.project) continue;
    // Legacy whole-file ownership is not authority over another agent's settings.
    if (path.endsWith("/.futureagi/agents.json")) continue;
    await removeProtected(path, hash, false);
  }
  for (const [path, entry] of Object.entries(registration?.settingsOwnership ?? {})) {
    if (path !== paths.project || !path.endsWith("/.futureagi/agents.json")) continue;
    await restoreJsonEntry(path, options.agent, entry.hash, entry.previous);
  }
  for (const directory of [...(registration?.createdDirectories ?? [])].sort(
    (a, b) => b.length - a.length,
  )) {
    if (allowedDirectories.has(directory))
      await rmdir(directory).catch(() => {});
  }
  await updateJson<Manifest>(paths.manifest, (old) => {
    const next = old ?? emptyManifest();
    const registrations = { ...next.registrations };
    delete registrations[options.agent];
    return { ...next, registrations };
  });
  return result;
}
