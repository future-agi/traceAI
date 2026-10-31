"""CrewAI-specific structural projection; deliberately not free-text secret detection."""

import json
import re
from dataclasses import fields
from typing import Any

from fi_instrumentation import TraceConfig

REDACTED = "__REDACTED__"
_CREDENTIAL_KEYS = frozenset(
    {
        "apikey",
        "xapikey",
        "authorization",
        "proxyauthorization",
        "accesstoken",
        "refreshtoken",
        "authtoken",
        "authenticationtoken",
        "bearertoken",
        "sessiontoken",
        "idtoken",
        "apitoken",
        "csrftoken",
        "clientsecret",
        "clientsecretkey",
        "clientkey",
        "secretkey",
        "secretaccesskey",
        "accesskey",
        "accesskeyid",
        "secret",
        "secrets",
        "apisecret",
        "secrettoken",
        "privatekey",
        "signingkey",
        "password",
        "passwd",
        "token",
        "credential",
        "credentials",
        "cookie",
        "setcookie",
    }
)
_CREDENTIAL_SUFFIXES = (
    "apikey",
    "authorization",
    "accesstoken",
    "refreshtoken",
    "authtoken",
    "authenticationtoken",
    "bearertoken",
    "sessiontoken",
    "idtoken",
    "apitoken",
    "csrftoken",
    "clientsecret",
    "clientsecretkey",
    "clientkey",
    "secretkey",
    "secretaccesskey",
    "accesskey",
    "accesskeyid",
    "privatekey",
    "signingkey",
    "password",
    "passwd",
)
_INPUT_ALIASES = (
    "input.value",
    "raw.input",
    "crew_inputs",
    "crew_agents",
    "crew_tasks",
    "formatted_description",
    "formatted_expected_output",
    "gen_ai.input.messages",
    "gen_ai.prompts",
    "gen_ai.prompt",
    "gen_ai.request.parameters",
    "gen_ai.tool.call.arguments",
    "tool.parameters",
    "tool_call.function.arguments",
    "message.function_call_arguments_json",
)
_OUTPUT_ALIASES = (
    "output.value",
    "raw.output",
    "gen_ai.output.messages",
    "gen_ai.completion",
    "gen_ai.tool.call.result",
)


def _credential_key(key: str) -> bool:
    # Do not classify every field containing 'token': usage and IDs are useful.
    # Flattened paths may put a value/index below a credential container, e.g.
    # headers.Authorization.0 or client.config.api_key.value.
    for part in re.split(r"[.\[\]]+", key.lower()):
        normalized = re.sub(r"[^a-z0-9]", "", part)
        if normalized in _CREDENTIAL_KEYS or normalized.endswith(_CREDENTIAL_SUFFIXES):
            return True
    return False


def _safe_data(value: Any, depth: int = 0) -> Any:
    """Copy only plain business data, never calling runtime serializers or repr."""
    if depth > 20:
        return "[omitted]"
    if type(value) is str:
        if value.lstrip().startswith(("{", "[")):
            if len(value) > 1_000_000:
                return "[omitted oversized structured data]"
            try:
                parsed = json.loads(value)
            except (ValueError, RecursionError):
                # Do not export a partially structured payload we cannot safely
                # inspect (including valid JSON beyond parser resource limits).
                return "[omitted unparseable structured data]"
            if type(parsed) in (dict, list):
                return json.dumps(_safe_data(parsed, depth + 1))
        return value
    if value is None or type(value) in (int, float, bool):
        return value
    if type(value) is dict:
        return {
            key: REDACTED if _credential_key(key) else _safe_data(item, depth + 1)
            for key, item in value.items()
            if type(key) is str
        }
    if type(value) in (list, tuple):
        return [_safe_data(item, depth + 1) for item in value]
    return "[omitted runtime object]"


def _has_prefix(key: str, prefixes: tuple) -> bool:
    return any(key == prefix or key.startswith(prefix + ".") for prefix in prefixes)


class _CrewAITraceConfig(TraceConfig):
    """Keep the caller's core policy and cover CrewAI aliases/events as well.

    Applying projection at the mask boundary also protects context attributes
    before samplers/processors see them, not just the wrapper's final setters.
    """

    def __init__(self, config: TraceConfig) -> None:
        for field in fields(TraceConfig):
            object.__setattr__(self, field.name, getattr(config, field.name))
        object.__setattr__(self, "_source", config)

    def mask(self, key: str, value: Any) -> Any:
        hide_input = (
            self.hide_inputs or self.hide_input_text or self.hide_input_messages
        )
        hide_output = (
            self.hide_outputs or self.hide_output_text or self.hide_output_messages
        )
        if (hide_input and _has_prefix(key, _INPUT_ALIASES)) or (
            hide_output and _has_prefix(key, _OUTPUT_ALIASES)
        ):
            value = REDACTED
        masked = self._source.mask(key, value)
        if masked is None:
            return None
        if _credential_key(key):
            return REDACTED
        projected = _safe_data(masked)
        # OTel attributes cannot carry dictionaries (nor lists of dictionaries).
        if type(projected) is dict or (
            type(projected) is list
            and any(type(item) in (dict, list) for item in projected)
        ):
            return json.dumps(projected)
        return projected
