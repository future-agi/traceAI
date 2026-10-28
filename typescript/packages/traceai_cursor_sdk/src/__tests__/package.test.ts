import * as fs from "fs";
import * as path from "path";
import * as api from "../index";

const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "..", "package.json"), "utf8"));

describe("package surface", () => {
  it("exports the instrumentation API", () => {
    expect(typeof api.CursorSDKInstrumentation).toBe("function");
    expect(typeof api.instrumentCursorSDK).toBe("function");
    expect(typeof api.shutdown).toBe("function");
    expect(typeof api.resolveContentPolicy).toBe("function");
    expect(typeof api.isPatched).toBe("function");
    expect(api.INSTRUMENTATION_NAME).toBe(pkg.name);
    expect(api.SpanNames.RUN).toBe("Cursor Agent run");
  });

  it("VERSION matches package.json", () => {
    expect(api.VERSION).toBe(pkg.version);
  });

  it("does not depend on @cursor/sdk at runtime (peer only)", () => {
    expect(pkg.dependencies["@cursor/sdk"]).toBeUndefined();
    expect(pkg.peerDependencies["@cursor/sdk"]).toBeDefined();
    expect(pkg.peerDependenciesMeta["@cursor/sdk"].optional).toBe(true);
  });

  it("instrumentCursorSDK() patches the given module", () => {
    const Agent = { create: async () => ({}) };
    const instrumentation = api.instrumentCursorSDK({ Agent });
    expect(api.isPatched(Agent)).toBe(true);
    instrumentation.unpatch();
    instrumentation.disable();
    expect(api.isPatched(Agent)).toBe(false);
  });
});
