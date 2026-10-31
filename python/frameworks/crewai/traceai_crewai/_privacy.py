"""CrewAI-specific structural projection; deliberately not free-text secret detection."""

import json
import re
from dataclasses import fields
from typing import Any

from fi_instrumentation import TraceConfig

REDACTED = "__REDACTED__"
# Match credential concepts, not a finite list of vendor header spellings.
_CREDENTIAL_MARKERS = (
    "token",
    "secret",
    "password",
    "passwd",
    "passphrase",
    "apikey",
    "accesskey",
    "privatekey",
    "credential",
    "auth",
    "cookie",
    "session",
    "signature",
    "bearer",
    "jwt",
    "otp",
    "clientkey",
    "signingkey",
)
# Exact normalized exceptions only: never exempt arbitrary '*_count'/'*_id'
# names (e.g. private_token_count could still carry a credential). This documents
# the supported non-secret usage counters, model limits, tokenizer metadata,
# authorship, and service-token metadata. Extend deliberately with regressions.
_NON_SECRET_NAMES = frozenset(
    {
        "inputtokens",
        "outputtokens",
        "totaltokens",
        "prompttokens",
        "completiontokens",
        "cachedtokens",
        "cachedinputtokens",
        "cachereadtokens",
        "cachewritetokens",
        "inputtokensdetails",
        "outputtokensdetails",
        "prompttokensdetails",
        "completiontokensdetails",
        "numtokens",
        "cachecreationinputtokens",
        "cachereadinputtokens",
        "reasoningtokens",
        "audiotokens",
        "tokencount",
        "inputtokencount",
        "outputtokencount",
        "totaltokencount",
        "prompttokencount",
        "completiontokencount",
        "cachedtokencount",
        "reasoningtokencount",
        "maxtokens",
        "maxinputtokens",
        "maxoutputtokens",
        "maxcompletiontokens",
        "tokensused",
        "tokenusage",
        "tokenizer",
        "tokenizername",
        "tokenizerversion",
        "tokenid",
        "securitytokenid",
        "apisecretid",
        "vaulttokencount",
        "consultokenttl",
        "subjecttokentype",
        "servicetokencount",
        "author",
        "authors",
        "authorid",
        "authorname",
        "authorship",
        "authority",
        "sessioncount",
        "sessionduration",
        "sessiondurationms",
    }
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


def _normalized_name(name: str) -> str:
    name = re.sub(r"^x[\W_]+", "", name.lower().strip(" '\""))
    return re.sub(r"[^a-z0-9]", "", name)


def _credential_key(key: str) -> bool:
    # Inspect every path component, including credential containers followed by
    # a value/index. Exceptions apply to one complete field, never its children.
    if _normalized_name(key) in _NON_SECRET_NAMES:
        return False
    parts = [_normalized_name(part) for part in re.split(r"[.\[\]]+", key) if part]
    for normalized in parts:
        if normalized in _NON_SECRET_NAMES:
            continue
        # PIN is whole-name only, so e.g. 'shipping' is not a credential.
        if normalized == "pin" or any(
            marker in normalized for marker in _CREDENTIAL_MARKERS
        ):
            return True
    # A dot/bracket can separate words of a credential name as well as paths,
    # e.g. headers.api.key or client[private][key].
    return any(
        marker in left + right
        for left, right in zip(parts, parts[1:])
        for marker in ("apikey", "accesskey", "privatekey", "clientkey", "signingkey")
    )


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
        # Name/value header records, including their flattened representations.
        # Keep the selector and unrelated fields; redact only the paired value.
        def path(key: str) -> tuple:
            return tuple(
                _normalized_name(part) for part in re.split(r"[.\[\]]+", key) if part
            )

        credential_values = set()
        for key, item in value.items():
            if type(key) is not str or type(item) is not str:
                continue
            parts = path(key)
            if (
                parts
                and parts[-1] in ("name", "key", "header", "0")
                and _credential_key(item)
            ):
                paired_field = "1" if parts[-1] == "0" else "value"
                credential_values.add(parts[:-1] + (paired_field,))
        return {
            key: (
                REDACTED
                if _credential_key(key) or path(key) in credential_values
                else _safe_data(item, depth + 1)
            )
            for key, item in value.items()
            if type(key) is str
        }
    if type(value) in (list, tuple):
        if len(value) == 2 and type(value[0]) is str and _credential_key(value[0]):
            return [value[0], REDACTED]
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
