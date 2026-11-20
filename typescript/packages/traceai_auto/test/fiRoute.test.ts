import { spawn } from "node:child_process";
import { join } from "node:path";
import { resolveConfig } from "../src/config";

function run(app: string, env: Record<string, string>): Promise<string> {
  return new Promise(done => {
    const child = spawn(process.execPath, ["--import", "tsx", join(__dirname, "apps", app)], {
      cwd: join(__dirname, ".."), timeout: 60000,
      env: { PATH: process.env.PATH, HOME: process.env.HOME, ...env },
    });
    let stderr = ""; child.stderr.on("data", c => { stderr += c; }); child.on("close", () => done(stderr));
  });
}

describe("the Future AGI route", () => {
  test("an absent or incompatible route factory is reported with a clear message and no value", async () => {
    const stderr = await run("incompatible-route.ts", { FI_API_KEY: "synthetic-fi-key-0001", FI_SECRET_KEY: "synthetic-fi-secret-0001", FI_PROJECT_NAME: "route" });
    expect(stderr).toMatch(/compatible Future AGI route/);
    expect(stderr).not.toContain("synthetic-fi");
  });
  test("a whitespace-only key is not a credential", () => {
    const config = resolveConfig({ apiKey: "   ", secretKey: "synthetic-secret-0001", configFile: "/nonexistent/futureagi.toml" }, {});
    expect(config.apiKey).toBeUndefined();
  });
});
