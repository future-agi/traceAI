import { integrations, inventory } from "../src/libraries";
import { resolveConfig } from "../src/config";

const HOSTED = ["@anthropic-ai/bedrock-sdk", "@anthropic-ai/vertex-sdk"];

test("the anthropic entry covers the Bedrock and Vertex AI clients", () => {
  const entry = integrations.find(item => item.name === "anthropic")!;
  expect(entry.package).toBe("@traceai/anthropic");
  expect(entry.className).toBe("AnthropicInstrumentation");
  for (const name of HOSTED) {
    expect(entry.targets).toContain(name);
    expect(entry.modules).toContain(name);
  }
  expect(entry.manual).toBeUndefined();
});

test("the anthropic entry is pending when only a hosted client package is installed", () => {
  const entry = integrations.find(item => item.name === "anthropic")!;
  const config = resolveConfig({}, {});
  // Both hosted packages are installed in this workspace; the registry reports the entry as pending or
  // missing its instrumentor, never as not installed.
  const status = inventory(config).find(row => row.name === "anthropic")!;
  expect(entry.targets).toEqual(expect.arrayContaining(HOSTED));
  expect(["pending", "missing_instrumentor"]).toContain(status.status);
});
