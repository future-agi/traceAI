"""Map an OpenAI-compatible client's base_url host to gen_ai.provider.name.

The mapping is deliberately small and explicit: a host is only labelled with a
vendor when it is that vendor's documented API host. Everything else (proxies,
loopback servers, self-hosted endpoints, look-alike domains) keeps the
instrumentor's historical label, "openai", rather than a guess.
"""

from typing import Optional

from fi_instrumentation.fi_types import FiLLMProviderValues

DEFAULT_PROVIDER = FiLLMProviderValues.OPENAI.value

# Exact API hosts.
_HOSTS = {
    "api.openai.com": FiLLMProviderValues.OPENAI.value,
    "generativelanguage.googleapis.com": FiLLMProviderValues.GOOGLE.value,
    "api.anthropic.com": FiLLMProviderValues.ANTHROPIC.value,
    "api.mistral.ai": FiLLMProviderValues.MISTRALAI.value,
    "api.deepseek.com": FiLLMProviderValues.DEEPSEEK.value,
    "api.minimax.io": "minimax",
    "api.minimax.cn": "minimax",
    "api.minimaxi.com": "minimax",
    "api.minimax.chat": "minimax",
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
