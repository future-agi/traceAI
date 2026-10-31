import { MCPInstrumentation } from "../mcp";
import { VERSION } from "../version";
// eslint-disable-next-line @typescript-eslint/no-var-requires
const { version } = require("../../package.json");

describe("instrumentation version", () => {
  it("reports the package version", () => {
    const instrumentation = new MCPInstrumentation({ instrumentationConfig: { enabled: false } });
    expect(VERSION).toBe(version);
    expect(instrumentation.instrumentationVersion).toBe(version);
  });
});
