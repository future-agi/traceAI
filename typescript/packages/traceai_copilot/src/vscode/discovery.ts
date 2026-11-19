import { homedir } from "node:os";
import { isAbsolute, join, resolve } from "node:path";
import { harness, type Harness, parseSettings, prefixes, readSettingsFile, EDITOR_HOLD } from "./settings.js";

export interface DiscoveryOptions {
  home?: string;
  env?: NodeJS.ProcessEnv;
  platform?: NodeJS.Platform;
  projectDir?: string;
}
export interface EditorLocation {
  userSettings: string;
  workspaceSettings?: string;
  selectedHarness?: Harness;
  channel: "stable" | "insiders";
  profile: "default" | "explicit";
}
export function discoverSettings(options: DiscoveryOptions = {}): EditorLocation {
  const env = options.env ?? process.env;
  const home = options.home ?? homedir();
  const platform = options.platform ?? process.platform;
  if (platform !== "darwin" && platform !== "linux") throw new Error("VSCODE_PLATFORM_NOT_QUALIFIED");
  if (env.VSCODE_AGENT_FOLDER || env.SSH_CONNECTION || env.REMOTE_CONTAINERS) throw new Error("VSCODE_REMOTE_HOST_NOT_QUALIFIED");
  const channel = env.FI_COPILOT_VSCODE_CHANNEL ?? "stable";
  if (channel !== "stable" && channel !== "insiders") throw new Error("VSCODE_INVALID_CHANNEL");
  const directory = channel === "stable" ? "Code" : "Code - Insiders";
  const custom = env.FI_COPILOT_VSCODE_USER_DATA_DIR;
  if (custom && !isAbsolute(custom)) throw new Error("VSCODE_ABSOLUTE_USER_DATA_DIR_REQUIRED");
  if (env.VSCODE_PORTABLE && !isAbsolute(env.VSCODE_PORTABLE)) throw new Error("VSCODE_ABSOLUTE_PORTABLE_DIR_REQUIRED");
  const userData = custom ?? (env.VSCODE_PORTABLE ? join(env.VSCODE_PORTABLE, "user-data") :
    platform === "darwin" ? join(home, "Library", "Application Support", directory) :
      join(env.XDG_CONFIG_HOME || join(home, ".config"), directory));
  const profile = env.FI_COPILOT_VSCODE_PROFILE_ID;
  // This is VS Code's on-disk profile id, not the CLI --profile display name.
  if (profile && !/^[A-Za-z0-9_-]{1,100}$/.test(profile)) throw new Error("VSCODE_INVALID_PROFILE_ID");
  return {
    userSettings: profile ? join(userData, "User", "profiles", profile, "settings.json") : join(userData, "User", "settings.json"),
    workspaceSettings: options.projectDir ? join(resolve(options.projectDir), ".vscode", "settings.json") : undefined,
    selectedHarness: harness(env.FI_COPILOT_VSCODE_HARNESS),
    channel,
    profile: profile ? "explicit" : "default",
  };
}
const telemetryEnv = [
  "COPILOT_OTEL_ENABLED", "COPILOT_OTEL_ENDPOINT", "COPILOT_OTEL_CAPTURE_CONTENT",
  "COPILOT_OTEL_CAPTURE_IDENTITY", "COPILOT_OTEL_EXPORTER_TYPE", "COPILOT_OTEL_PROTOCOL",
  "COPILOT_OTEL_FILE_EXPORTER_PATH", "COPILOT_OTEL_DB_SPAN_EXPORTER_ENABLED",
  "COPILOT_OTEL_MAX_ATTRIBUTE_SIZE_CHARS", "COPILOT_OTEL_HTTP_INSTRUMENTATION",
  "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
  "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "OTEL_EXPORTER_OTLP_PROTOCOL",
  "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "OTEL_RESOURCE_ATTRIBUTES", "OTEL_SERVICE_NAME",
];
export interface EditorInspection {
  qualified: false;
  registered: false;
  activated: false;
  selectedHarness?: Harness;
  scope: "user" | "workspace";
  provenance: Record<string, "user" | "workspace">;
  environmentOverrides: string[];
  /** Static codes only: no raw setting, path, header, account or policy values. */
  issues: string[];
  policy: "not-observable-outside-editor";
}
export function inspectSettings(userText: string | undefined, workspaceText: string | undefined,
                                selected: Harness | undefined, env: NodeJS.ProcessEnv = {}): EditorInspection {
  const user = parseSettings(userText), workspace = parseSettings(workspaceText);
  const issues = [EDITOR_HOLD];
  if (!selected) issues.push("VSCODE_HARNESS_SELECTION_REQUIRED");
  if (userText === undefined) issues.push("VSCODE_USER_SETTINGS_NOT_FOUND");
  const provenance: Record<string, "user" | "workspace"> = {};
  const allowed = ["enabled", "exporterType", "otlpEndpoint", "captureContent", "captureIdentity", "dbSpanExporter.enabled", "outfile", "headers", "protocol", "resourceAttributes", "maxAttributeSizeChars"];
  const effective = { ...user, ...workspace };
  for (const prefix of Object.values(prefixes)) {
    for (const suffix of allowed) {
      const key = prefix + suffix;
      if (Object.prototype.hasOwnProperty.call(workspace, key)) provenance[key] = "workspace";
      else if (Object.prototype.hasOwnProperty.call(user, key)) provenance[key] = "user";
    }
  }
  // This is file provenance, not VS Code's effective value; application-scoped and managed
  // configuration may ignore workspace values. Never infer activation or policy from it.
  if (Object.values(provenance).includes("workspace")) issues.push("VSCODE_WORKSPACE_SETTINGS_REQUIRE_EDITOR_RESOLUTION");
  if (effective["telemetry.telemetryLevel"] === "off") issues.push("VSCODE_TELEMETRY_DISABLED");
  const active = Object.entries(prefixes).filter(([, p]) => effective[p + "enabled"] === true || effective[p + "dbSpanExporter.enabled"] === true);
  if (active.length > 1) issues.push("VSCODE_MULTIPLE_TELEMETRY_HARNESSES");
  if (selected && active.some(([name]) => name !== selected)) issues.push("VSCODE_OTHER_HARNESS_ENABLED");
  if (selected) {
    const p = prefixes[selected];
    if (effective[p + "captureIdentity"] === true) issues.push("VSCODE_NATIVE_IDENTITY_CAPTURE_ENABLED");
    if (effective[p + "headers"] !== undefined) issues.push("VSCODE_EXISTING_HEADER_CONFIGURATION");
    if (effective[p + "dbSpanExporter.enabled"] === true) issues.push("VSCODE_DB_FANOUT_REQUIRES_QUALIFICATION");
    if (effective[p + "exporterType"] === "file" || effective[p + "outfile"]) issues.push("VSCODE_FILE_EXPORT_REQUIRES_PRIVACY_REVIEW");
  }
  const environmentOverrides = telemetryEnv.filter((key) => env[key] !== undefined);
  if (environmentOverrides.length) issues.push("VSCODE_INHERITED_TELEMETRY_CONFIGURATION");
  issues.push("VSCODE_MANAGED_POLICY_NOT_OBSERVABLE", "VSCODE_GUI_ENVIRONMENT_NOT_OBSERVABLE");
  return {
    qualified: false, registered: false, activated: false, selectedHarness: selected,
    scope: workspaceText === undefined ? "user" : "workspace", provenance, environmentOverrides,
    issues, policy: "not-observable-outside-editor",
  };
}
export async function inspectEditor(options: DiscoveryOptions = {}): Promise<EditorInspection> {
  const location = discoverSettings(options);
  const user = await readSettingsFile(location.userSettings);
  const workspace = location.workspaceSettings ? await readSettingsFile(location.workspaceSettings) : undefined;
  return inspectSettings(user, workspace, location.selectedHarness, options.env ?? process.env);
}
