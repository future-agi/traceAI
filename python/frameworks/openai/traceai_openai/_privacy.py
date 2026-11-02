"""Telemetry-only copies; never mutate SDK requests, responses or exceptions."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from fi_instrumentation import TraceConfig

REDACTED = "__REDACTED__"


def redact(value: Any, secrets: Iterable[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, (list, tuple)):
        return type(value)(redact(item, secrets) for item in value)
    return value


def request_credentials(instance: Any, options: Any, parameters: Mapping) -> tuple[str, ...]:
    """Read static credentials only; never invoke a caller's token provider."""
    values = [getattr(instance, "api_key", None)]
    for headers in (getattr(instance, "_custom_headers", None), getattr(options, "headers", None)):
        if isinstance(headers, Mapping):
            for name, value in headers.items():
                if str(name).lower() in {"authorization", "api-key", "x-api-key"}:
                    values.append(value)
                    if isinstance(value, str) and value.lower().startswith("bearer "):
                        values.append(value[7:])
    tools = parameters.get("tools")
    if isinstance(tools, (list, tuple)):
        for tool in tools:
            if isinstance(tool, Mapping) and tool.get("type") == "mcp":
                values.append(tool.get("authorization"))
                headers = tool.get("headers")
                if isinstance(headers, Mapping):
                    values.extend(headers.values())
    secrets = set()
    for value in values:
        if isinstance(value, str) and value:
            secrets.update((value, json.dumps(value)[1:-1], repr(value)[1:-1]))
    return tuple(sorted(secrets, key=len, reverse=True))


def safe_tool(tool: Any) -> Any:
    if isinstance(tool, Mapping) and tool.get("type") == "mcp":
        return {key: value for key, value in tool.items() if key not in {"authorization", "headers"}}
    return tool


def safe_request(parameters: Mapping) -> dict:
    """MCP headers are arbitrary authentication material, not tool schemas."""
    result = dict(parameters)
    tools = result.get("tools")
    if isinstance(tools, (list, tuple)):
        result["tools"] = [safe_tool(tool) for tool in tools]
    return result


def response_output(response: Any, config: TraceConfig | None = None) -> dict:
    """Only response-owned data belongs in output, not echoed instructions/tools.

    An allowlist also keeps new SDK request-echo fields from bypassing input
    privacy flags. The SDK response returned to the application is unchanged.
    """
    fields = {"id", "object", "created_at", "model", "status", "output", "usage",
              "error", "incomplete_details"}
    if config is not None and getattr(config, "hide_error_details", False):
        fields.difference_update({"error", "incomplete_details"})
    return response.model_dump(include=fields, exclude_none=True, mode="json")
