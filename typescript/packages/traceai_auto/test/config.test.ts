import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readdirSync, readFileSync } from "node:fs";
import { basename, join, resolve } from "node:path";
import { credentialsForConfig, enabled, maskConfig, parseConfigFile, resolveConfig } from "../src/config";
import { getRegisteredSecrets, seedSecrets } from "@traceai/fi-core";
import { integrations, inventory } from "../src/libraries";

test("flat TOML subset handles strings, comments, arrays and ignored tables", () => {
  expect(parseConfigFile('project_name = "a # b" # comment\nwire_capture = false\nsample_rate = 0.25\ninstrumentations = ["openai", "wire"]\n[ignored]\nbad = {unsupported}\n')).toEqual({ project_name: "a # b", wire_capture: false, sample_rate: 0.25, instrumentations: ["openai", "wire"] });
});
test("configuration precedence is code > environment > file > defaults", () => {
  const dir = mkdtempSync(join(__dirname, ".config-")), path = join(dir, "futureagi.toml");
  try {
    writeFileSync(path, 'project_name = "file"\nsample_rate = 0.25\nwire_capture = false\ninstrumentations = ["wire"]\ndebug = true\n');
    const config = resolveConfig({ configFile: path, projectName: "code" }, { FI_PROJECT_NAME: "environment", FI_SAMPLE_RATE: "0.5", FI_API_KEY: "secret", FI_SECRET_KEY: "secret2" });
    expect(config).toMatchObject({ projectName: "code", sampleRate: 0.5, wireCapture: false, instrumentations: ["wire"], debug: true, projectType: "observe" });
    expect(maskConfig(config)).toMatchObject({ apiKey: "***", secretKey: "***" });
    expect(JSON.stringify(maskConfig(config))).not.toContain('"secret"');
    expect(JSON.stringify(maskConfig(config))).not.toContain('"secret2"');
    expect(resolveConfig({ configFile: path }, { FI_PROJECT_NAME: "environment" }).projectName).toBe("environment");
    expect(resolveConfig({ configFile: path }, {}).projectName).toBe("file");
    expect(resolveConfig({ configFile: join(dir, "missing") }, {}).projectName).toBe(basename(process.cwd()));
    expect(resolveConfig({ configFile: join(dir, "missing") }, { OTEL_SERVICE_NAME: "service" }).projectName).toBe("service");
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
test("privacy options honor file/env/code precedence without mutating the environment", () => {
  const dir = mkdtempSync(join(__dirname, ".privacy-")), path = join(dir, "futureagi.toml");
  try {
    writeFileSync(path, "hide_inputs = true\nhide_outputs = true\n");
    const config = resolveConfig({ configFile: path, traceConfig: { hideOutputs: false } }, { FI_HIDE_INPUTS: "false" });
    expect(config.traceConfig).toEqual({ hideInputs: false, hideOutputs: false });
    expect(resolveConfig({ configFile: path }, {}).traceConfig).toEqual({ hideInputs: true, hideOutputs: true });
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
test("allowlist, denylist, wire setting, disabled SDK and numeric validation", () => {
  const config = resolveConfig({}, { FI_INSTRUMENTATIONS: "OpenAI, Wire", FI_DISABLED_INSTRUMENTATIONS: "openai", FI_SAMPLE_RATE: "2", FI_WIRE_MAX_BODY_BYTES: "bad", FI_AUTO_DEBUG: "true" });
  expect(enabled("openai", config)).toBe(false); expect(enabled("wire", config)).toBe(true); expect(enabled("langchain", config)).toBe(false);
  expect(config).toMatchObject({ sampleRate: 1, wireMaxBodyBytes: 4194304, debug: true });
  expect(enabled("wire", resolveConfig({}, { OTEL_SDK_DISABLED: "true" }))).toBe(false);
});
test.each(["apiKey", "secretKey"] as const)("S7-pre ignores an explicit half-pair %s instead of combining sources", half => {
  const warn = jest.spyOn(console, "warn").mockImplementation(() => {});
  try {
    const config = resolveConfig({ [half]: "explicit-private-half" }, { FI_API_KEY: "env-private-key", FI_SECRET_KEY: "env-private-secret" });
    expect(config).toMatchObject({ apiKey: "env-private-key", secretKey: "env-private-secret" });
    expect(warn).toHaveBeenCalledTimes(1); expect(warn.mock.calls[0][0]).toContain(half === "apiKey" ? "FI_SECRET_KEY" : "FI_API_KEY");
    expect(JSON.stringify(warn.mock.calls)).not.toContain("explicit-private-half");
  } finally { warn.mockRestore(); }
});
test.each(["FI_API_KEY", "FI_SECRET_KEY"])("S7-pre ignores environment half %s and selects the complete file pair", half => {
  const dir = mkdtempSync(join(__dirname, ".pairs-")), path = join(dir, "futureagi.toml"), warn = jest.spyOn(console, "warn").mockImplementation(() => {});
  try {
    writeFileSync(path, 'api_key = "file-private-key"\nsecret_key = "file-private-secret"\n');
    expect(resolveConfig({ configFile: path }, { [half]: "env-private-half" })).toMatchObject({ apiKey: "file-private-key", secretKey: "file-private-secret" });
    expect(warn).toHaveBeenCalledTimes(1); expect(JSON.stringify(warn.mock.calls)).not.toContain("env-private-half");
    expect(resolveConfig({ configFile: path, apiKey: "code-private-key", secretKey: "code-private-secret" }, { FI_API_KEY: "env-private-key", FI_SECRET_KEY: "env-private-secret" })).toMatchObject({ apiKey: "code-private-key", secretKey: "code-private-secret" });
  } finally { warn.mockRestore(); rmSync(dir, { recursive: true, force: true }); }
});
test("S7-pre file and fi profile sources never combine halves, selected profile is isolated", () => {
  const dir = mkdtempSync(join(__dirname, ".profiles-")), path = join(dir, "futureagi.toml"), warn = jest.spyOn(console, "warn").mockImplementation(() => {});
  const cwd = jest.spyOn(process, "cwd").mockReturnValue(dir), env = { XDG_CONFIG_HOME: join(dir, "user"), FI_PROFILE: "test-profile" };
  mkdirSync(join(dir, ".fi"), { recursive: true }); mkdirSync(join(dir, "user/fi"), { recursive: true });
  try {
    writeFileSync(path, 'api_key = "file-private-half"\n[unrelated]\nsecret_key = "must-not-combine"\n');
    writeFileSync(join(dir, ".fi/config.toml"), '[profiles.test-profile]\nsecret_key = "project-private-half"\n');
    writeFileSync(join(dir, "user/fi/config.toml"), '[profiles.default]\napi_key = "wrong-profile-key"\nsecret_key = "wrong-profile-secret"\n[profiles."test-profile"]\napi_key = \'user-private-key\'\nsecret_key = \'user-private-secret\'\nbase_url = "https://collector.invalid"\n');
    expect(resolveConfig({ configFile: path }, env)).toMatchObject({ apiKey: "user-private-key", secretKey: "user-private-secret", baseUrl: "https://collector.invalid" });
    expect(warn).toHaveBeenCalledTimes(2);
    writeFileSync(join(dir, "user/fi/config.toml"), '[profiles.test-profile]\napi_key = "user-private-half"\n');
    const missing = resolveConfig({ configFile: path, apiKey: "code-private-half" }, { ...env, FI_SECRET_KEY: "env-private-half" });
    expect(missing.apiKey).toBeUndefined(); expect(missing.secretKey).toBeUndefined();
    expect(JSON.stringify(warn.mock.calls)).not.toMatch(/private-half|private-key|private-secret/);
  } finally { cwd.mockRestore(); warn.mockRestore(); rmSync(dir, { recursive: true, force: true }); }
});
test.each(["api_key", "secret_key"])("S7 ignored %s from every config/profile source stays private and seeds S5", half => {
  const dir = mkdtempSync(join(__dirname, ".ignored-pairs-")), path = join(dir, "futureagi.toml");
  const cwd = jest.spyOn(process, "cwd").mockReturnValue(dir), warn = jest.spyOn(console, "warn").mockImplementation(() => {});
  mkdirSync(join(dir, ".fi"), { recursive: true }); mkdirSync(join(dir, "user/fi"), { recursive: true });
  try {
    writeFileSync(path, `${half} = "ignored-file-private-half-${half}"\n`);
    writeFileSync(join(dir, ".fi/config.toml"), `[profiles.default]\n${half} = "ignored-project-private-half-${half}"\n`);
    writeFileSync(join(dir, "user/fi/config.toml"), `[profiles.default]\n${half} = "ignored-user-private-half-${half}"\n`);
    const config = resolveConfig({ configFile: path, apiKey: "selected-code-private-key", secretKey: "selected-code-private-secret" }, { XDG_CONFIG_HOME: join(dir, "user"), FI_API_KEY: "ignored-environment-private-half" });
    seedSecrets({}, credentialsForConfig(config));
    for (const source of ["file", "project", "user"]) {
      const secret = `ignored-${source}-private-half-${half}`;
      expect(getRegisteredSecrets()).toContain(secret);
      expect(JSON.stringify(config)).not.toContain(secret); expect(JSON.stringify(maskConfig(config))).not.toContain(secret);
    }
    expect(getRegisteredSecrets()).toContain("ignored-environment-private-half");
    expect(config).toMatchObject({ apiKey: "selected-code-private-key", secretKey: "selected-code-private-secret" });
    expect(JSON.stringify(warn.mock.calls)).not.toContain("private");
  } finally { cwd.mockRestore(); warn.mockRestore(); rmSync(dir, { recursive: true, force: true }); }
});
test("registry covers every instrumentation package and records exported class and targets", () => {
  const packages = resolve(__dirname, "../..");
  const paths = readdirSync(packages).filter(p => p.startsWith("traceai_") && p !== "traceai_auto");
  expect(integrations).toHaveLength(paths.length);
  for (const path of paths) {
    const manifest = JSON.parse(readFileSync(join(packages, path, "package.json"), "utf8"));
    const integration = integrations.find(i => i.package === manifest.name)!;
    expect(integration).toBeDefined(); expect(integration.targets.length).toBeGreaterThan(0);
    if (!integration.manual) {
      const sources = readdirSync(join(packages, path, "src")).filter(f => f.endsWith(".ts")).map(f => readFileSync(join(packages, path, "src", f), "utf8")).join("\n");
      expect(sources).toContain(`export class ${integration.className}`);
    }
  }
  const statuses = inventory(resolveConfig({ instrumentations: ["wire"] }, {}));
  expect(statuses.every(s => s.status === "disabled")).toBe(true);
});
