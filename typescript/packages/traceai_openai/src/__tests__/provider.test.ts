import { providerForResource } from "../provider";

describe("providerForResource", () => {
  it.each([
    ["api.openai.com", "openai"],
    ["generativelanguage.googleapis.com", "google"],
    ["api.anthropic.com", "anthropic"],
    ["api.mistral.ai", "mistralai"],
    ["api.deepseek.com", "deepseek"],
    ["api.x.ai", "xai"],
    ["api.together.ai", "together"],
    ["inference.baseten.co", "baseten"],
    ["dashscope.aliyuncs.com", "dashscope"],
    ["dashscope-intl.aliyuncs.com", "dashscope"],
    ["dashscope-us.aliyuncs.com", "dashscope"],
    ["api.perplexity.ai", "perplexity"],
    ["api.orcarouter.ai", "orcarouter"],
    ["api.anannas.ai", "anannas"],
    ["anannas.ai", "anannas"],
    ["api.doubleword.ai", "doubleword"],
    ["resource.openai.azure.com", "azure"],
    ["resource.services.ai.azure.com", "azure"],
    ["resource.cognitiveservices.azure.com", "azure"],
    ["us-central1-aiplatform.googleapis.com", "vertexai"],
    ["endpoint.maas.aliyuncs.com", "dashscope"],
    ["workspace.cloud.databricks.com", "databricks"],
    ["workspace.gcp.databricks.com", "databricks"],
    ["workspace.azuredatabricks.net", "databricks"],
    ["endpoint.xenon.lepton.run", "lepton"],
  ])("recognizes %s only at a hostname boundary", (host, provider) => {
    expect(providerForResource({ _client: { baseURL: `https://${host.toUpperCase()}.:8443/v1` } })).toBe(provider);
    expect(providerForResource({ client: { baseURL: `https://${host}/v1` } })).toBe(provider);
    expect(providerForResource({ _client: { baseURL: `https://${host}.evil.test/v1` } })).toBe("openai");
    expect(providerForResource({ _client: { baseURL: `https://evil.test/${host}` } })).toBe("openai");
  });

  it.each([undefined, null, {}, { _client: null }, { _client: {} }, { _client: { baseURL: 123 } },
    { _client: { baseURL: "not a url" } }, { _client: { baseURL: "ftp://api.anannas.ai" } },
    { _client: { baseURL: "https://constructor" } }, { _client: { baseURL: "http://127.0.0.1/v1" } },
    { _client: { baseURL: "https://notopenai.azure.com" } },
  ])("falls back safely for %p", (resource) => {
    expect(providerForResource(resource)).toBe("openai");
  });

  it("does not fail the application when resource access throws", () => {
    expect(providerForResource({ get _client() { throw new Error("synthetic getter failure"); } })).toBe("openai");
  });
});
