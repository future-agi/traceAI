import { LLMProvider } from "@traceai/fi-semantic-conventions";

// Keep the explicit host mapping aligned with the Python OpenAI instrumentor.
// A model name identifies a model, not the service receiving the request.
const HOSTS: Readonly<Record<string, string>> = {
  "api.openai.com": LLMProvider.OPENAI,
  "generativelanguage.googleapis.com": LLMProvider.GOOGLE,
  "api.anthropic.com": LLMProvider.ANTHROPIC,
  "api.mistral.ai": LLMProvider.MISTRALAI,
  "api.deepseek.com": LLMProvider.DEEPSEEK,
  "api.x.ai": LLMProvider.XAI,
  "api.together.ai": LLMProvider.TOGETHER,
  "inference.baseten.co": "baseten",
  "dashscope.aliyuncs.com": "dashscope",
  "dashscope-intl.aliyuncs.com": "dashscope",
  "dashscope-us.aliyuncs.com": "dashscope",
  "api.perplexity.ai": "perplexity",
  "api.orcarouter.ai": "orcarouter",
  "api.anannas.ai": "anannas",
  "anannas.ai": "anannas",
  "api.doubleword.ai": "doubleword",
};

const SUFFIXES: ReadonlyArray<readonly [string, string]> = [
  [".openai.azure.com", LLMProvider.AZURE],
  [".services.ai.azure.com", LLMProvider.AZURE],
  [".cognitiveservices.azure.com", LLMProvider.AZURE],
  ["-aiplatform.googleapis.com", "vertexai"],
  [".maas.aliyuncs.com", "dashscope"],
  [".cloud.databricks.com", "databricks"],
  [".gcp.databricks.com", "databricks"],
  [".azuredatabricks.net", "databricks"],
  [".xenon.lepton.run", "lepton"],
];

/** Read the SDK resource's owning client on each call, never exporting its URL. */
export function providerForResource(resource: unknown): string {
  try {
    // OpenAI 4.0 uses `client`; later SDKs use `_client`.
    const sdkResource = resource as {
      _client?: { baseURL?: unknown };
      client?: { baseURL?: unknown };
    } | null;
    const baseURL = (sdkResource?._client ?? sdkResource?.client)?.baseURL;
    if (typeof baseURL !== "string") return LLMProvider.OPENAI;
    const url = new URL(baseURL);
    if (url.protocol !== "https:" && url.protocol !== "http:") return LLMProvider.OPENAI;
    const host = url.hostname.toLowerCase().replace(/\.+$/, "");
    if (Object.prototype.hasOwnProperty.call(HOSTS, host)) return HOSTS[host];
    for (const [suffix, provider] of SUFFIXES) {
      if (host.endsWith(suffix)) return provider;
    }
  } catch {
    // Instrumentation must not break a request on an absent or malformed URL.
  }
  // Preserve the historical fallback for custom proxies and local endpoints.
  return LLMProvider.OPENAI;
}
