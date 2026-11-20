import { spawn } from "node:child_process";
import { join } from "node:path";

/** `init({ allowInsecureHttp: true })` reaches the provider as well as the exporter, so a plaintext self-hosted base URL starts. */
function run(env: Record<string, string>): Promise<{ stdout: string; stderr: string }> {
  return new Promise(done => {
    const child = spawn(process.execPath, ["--import", "tsx", join(__dirname, "apps", "insecure-option.ts")], {
      cwd: join(__dirname, ".."), timeout: 60000,
      env: { PATH: process.env.PATH, HOME: process.env.HOME, FI_PROJECT_NAME: "insecure-option", FI_API_KEY: "synthetic-fi-key-0001", FI_SECRET_KEY: "synthetic-fi-secret-0001", ...env },
    });
    let stdout = "", stderr = ""; child.stdout.on("data", c => { stdout += c; }); child.stderr.on("data", c => { stderr += c; });
    child.on("close", () => done({ stdout, stderr }));
  });
}
const BASE = "http://collector.example:4318";

describe("allowInsecureHttp in init()", () => {
  test("a plaintext self-hosted base URL starts with the option, with the one warning and no value", async () => {
    const { stdout, stderr } = await run({ FI_TEST_BASE_URL: BASE, FI_TEST_ALLOW: "1" });
    expect(stdout).toContain("INIT_OK");
    // The same when the base URL comes from the environment, which the provider reads as well.
    expect((await run({ FI_BASE_URL: BASE, FI_TEST_ALLOW: "1" })).stdout).toContain("INIT_OK");
    expect(stderr.split("\n").filter(line => /FI_ALLOW_INSECURE_HTTP|allowInsecureHttp/.test(line) && /plaintext/i.test(line))).toHaveLength(1);
    expect(stderr).not.toMatch(/synthetic-fi/);
    expect(stderr).not.toMatch(/could not initialize/);
  });
  test("without the option it is refused, naming the setting", async () => {
    const { stdout, stderr } = await run({ FI_TEST_BASE_URL: BASE });
    expect(stdout).toContain("INIT_NONE");
    expect(stderr).toContain("FI_ALLOW_INSECURE_HTTP");
  });
  test("the environment variable does the same as the option", async () => {
    expect((await run({ FI_TEST_BASE_URL: BASE, FI_ALLOW_INSECURE_HTTP: "1" })).stdout).toContain("INIT_OK");
  });
});
