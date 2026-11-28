import { spawnSync } from "node:child_process";
import * as path from "node:path";

const fixture = path.resolve(__dirname, "../../test/requestRejection.cjs");
function run(endpoint: string, mode: string, instrumented: boolean) {
  const env: NodeJS.ProcessEnv = {};
  for (const key of ["PATH", "HOME", "SYSTEMROOT", "TMPDIR", "OPENAI_TEST_MODULE"]) {
    if (process.env[key]) env[key] = process.env[key];
  }
  const result = spawnSync(process.execPath, ["--unhandled-rejections=throw", fixture,
    endpoint, mode, instrumented ? "instrumented" : "baseline"], {
    env, encoding: "utf8", timeout: 20000,
  });
  if (result.error) throw result.error;
  return result;
}
function completed(endpoint: string, mode: string, instrumented: boolean) {
  const result = run(endpoint, mode, instrumented);
  if (result.status !== 0) throw new Error(`Child exited ${result.status}: ${result.stderr}`);
  return JSON.parse(result.stdout.trim().split("\n").pop()!);
}

describe.each(["chat", "embeddings"])("dropped request rejections: %s", endpoint => {
  test("emits exactly one unhandledRejection like the native SDK", () => {
    const native = completed(endpoint, "dropped", false);
    const traced = completed(endpoint, "dropped", true);
    expect(native.unhandled_events).toHaveLength(1);
    expect(traced.unhandled_events).toEqual(native.unhandled_events);
    expect(traced.spans).toEqual([{ status: 2, exceptions: 1 }]);
  }, 30000);

  test("exits nonzero under Node's throw policy like the native SDK", () => {
    const native = run(endpoint, "default-exit", false);
    const traced = run(endpoint, "default-exit", true);
    expect(native.status).toBe(1);
    expect(native.stderr).toContain("synthetic request failure");
    expect(traced.status).toBe(native.status);
    expect(traced.stderr).toContain("synthetic request failure");
  }, 30000);
});

describe.each(["chat", "embeddings"])("request rejection controls: %s", endpoint => {
  test.each(["awaited", "then", "catch", "finally", "withResponse", "asResponse", "unwrap"])(
    "%s preserves the error and records one failed span without an unhandled rejection", mode => {
      const native = completed(endpoint, mode, false);
      const traced = completed(endpoint, mode, true);
      expect(traced.caught).toEqual(native.caught);
      expect(traced.sameError).toBe(true);
      expect(native.unhandled_events).toEqual([]);
      expect(traced.unhandled_events).toEqual([]);
      expect(traced.spans).toEqual([{ status: 2, exceptions: 1 }]);
    }, 30000,
  );

  test.each(["with-response", "raw-response", "error-body"])(
    "%s preserves the native helpers, lazy parsing and response identity", mode => {
      const native = completed(endpoint, mode, false);
      const traced = completed(endpoint, mode, true);
      expect(native.unhandled_events).toEqual([]);
      expect(traced.unhandled_events).toEqual([]);
      expect(traced.spans).toEqual([{ status: mode === "error-body" ? 2 : 1, exceptions: mode === "error-body" ? 1 : 0 }]);
    }, 30000,
  );
});

test("stream controls preserve the native stream and its helpers", () => {
  const native = completed("chat", "stream", false);
  const traced = completed("chat", "stream", true);
  expect(native.unhandled_events).toEqual([]);
  expect(traced.unhandled_events).toEqual([]);
  expect(traced.spans).toEqual([{ status: 1, exceptions: 0 }]);
}, 30000);
