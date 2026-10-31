import { Attributes, diag } from "@opentelemetry/api";
import { LLMProvider, SemanticConventions } from "@traceai/fi-semantic-conventions";

// Only explicit Cloudflare route identifiers, never model-family name heuristics.
// https://developers.cloudflare.com/ai-gateway/usage/rest-api/#model-naming
// https://developers.cloudflare.com/ai-gateway/usage/chat-completion/#parameters
const CLOUDFLARE_PROVIDERS = new Map<string, string>([
  ["openai", LLMProvider.OPENAI],
  ["anthropic", LLMProvider.ANTHROPIC],
  ["google", LLMProvider.GOOGLE],
  ["google-ai-studio", LLMProvider.GOOGLE],
  ["xai", LLMProvider.XAI],
  ["grok", LLMProvider.XAI],
  ["@cf", "cloudflare"],
  ["workers-ai", "cloudflare"],
]);

/**
 * The OpenAI SDK is a protocol client, not proof that OpenAI served a request.
 * Record the endpoint separately from a known provider. Unknown/custom endpoints
 * and dynamic Cloudflare aliases intentionally have no provider attribute.
 * This describes the requested route; native gateway spans describe actual routing.
 */
export function getProviderAttributes(resource: unknown, model: string): Attributes {
  try {
    // Early OpenAI 4.x resources expose `client`; later releases use `_client`.
    const sdkResource = resource as { _client?: { baseURL?: unknown }; client?: { baseURL?: unknown } } | null;
    const baseURL = (sdkResource?._client ?? sdkResource?.client)?.baseURL;
    if (typeof baseURL !== "string") return {};
    const url = new URL(baseURL);
    if (url.protocol !== "https:" && url.protocol !== "http:") return {};
    const host = url.hostname.toLowerCase().replace(/\.$/, "");
    // Do not export credentials, account/gateway identifiers, query parameters or fragments.
    const attributes: Attributes = { "server.address": host };
    if (url.port) attributes["server.port"] = Number(url.port);

    let provider: string | undefined;
    if (host === "api.openai.com") {
      provider = LLMProvider.OPENAI;
    } else if ([".openai.azure.com", ".services.ai.azure.com", ".cognitiveservices.azure.com"].some((suffix) => host.endsWith(suffix))) {
      provider = LLMProvider.AZURE;
    } else if (
      (host === "api.cloudflare.com" && /^\/client\/v4\/accounts\/[^/]+\/ai\/v1\/?$/.test(url.pathname)) ||
      (host === "gateway.ai.cloudflare.com" && /^\/v1\/[^/]+\/[^/]+\/compat\/?$/.test(url.pathname))
    ) {
      // Require both a known route prefix and a non-empty model. Never infer from
      // an unprefixed response model (or a dynamic route's name).
      const slash = typeof model === "string" ? model.indexOf("/") : -1;
      if (slash > 0 && slash < model.length - 1) {
        provider = CLOUDFLARE_PROVIDERS.get(model.slice(0, slash));
      }
    } else if (host === "gateway.ai.cloudflare.com") {
      // Provider-specific OpenAI-compatible endpoint (not /compat).
      const route = /^\/v1\/[^/]+\/[^/]+\/(openai|workers-ai)(?:\/v1)?\/?$/.exec(url.pathname)?.[1];
      if (route === "openai") provider = LLMProvider.OPENAI;
      if (route === "workers-ai") provider = "cloudflare";
    }
    if (provider !== undefined) attributes[SemanticConventions.LLM_PROVIDER] = provider;
    return attributes;
  } catch {
    // Fail open without logging the URL or error text, which can contain credentials.
    diag.debug("@traceai/openai: endpoint metadata unavailable; provider attributes omitted");
    return {};
  }
}
