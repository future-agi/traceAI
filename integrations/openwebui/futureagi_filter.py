"""title: Future AGI
author: Future AGI
version: 0.1.0
license: Apache-2.0
description: Send Open WebUI chat turns to Future AGI.
requirements: opentelemetry-api,opentelemetry-sdk,opentelemetry-exporter-otlp-proto-http
"""

# Standalone on purpose. Open WebUI installs the frontmatter requirements
# into its own environment, and importing traceAI there would pull in
# instrumentors this filter does not use.

import hashlib
import json
import logging
from typing import Any

from pydantic import BaseModel, SecretStr

logger = logging.getLogger(__name__)

_COLLECTOR_PATH = "/tracer/v1/traces"


class Valves(BaseModel):
    api_key: SecretStr
    secret_key: SecretStr
    endpoint: str = "https://api.futureagi.com"
    project: str
    redact: bool = False
    include_email_hash: bool = False


def collector_endpoint(endpoint: str) -> str:
    trimmed = endpoint.rstrip("/")
    suffix = _COLLECTOR_PATH.rstrip("/")
    if trimmed.endswith(suffix):
        return trimmed
    return f"{trimmed}{_COLLECTOR_PATH}"


def email_hash(email: str) -> str:
    return hashlib.sha256(email.encode()).hexdigest()


def span_attributes(form_data: dict[str, Any], valves: Valves) -> dict[str, Any]:
    """Attributes for one chat turn. Never includes a raw email or a credential."""
    attributes: dict[str, Any] = {"gen_ai.operation.name": "chat"}
    model = form_data.get("model")
    if model:
        attributes["gen_ai.request.model"] = model
        attributes["llm.model_name"] = model
    user_id = form_data.get("user_id")
    if user_id:
        attributes["user.id"] = user_id
    chat_id = form_data.get("chat_id")
    if chat_id:
        attributes["session.id"] = chat_id
        attributes["gen_ai.conversation.id"] = chat_id
    usage = form_data.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    if prompt_tokens is None and completion_tokens is None:
        attributes["gen_ai.usage.unavailable"] = True
    else:
        if prompt_tokens is not None:
            attributes["gen_ai.usage.input_tokens"] = prompt_tokens
            attributes["llm.token_count.prompt"] = prompt_tokens
        if completion_tokens is not None:
            attributes["gen_ai.usage.output_tokens"] = completion_tokens
            attributes["llm.token_count.completion"] = completion_tokens
    if valves.include_email_hash and form_data.get("email"):
        attributes["user.email_hash"] = email_hash(form_data["email"])
    if not valves.redact:
        messages = form_data.get("messages")
        if messages is not None:
            attributes["input.value"] = json.dumps(messages, default=str)
        output = form_data.get("output")
        if output is not None:
            attributes["output.value"] = output
    return attributes


class Filter:
    def __init__(self) -> None:
        self.valves: Valves | None = None
        self._turns: dict[str, dict[str, Any]] = {}

    def inlet(self, body: dict[str, Any], __user__: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            turn_id = str(body.get("chat_id") or body.get("id") or id(body))
            self._turns[turn_id] = {"messages": body.get("messages"), "user": __user__ or {}}
        except Exception:
            logger.warning("Future AGI inlet failed", exc_info=True)
        return body

    def outlet(self, body: dict[str, Any], __user__: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            if self.valves is not None:
                self._export(span_attributes(body, self.valves))
        except Exception:
            logger.warning("Future AGI outlet failed", exc_info=True)
        return body

    def _export(self, attributes: dict[str, Any]) -> None:
        """Lazy import so a missing exporter never breaks the chat."""
        assert self.valves is not None
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor

        exporter = OTLPSpanExporter(
            endpoint=collector_endpoint(self.valves.endpoint),
            headers={
                "X-Api-Key": self.valves.api_key.get_secret_value(),
                "X-Secret-Key": self.valves.secret_key.get_secret_value(),
            },
        )
        provider = TracerProvider(
            resource=Resource.create({"project_name": self.valves.project, "project_type": "observe"})
        )
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        with provider.get_tracer("openwebui-futureagi").start_as_current_span("openwebui.chat") as span:
            for key, value in attributes.items():
                span.set_attribute(key, value)

    def stream(self, event: dict[str, Any]) -> dict[str, Any]:
        return event
