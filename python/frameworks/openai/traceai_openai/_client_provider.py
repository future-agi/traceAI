"""Explicit provider attribution for individual OpenAI-compatible SDK clients."""

import re
from typing import Optional, Union

from openai import AsyncOpenAI, OpenAI

_Client = Union[OpenAI, AsyncOpenAI]
_PROVIDER_ATTRIBUTE = "_traceai_provider_name"
_PROVIDER_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}")


def set_client_provider(client: _Client, *, provider: Optional[str]) -> None:
    """Set ``gen_ai.provider.name`` for requests made by this client instance.

    Use a lowercase provider identifier, such as ``ollama`` or ``vllm``, not a
    URL or credential. ``None`` clears the override and restores host detection.
    The override takes precedence over host detection, is never sent to the
    server, and does not keep the client alive. Set it before starting requests.

    This does not install instrumentation: call ``OpenAIInstrumentor.instrument``
    as usual. Copies made with ``copy`` or ``with_options`` are separate clients
    and need their own override, especially when changing their base URL.
    """
    if not isinstance(client, (OpenAI, AsyncOpenAI)):
        raise TypeError("client must be an OpenAI or AsyncOpenAI instance")
    if provider is not None and not (
        isinstance(provider, str) and _PROVIDER_NAME.fullmatch(provider)
    ):
        raise ValueError(
            "provider must be a lowercase identifier of 1-64 characters "
            "(letters, digits, '.', '_' or '-', starting with a letter), or None"
        )
    setattr(client, _PROVIDER_ATTRIBUTE, provider)


def _get_client_provider(client: _Client) -> Optional[str]:
    provider = getattr(client, _PROVIDER_ATTRIBUTE, None)
    return provider if isinstance(provider, str) else None
