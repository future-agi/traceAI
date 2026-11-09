"""Provider identity from adapter classes and known compatible endpoints.

Never infer a provider from a model name: Azure deployments and OpenAI-compatible
servers may serve the same model. No optional provider SDK is imported here.
"""

from typing import Any, Optional
from urllib.parse import urlparse


_ADAPTERS = {
    "azure_openai": "azure.ai.openai",
    "openai_like": "openai_compatible",
    "openai": "openai",
    "anthropic": "anthropic",
    "bedrock": "aws.bedrock",
    "bedrock_converse": "aws.bedrock",
    "gemini": "google",
    "google_genai": "google",
    "vertex": "gcp.vertex_ai",
    "cohere": "cohere",
    "ollama": "ollama",
    "groq": "groq",
    "mistralai": "mistral_ai",
    "together": "together",
    "fireworks": "fireworks",
    "deepseek": "deepseek",
    "openrouter": "openrouter",
}
_ENDPOINTS = {
    "api.openai.com": "openai",
    "api.groq.com": "groq",
    "api.deepseek.com": "deepseek",
    "api.together.xyz": "together",
    "api.together.ai": "together",
    "api.fireworks.ai": "fireworks",
    "openrouter.ai": "openrouter",
}


def provider_name(instance: Any) -> Optional[str]:
    provider = None
    # A custom subclass still inherits the concrete adapter's identity. The
    # first known class wins, so Azure/Groq/OpenAILike do not become OpenAI.
    for cls in type(instance).__mro__:
        parts = cls.__module__.split(".")
        if len(parts) >= 3 and parts[0] == "llama_index" and parts[1] in ("llms", "multi_modal_llms"):
            provider = _ADAPTERS.get(parts[2])
            if provider:
                break
    if provider in ("openai", "openai_compatible"):
        endpoint = getattr(instance, "api_base", None)
        if isinstance(endpoint, str):
            try:
                host = (urlparse(endpoint).hostname or "").lower().rstrip(".")
            except ValueError:
                return provider
            if host.endswith((".openai.azure.com", ".services.ai.azure.com")):
                return "azure.ai.openai"
            return _ENDPOINTS.get(host, provider)
    return provider
