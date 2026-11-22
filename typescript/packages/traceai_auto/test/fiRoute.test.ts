import { spawn } from "node:child_process";
import { join } from "node:path";
import { resolveConfig } from "../src/config";

function run(app: string, env: Record<string, string>, stream: "stdout" | "stderr" = "stderr"): Promise<string> {
  return new Promise(done => {
    const child = spawn(process.execPath, ["--import", "tsx", join(__dirname, "apps", app)], {
      cwd: join(__dirname, ".."), timeout: 60000,
      env: { PATH: process.env.PATH, HOME: process.env.HOME, ...env },
    });
    let out = ""; child[stream].on("data", c => { out += c; }); child.on("close", () => done(out));
  });
}

describe("the Future AGI route", () => {
  test("an absent or incompatible route factory is reported with a clear message and no value", async () => {
    const stderr = await run("incompatible-route.ts", { FI_API_KEY: "synthetic-fi-key-0001", FI_SECRET_KEY: "synthetic-fi-secret-0001", FI_PROJECT_NAME: "route" });
    expect(stderr).toMatch(/compatible Future AGI route/);
    expect(stderr).not.toContain("synthetic-fi");
  });
  test("a refused plaintext base URL is reported with the setting to change, and no value", async () => {
    const env = { FI_TEST_BASE_URL: "http://collector.example:4318", FI_PROJECT_NAME: "route" };
    const stderr = await run("refused-base-url.ts", env);
    expect(stderr).toContain("FI_ALLOW_INSECURE_HTTP");
    expect(stderr).not.toMatch(/synthetic-fi|collector\.example/);
    // With the opt-in the route is built.
    expect(await run("refused-base-url.ts", { ...env, FI_ALLOW_INSECURE_HTTP: "1" }, "stdout")).toContain("INIT_OK");
  });
  test("an ambiguous base URL is refused with its own message", async () => {
    const stderr = await run("refused-base-url.ts", { FI_TEST_BASE_URL: "https://api.futureagi.com%5C@collector.example", FI_PROJECT_NAME: "route" });
    expect(stderr).toMatch(/names its host in a way parsers read differently/);
    expect(stderr).not.toMatch(/synthetic-fi|collector\.example/);
  });
  test("init() with a whitespace-only key builds no Future AGI route", async () => {
    const stdout = await run("whitespace-key.ts", { FI_API_KEY: "   ", FI_SECRET_KEY: "synthetic-fi-secret-0001", FI_PROJECT_NAME: "route" }, "stdout");
    expect(stdout).toContain("INIT_DONE");
    expect(stdout).not.toContain("ROUTE_BUILT");
  });
  test("a whitespace-only key is not a credential", () => {
    const config = resolveConfig({ apiKey: "   ", secretKey: "synthetic-secret-0001", configFile: "/nonexistent/futureagi.toml" }, {});
    expect(config.apiKey).toBeUndefined();
  });
});
