import { execFileSync } from "node:child_process";
import path from "node:path";

it.each(["request", "stream"])("runs the documented %s with real SDKs and a loopback OTLP receiver", (mode) => {
  const directory = path.resolve(__dirname, "../..");
  const output = execFileSync(process.execPath, [path.join(directory, "contract/readme.cjs"), directory, mode], {
    encoding: "utf8", timeout: 20000,
    // Do not inherit credentials, proxies, or tracing configuration from the test runner.
    env: { PATH: process.env.PATH, NODE_ENV: "test" },
  });
  expect(output).toContain("OTLP parenting and privacy passed");
}, 25000);
