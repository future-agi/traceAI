import { getProviderAttributes } from "../providerAttributes";

function attributes(baseURL: unknown, model = "anthropic/claude-test") {
  return getProviderAttributes({ _client: { baseURL } }, model);
}

describe("provider endpoint resolution", () => {
  test.each([
    ["https://api.openai.com/v1", "openai"],
    ["https://API.OPENAI.COM./v1", "openai"],
    ["https://deployment.openai.azure.com/openai/deployments/alias", "azure"],
    ["https://deployment.services.ai.azure.com/openai/v1", "azure"],
    ["https://deployment.cognitiveservices.azure.com/openai/v1", "azure"],
    ["https://api.cloudflare.com/client/v4/accounts/account/ai/v1/", "anthropic"],
    ["https://gateway.ai.cloudflare.com/v1/account/gateway/compat/", "anthropic"],
    ["https://gateway.ai.cloudflare.com/v1/account/gateway/openai", "openai"],
    ["https://gateway.ai.cloudflare.com/v1/account/gateway/workers-ai/v1", "cloudflare"],
    ["https://gateway.ai.cloudflare.com/v1/account/gateway/dynamic/anthropic", undefined],
    ["https://api.cloudflare.com/client/v4/accounts/account/unrelated", undefined],
    ["https://api.openai.com.attacker.invalid/v1", undefined],
    ["https://api.cloudflare.com.attacker.invalid/client/v4/accounts/account/ai/v1", undefined],
    ["https://gateway.ai.cloudflare.com.attacker.invalid/v1/account/gateway/compat", undefined],
    ["https://notopenai.azure.com/v1", undefined],
    ["https://deployment.openai.azure.com.attacker.invalid/v1", undefined],
    ["http://127.0.0.1:8000/v1", undefined],
    ["https://my-proxy.invalid/v1", undefined],
  ])("%s resolves only trustworthy routing information", (baseURL, provider) => {
    expect(attributes(baseURL)["gen_ai.provider.name"]).toBe(provider);
    expect(attributes(baseURL)["server.address"]).toBe(new URL(baseURL).hostname.toLowerCase().replace(/\.$/, ""));
  });

  it("exports only the endpoint host and explicit port, never URL secrets or account identifiers", () => {
    expect(attributes("https://private-user:private-password@api.cloudflare.com:8443/client/v4/accounts/private-account/ai/v1?token=private-token#private-fragment")).toEqual({
      "server.address": "api.cloudflare.com", "server.port": 8443, "gen_ai.provider.name": "anthropic",
    });
  });

  test.each(["", "not a url", "file:///tmp/local", undefined, null, 123])("invalid baseURL %s does not break requests", (baseURL) => {
    expect(attributes(baseURL)).toEqual({});
  });

  it("tolerates absent or unexpected SDK internals", () => {
    expect(getProviderAttributes(null, "gpt-test")).toEqual({});
    expect(getProviderAttributes({}, "gpt-test")).toEqual({});
    expect(getProviderAttributes({ get _client() { throw new Error("unavailable"); } }, "gpt-test")).toEqual({});
  });

  test.each(["anthropic/", "Anthropic/model", "gpt-test", "claude-test", "dynamic/anthropic/model", "toString/model", "constructor/model"])("does not guess a provider for %s", (model) => {
    expect(attributes("https://gateway.ai.cloudflare.com/v1/account/gateway/compat", model)).toEqual({ "server.address": "gateway.ai.cloudflare.com" });
  });
});
