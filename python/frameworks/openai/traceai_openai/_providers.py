"""Resolve providers from documented OpenAI-compatible endpoints.

Most providers are identified by their API host. Cloudflare's unified endpoints
also require an explicit model prefix; dynamic or unknown routes stay unlabelled.
Other unknown hosts keep the instrumentor's historical "openai" fallback.
"""

import re
from typing import Optional

from fi_instrumentation.fi_types import FiLLMProviderValues

DEFAULT_PROVIDER = FiLLMProviderValues.OPENAI.value

# Cloudflare's documented unified endpoints, not arbitrary proxies or look-alikes.
# https://developers.cloudflare.com/ai-gateway/usage/rest-api/#model-naming
# https://developers.cloudflare.com/ai-gateway/usage/chat-completion/#parameters
CLOUDFLARE_ENDPOINTS = {
    "gateway.ai.cloudflare.com": r"/v1/[^/]+/[^/]+/compat/?",
    "api.cloudflare.com": r"/client/v4/accounts/[^/]+/ai/v1/?",
}
_CLOUDFLARE_MODEL_PROVIDERS = {
    "openai": FiLLMProviderValues.OPENAI.value,
    "anthropic": FiLLMProviderValues.ANTHROPIC.value,
    "google": FiLLMProviderValues.GOOGLE.value,
    "google-ai-studio": FiLLMProviderValues.GOOGLE.value,
    "xai": FiLLMProviderValues.XAI.value,
    "grok": FiLLMProviderValues.XAI.value,
    "@cf": "cloudflare",
}


def provider_from_cloudflare(*, host: str, path: str, model: object) -> Optional[str]:
    """Resolve explicit routes only; dynamic routing does not identify a provider."""
    pattern = CLOUDFLARE_ENDPOINTS.get(host)
    if pattern is None or not re.fullmatch(pattern, path) or not isinstance(model, str):
        return None
    prefix, _, name = model.partition("/")
    if not name:
        return None
    return _CLOUDFLARE_MODEL_PROVIDERS.get(prefix)


# Exact API hosts.
_HOSTS = {
    "api.openai.com": FiLLMProviderValues.OPENAI.value,
    "generativelanguage.googleapis.com": FiLLMProviderValues.GOOGLE.value,
    "api.anthropic.com": FiLLMProviderValues.ANTHROPIC.value,
    "api.mistral.ai": FiLLMProviderValues.MISTRALAI.value,
    "api.deepseek.com": FiLLMProviderValues.DEEPSEEK.value,
    "api.x.ai": FiLLMProviderValues.XAI.value,
    "api.together.ai": "together",
    "inference.baseten.co": "baseten",
    "dashscope.aliyuncs.com": "dashscope",
    "dashscope-intl.aliyuncs.com": "dashscope",
    "dashscope-us.aliyuncs.com": "dashscope",
    "api.perplexity.ai": "perplexity",
    "api.orcarouter.ai": "orcarouter",
    "api.anannas.ai": "anannas",
    "anannas.ai": "anannas",
    "api.doubleword.ai": "doubleword",
}

# Vendors that give every resource, workspace or endpoint its own subdomain.
_SUFFIXES = (
    (".openai.azure.com", FiLLMProviderValues.AZURE.value),
    (".services.ai.azure.com", FiLLMProviderValues.AZURE.value),
    (".cognitiveservices.azure.com", FiLLMProviderValues.AZURE.value),
    ("-aiplatform.googleapis.com", FiLLMProviderValues.VERTEXAI.value),
    (".maas.aliyuncs.com", "dashscope"),
    (".cloud.databricks.com", "databricks"),
    (".gcp.databricks.com", "databricks"),
    (".azuredatabricks.net", "databricks"),
    (".xenon.lepton.run", "lepton"),
)


def provider_from_host(host: Optional[str]) -> str:
    if not isinstance(host, str) or not host:
        return DEFAULT_PROVIDER
    host = host.lower().rstrip(".")
    if host in _HOSTS:
        return _HOSTS[host]
    for suffix, provider in _SUFFIXES:
        if host.endswith(suffix):
            return provider
    return DEFAULT_PROVIDER
