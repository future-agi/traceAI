import { ChromaDBInstrumentation } from "../instrumentation";

class TestInstrumentation extends ChromaDBInstrumentation {
  supportedVersions() {
    return this.init().supportedVersions;
  }
}

test("peer dependency and automatic instrumentation share the executable Node floor", () => {
  const instrumentation = new TestInstrumentation({ instrumentationConfig: { enabled: false } });
  try {
    // 1.0.0 publishes no Node entry point. 1.1.0 is exercised in the real SDK matrix.
    const ranges = ["^1.1.0", "^2.0.0", "^3.0.0"];
    expect(instrumentation.supportedVersions()).toEqual(ranges);
    expect(require("../../package.json").peerDependencies.chromadb).toBe(ranges.join(" || "));
  } finally {
    instrumentation.disable();
  }
});
