import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { resolveConfig } from "../src/config";

// The Future AGI keys go to the resolved base URL. When that URL comes from a file or profile rather than the
// application's own code or environment, init() names its host once, and nothing else about the URL.
function runInit(env: Record<string, string>): Promise<string> {
  return new Promise(done => {
    const child = spawn(process.execPath, ["--import", "tsx", join(__dirname, "apps", "base-host.ts")], {
      cwd: join(__dirname, ".."), timeout: 60000,
      env: { PATH: process.env.PATH, HOME: process.env.HOME, FI_API_KEY: "synthetic-fi-key", FI_SECRET_KEY: "synthetic-fi-secret", FI_PROJECT_NAME: "base-host", ...env },
    });
    let stderr = ""; child.stderr.on("data", c => { stderr += c; }); child.on("close", () => done(stderr));
  });
}
const FILE_URL = "https://user:pw@collector.test.invalid:8443/private/path?token=value-in-the-query#fragment";

describe("the resolved base URL", () => {
  let dir: string, file: string;
  beforeEach(() => { dir = mkdtempSync(join(tmpdir(), "base-host-")); file = join(dir, "futureagi.toml"); });
  afterEach(() => rmSync(dir, { recursive: true, force: true }));

  test("its source is reported", () => {
    writeFileSync(file, `base_url = "${FILE_URL}"\n`);
    expect((resolveConfig({ configFile: file }, {}) as any).baseUrlSource).toBe("config file");
    expect((resolveConfig({ configFile: file, baseUrl: "https://code.test.invalid" }, {}) as any).baseUrlSource).toBe("code");
    expect((resolveConfig({ configFile: file }, { FI_BASE_URL: "https://env.test.invalid" }) as any).baseUrlSource).toBe("environment");
    expect((resolveConfig({ configFile: join(dir, "missing.toml") }, {}) as any).baseUrlSource).toBe("default");
  });
  test("from a config file, init() logs the host once and no other part of the URL", async () => {
    writeFileSync(file, `base_url = "${FILE_URL}"\n`);
    const stderr = await runInit({ FI_TEST_CONFIG_FILE: file });
    const lines = stderr.split("\n").filter(line => line.includes("collector.test.invalid"));
    expect(lines).toHaveLength(1);
    expect(lines[0]).toContain("collector.test.invalid:8443");
    expect(lines[0]).toContain("config file");
    for (const part of ["user", "pw", "private", "path", "token", "value-in-the-query", "fragment", "synthetic-fi"]) expect(lines[0]).not.toContain(part);
  });
  test("from the application's own code or environment, init() does not add a line", async () => {
    expect(await runInit({ FI_TEST_BASE_URL: "https://code.test.invalid" })).not.toContain("code.test.invalid");
    expect(await runInit({ FI_BASE_URL: "https://env.test.invalid", FI_TEST_CONFIG_FILE: join(dir, "missing.toml") })).not.toContain("env.test.invalid");
  });
});
